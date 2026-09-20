package garden

import (
	"context"
	"database/sql"
	"errors"
	"net/http"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"sync"
	"time"

	"github.com/modelcontextprotocol/go-sdk/mcp"
	"github.com/veridian69/cairn/a2a/internal/gardenauth"
	"github.com/veridian69/cairn/a2a/internal/state"
	"github.com/veridian69/cairn/a2a/internal/transport"
	"golang.org/x/sys/unix"
)

// Server owns one gateway's connection, deployment lock and durable inbox state.
type Server struct {
	cfg      Config
	binding  Binding
	auth     *gardenauth.Client
	mu       sync.Mutex
	stream   *transport.Stream
	daemonID string
	db       *state.DB
	sql      *sql.DB
	lock     *os.File
	closed   bool
	done     chan struct{}
}

// New validates and enrols configured principals before any data is exposed.
func New(ctx context.Context, cfg Config) (s *Server, err error) {
	if err = cfg.validate(); err != nil {
		return nil, err
	}
	cfg.Auth.Scope.Segments = append([]gardenauth.Segment{}, cfg.Auth.Scope.Segments...)
	principals := make(map[string]string, len(cfg.Principals))
	for k, v := range cfg.Principals {
		principals[k] = v
	}
	cfg.Principals = principals
	if cfg.Version == "" {
		cfg.Version = "dev"
	}
	auth, err := gardenauth.New(cfg.Auth)
	if err != nil {
		return nil, err
	}
	s = &Server{cfg: cfg, auth: auth, binding: Binding{cfg.Auth.InstanceID, cfg.Auth.Scope, cfg.Auth.Classification}, done: make(chan struct{})}
	defer func(owned *Server) {
		if err != nil {
			_ = owned.Close()
		}
	}(s)
	// Establish the daemon's actual store before opening any caller-selected
	// database or lock. A second path must never create a parallel binding.
	if err = s.ensure(ctx); err != nil {
		return nil, err
	}

	cfg = s.cfg
	s.lock, err = os.OpenFile(filepath.Join(cfg.DataDir, "garden.lock"), os.O_CREATE|os.O_RDWR, 0600)
	if err != nil {
		return nil, err
	}
	if err = unix.Flock(int(s.lock.Fd()), unix.LOCK_EX|unix.LOCK_NB); err != nil {
		return nil, errors.New("another Garden service owns this data directory")
	}
	s.db, err = state.Open(filepath.Join(cfg.DataDir, "state.db"))
	if err != nil {
		return nil, err
	}
	s.sql, err = sql.Open("sqlite", filepath.Join(cfg.DataDir, "state.db")+"?_pragma=journal_mode(WAL)&_pragma=busy_timeout(5000)&_pragma=foreign_keys(1)&_txlock=immediate")
	if err != nil {
		return nil, err
	}
	s.sql.SetMaxOpenConns(1)
	p, err := s.stream.Position(ctx)
	if err != nil {
		return nil, err
	}
	if err = s.initStore(ctx, p.Created.UTC().Format(time.RFC3339Nano), p.Last); err != nil {
		return nil, err
	}
	return s, nil
}

// Close releases all gateway resources; it is safe to call repeatedly.
func (s *Server) Close() error {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.closed {
		return nil
	}
	s.closed = true
	close(s.done)
	if s.stream != nil {
		s.stream.Close()
	}
	if s.db != nil {
		_ = s.db.Close()
	}
	if s.sql != nil {
		_ = s.sql.Close()
	}
	if s.lock != nil {
		_ = unix.Flock(int(s.lock.Fd()), unix.LOCK_UN)
		return s.lock.Close()
	}
	return nil
}
func (s *Server) ensure(ctx context.Context) error {
	if s.closed {
		return failure("unavailable", "Garden service is closed")
	}
	if s.stream != nil && s.stream.NATSConn().IsConnected() && s.stream.NATSConn().ConnectedServerId() == s.daemonID {
		return nil
	}
	if s.stream != nil {
		s.stream.Close()
		s.stream = nil
	}
	raw, err := os.ReadFile(s.cfg.DaemonURLFile)
	if err != nil || len(raw) > 4096 {
		return failure("unavailable", "Garden daemon is unavailable")
	}
	s.stream, err = transport.NewStreamWithTimeout(strings.TrimSpace(string(raw)), 2*time.Second)
	if err != nil {
		return failure("unavailable", "Garden daemon is unavailable")
	}
	identityContext, cancel := context.WithTimeout(ctx, 2*time.Second)
	defer cancel()
	actual, err := s.stream.StorageDirectory(identityContext)
	if err != nil {
		s.stream.Close()
		s.stream = nil
		return failure("unavailable", "Garden daemon storage identity unavailable; restart the daemon")
	}
	configuredInfo, cfgErr := os.Stat(s.cfg.DataDir)
	actualInfo, actualErr := os.Stat(actual)
	if cfgErr != nil || actualErr != nil || !configuredInfo.IsDir() || !os.SameFile(configuredInfo, actualInfo) {
		s.stream.Close()
		s.stream = nil
		return failure("unavailable", "Garden data_dir does not match the daemon storage directory")
	}

	s.cfg.DataDir = actual
	s.daemonID = s.stream.NATSConn().ConnectedServerId()

	return nil
}
func (s *Server) authenticate(ctx context.Context, token string) (gardenauth.Identity, error) {
	id, err := s.auth.Authenticate(ctx, token)
	if err != nil {
		if errors.Is(err, gardenauth.ErrInvalidCredentials) || errors.Is(err, gardenauth.ErrPermissionDenied) {
			return id, failure("forbidden", "Garden authentication denied")
		}
		return id, failure("unavailable", "Garden authentication unavailable")
	}
	if !id.CanRead || s.cfg.Principals[id.PrincipalID] == "" {
		return id, failure("forbidden", "Garden access denied")
	}
	return id, nil
}

// Handler serves only authenticated stateless MCP requests at /mcp.
func (s *Server) Handler() http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/mcp" || r.URL.RawQuery != "" {
			http.NotFound(w, r)
			return
		}
		if s.cfg.PublicEndpoint != "" && !matchesPublicAuthority(s.cfg.PublicEndpoint, r.Host) {
			http.Error(w, "invalid Host header", http.StatusForbidden)
			return
		}
		if len(r.Header.Values("Origin")) != 0 {
			http.Error(w, "origin forbidden", 403)
			return
		}
		values := r.Header.Values("Authorization")
		if len(values) != 1 || !strings.HasPrefix(values[0], "Bearer ") || strings.ContainsAny(strings.TrimPrefix(values[0], "Bearer "), " ,\t\r\n") {
			http.Error(w, "authentication required", 401)
			return
		}
		token := strings.TrimPrefix(values[0], "Bearer ")
		id, err := s.authenticate(r.Context(), token)
		if err != nil {
			status := 403
			if e := err.(*Error); e.Code == "unavailable" {
				status = 503
			}
			http.Error(w, http.StatusText(status), status)
			return
		}
		r.Body = http.MaxBytesReader(w, r.Body, 128*1024)
		server := mcp.NewServer(&mcp.Implementation{Name: "garden", Version: s.cfg.Version}, nil)
		s.register(server, id, token)
		// Our explicit authority check replaces the SDK loopback heuristic only
		// for configured endpoints; Origin rejection and authentication still apply.
		h := mcp.NewStreamableHTTPHandler(func(*http.Request) *mcp.Server { return server }, &mcp.StreamableHTTPOptions{Stateless: true, JSONResponse: true, PropagateRequestCancellation: true, DisableLocalhostProtection: s.cfg.PublicEndpoint != ""})
		h.ServeHTTP(w, r)
	})
}
func register[I any](m *mcp.Server, name, description string, fn func(context.Context, I) (any, error)) {
	mcp.AddTool[I, any](m, &mcp.Tool{Name: name, Description: description}, func(ctx context.Context, _ *mcp.CallToolRequest, args I) (*mcp.CallToolResult, any, error) {
		ctx, cancel := context.WithTimeout(ctx, 35*time.Second)
		defer cancel()
		out, err := fn(ctx, args)
		if err == nil {
			return nil, out, nil
		}
		var e *Error
		if !errors.As(err, &e) {
			e = &Error{"unavailable", "Garden operation unavailable"}
		}
		return &mcp.CallToolResult{IsError: true, StructuredContent: e, Content: []mcp.Content{&mcp.TextContent{Text: e.Error()}}}, nil, nil
	})
}
func (s *Server) register(m *mcp.Server, id gardenauth.Identity, token string) {
	recheck := func(ctx context.Context, out any, err error) (any, error) {
		if err != nil {
			return nil, err
		}
		fresh, e := s.authenticate(ctx, token)
		if e != nil {
			return nil, e
		}
		if fresh.PrincipalID != id.PrincipalID {
			return nil, failure("forbidden", "Garden identity changed")
		}
		return out, nil
	}
	register(m, "status", "Describe this scoped Garden and authenticated participant.", func(ctx context.Context, _ struct{}) (any, error) {
		out, err := s.status(ctx, id.PrincipalID)
		return recheck(ctx, out, err)
	})
	register(m, "send_message", "Send shared room content; explicit recipients route attention, not private access.", func(ctx context.Context, a SendArgs) (any, error) {
		if !id.CanWrite {
			return nil, failure("forbidden", "Garden sending requires retrieve and ingest")
		}
		return s.send(ctx, id.PrincipalID, a)
	})
	register(m, "read_messages", "Read retained shared room history without advancing your inbox.", func(ctx context.Context, a ReadArgs) (any, error) {
		out, err := s.read(ctx, id.PrincipalID, a)
		return recheck(ctx, out, err)
	})
	register(m, "poll_inbox", "Lease and return your first pending addressed message. Acknowledge only after host acceptance.", func(ctx context.Context, a PollArgs) (any, error) {
		out, err := s.poll(ctx, id.PrincipalID, a)
		return recheck(ctx, out, err)
	})
	register(m, "acknowledge", "Acknowledge accepted delivery using your current adapter lease and receipt.", func(ctx context.Context, a AckArgs) (any, error) {
		err := s.ack(ctx, id.PrincipalID, a)
		return map[string]bool{"acknowledged": err == nil}, err
	})
}
func (s *Server) position(ctx context.Context, principal string) (transport.Position, inbox, error) {
	if err := s.ensure(ctx); err != nil {
		return transport.Position{}, inbox{}, err
	}
	b, err := s.loadInbox(ctx, principal)
	if err != nil {
		return transport.Position{}, b, err
	}
	p, err := s.stream.Position(ctx)
	if err != nil {
		return p, b, failure("unavailable", "Garden stream unavailable")
	}
	if p.Created.UTC().Format(time.RFC3339Nano) != b.generation {
		return p, b, failure("stream_reset", "Garden stream generation changed; operator recovery required")
	}
	return p, b, nil
}
func (s *Server) status(ctx context.Context, principal string) (StatusResult, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	_, b, err := s.position(ctx, principal)
	if err != nil {
		return StatusResult{}, err
	}
	names := make([]string, 0, len(s.cfg.Principals))
	for _, name := range s.cfg.Principals {
		names = append(names, name)
	}
	sort.Strings(names)
	return StatusResult{Binding: s.binding, Participant: b.name, Generation: b.generation, Participants: names}, nil
}
