// Package mcpserver exposes the a2a message stream as an MCP server over
// stdio, so external coding agents (Claude Code, Codex) can participate in
// the shared conversation as thin clients of the running daemon.
package mcpserver

import (
	"context"
	"fmt"
	"path/filepath"
	"sync"
	"time"

	"github.com/modelcontextprotocol/go-sdk/mcp"
	"github.com/veridian69/cairn/a2a/internal/model"
	"github.com/veridian69/cairn/a2a/internal/state"
	"github.com/veridian69/cairn/a2a/internal/transport"
)

const daemonRemedy = "a2a daemon not reachable — start it with `a2a start`"

type Options struct {
	// Name is the participant identity this MCP session speaks as.
	Name    string
	Version string
	// DaemonURL and DataDir are injected so tests can point the server at an
	// embedded NATS server and a temp data directory (cmd wires them to
	// ReadDaemonURL / ResolveDataDir).
	DaemonURL func() (string, error)
	DataDir   func() (string, error)
}

type Server struct {
	opts Options
	mcp  *mcp.Server

	// done is closed by Shutdown to release blocking handlers. The stdio
	// transport wraps handler contexts so cancellation is not propagated to
	// them, so a signal-cancelled Run would otherwise wait out an in-flight
	// wait_for_messages — up to 300s holding the runtime lease.
	done     chan struct{}
	doneOnce sync.Once

	// waitSem serialises wait_for_messages calls. The MCP SDK dispatches tool
	// calls concurrently, and two waits reading from the same cursor would
	// deliver the same entries twice; queued waits run against the cursor the
	// earlier one advanced.
	waitSem chan struct{}

	mu         sync.Mutex
	stream     *transport.Stream
	db         *state.DB
	self       model.Participant
	cursor     uint64
	cursorInit bool
}

func New(opts Options) *Server {
	s := &Server{opts: opts, done: make(chan struct{}), waitSem: make(chan struct{}, 1)}
	s.mcp = mcp.NewServer(&mcp.Implementation{Name: "a2a", Version: opts.Version}, nil)
	s.registerTools()
	return s
}

// Run serves MCP on stdio until ctx is cancelled or the client closes stdin.
func (s *Server) Run(ctx context.Context) error {
	return s.mcp.Run(ctx, &mcp.StdioTransport{})
}

// Shutdown signals blocking handlers to return. It is safe to call more than
// once and from any goroutine; it does not close the daemon connection (that
// is Close's job, on the goroutine that owns Run).
func (s *Server) Shutdown() {
	s.doneOnce.Do(func() { close(s.done) })
}

// Connect attaches the server to an arbitrary transport (tests use the SDK's
// in-memory transport pair).
func (s *Server) Connect(ctx context.Context, t mcp.Transport) (*mcp.ServerSession, error) {
	return s.mcp.Connect(ctx, t, nil)
}

// ensure lazily connects to the daemon, registers the participant, and
// initialises the wait cursor to the current stream tail. The MCP server
// starts before the daemon may be up, so this runs on each tool call and is
// a no-op once connected. It returns the connection snapshot taken under the
// lock: handlers must use those locals rather than the fields, which Close
// may nil concurrently.
func (s *Server) ensure(ctx context.Context) (*transport.Stream, *state.DB, model.Participant, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	reconnected := false
	if s.stream != nil {
		if s.stream.NATSConn().IsConnected() {
			return s.stream, s.db, s.self, nil
		}
		// The daemon restarted: the embedded NATS server binds a fresh random
		// port each start, so the client's built-in reconnect can never reach
		// it. Drop the dead handles and re-read daemon.url below.
		s.stream.Close()
		s.db.Close()
		s.stream, s.db = nil, nil
		reconnected = true
	}

	url, err := s.opts.DaemonURL()
	if err != nil {
		return nil, nil, model.Participant{}, fmt.Errorf("%s (%w)", daemonRemedy, err)
	}
	stream, err := transport.NewStreamWithTimeout(url, 2*time.Second)
	if err != nil {
		return nil, nil, model.Participant{}, fmt.Errorf("%s (%w)", daemonRemedy, err)
	}

	dataDir, err := s.opts.DataDir()
	if err != nil {
		stream.Close()
		return nil, nil, model.Participant{}, err
	}
	db, err := state.Open(filepath.Join(dataDir, "state.db"))
	if err != nil {
		stream.Close()
		return nil, nil, model.Participant{}, fmt.Errorf("state: %w", err)
	}

	// A row with provider/model set was registered by the daemon for a
	// configured agent — possibly one removed from config since, which the
	// startup config check cannot see. Adopting it would share its participant
	// ID (a re-added agent would skip this client's messages as its own) and
	// the get-or-create update below would blank its provider/model. Rows this
	// server registers have both fields empty, so its own previous sessions
	// are still adopted.
	if existing, lookupErr := db.GetParticipantByName(s.opts.Name); lookupErr == nil &&
		existing.Kind == model.KindAgent && (existing.Provider != "" || existing.Model != "") {
		stream.Close()
		db.Close()
		return nil, nil, model.Participant{}, fmt.Errorf(
			"participant %q belongs to a daemon agent (provider %s); pick a different --name", s.opts.Name, existing.Provider)
	}

	self, err := db.RegisterParticipant(model.Participant{Name: s.opts.Name, Kind: model.KindAgent})
	if err != nil {
		stream.Close()
		db.Close()
		return nil, nil, model.Participant{}, fmt.Errorf("register participant: %w", err)
	}

	// Cursor starts at the stream tail: wait_for_messages never replays
	// history from before this MCP session connected. On a reconnect the
	// cursor is preserved — the JetStream files survive a daemon restart, so
	// resetting to the tail would silently skip anything published while the
	// client was disconnected. One exception: sequences never shrink on a
	// surviving stream, so a preserved cursor beyond the new tail proves the
	// stream was recreated (wiped data dir); snapping back to the tail turns
	// a permanently wedged wait into a fresh session view.
	if !s.cursorInit || reconnected {
		entries, err := stream.TailWithSeq(ctx, 1)
		if err != nil {
			stream.Close()
			db.Close()
			return nil, nil, model.Participant{}, fmt.Errorf("stream tail: %w", err)
		}
		newest := uint64(0)
		if len(entries) > 0 {
			newest = entries[len(entries)-1].Seq
		}
		if !s.cursorInit || s.cursor > newest {
			s.cursor = newest
		}
		s.cursorInit = true
	}

	s.stream, s.db, s.self = stream, db, self
	return stream, db, self, nil
}

func (s *Server) cursorValue() uint64 {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.cursor
}

// advanceCursor moves the cursor forward only — never backward.
func (s *Server) advanceCursor(seq uint64) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if seq > s.cursor {
		s.cursor = seq
	}
}

// advanceCursorIfContiguous advances the cursor to newest only when the
// scanned window starts at or before the next unseen message — a window that
// skipped earlier entries would consume messages never delivered.
func (s *Server) advanceCursorIfContiguous(windowStart, newest uint64) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if windowStart <= s.cursor+1 && newest > s.cursor {
		s.cursor = newest
	}
}

// renderContent applies the redaction table to a message, matching the
// behaviour of `a2a watch` and `a2a history`.
func renderContent(db *state.DB, m model.Message) (string, error) {
	reason, redacted, err := db.RedactionReason(m.ID)
	if err != nil {
		return "", fmt.Errorf("redaction lookup: %w", err)
	}
	if redacted {
		return fmt.Sprintf("[redacted: %s]", reason), nil
	}
	return m.Content, nil
}

// Close deliberately races any handler that Shutdown just released: such a
// handler still holds its own snapshot of these handles (see ensure), so it
// never sees the nil'd fields — at worst it reads a closed stream/db, gets an
// error, and returns it to an already-closed session where it is discarded.
// The cursor only advances after a successful render, so a call that dies
// this way consumes nothing.
func (s *Server) Close() {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.stream != nil {
		s.stream.Close()
		s.stream = nil
	}
	if s.db != nil {
		s.db.Close()
		s.db = nil
	}
}
