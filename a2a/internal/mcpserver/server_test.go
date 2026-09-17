package mcpserver_test

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/modelcontextprotocol/go-sdk/mcp"
	"github.com/nats-io/nats.go"
	"github.com/veridian69/cairn/a2a/internal/mcpserver"
	"github.com/veridian69/cairn/a2a/internal/model"
	"github.com/veridian69/cairn/a2a/internal/state"
	"github.com/veridian69/cairn/a2a/internal/transport"
)

// agentStatusWire mirrors the JSON shape served on a2a.status (daemon.AgentStatus).
type agentStatusWire struct {
	Name           string  `json:"name"`
	Provider       string  `json:"provider"`
	Model          string  `json:"model"`
	Active         bool    `json:"active"`
	State          string  `json:"state"`
	Responsiveness float64 `json:"responsiveness"`
	QueueDepth     int     `json:"queue_depth"`
	LastSeenSeq    uint64  `json:"last_seen_seq"`
	HourlyCount    int     `json:"hourly_count"`
}

type harness struct {
	url     string
	dataDir string
	stream  *transport.Stream // direct handle for seeding and verification
	db      *state.DB         // direct handle for verification and redactions
	srv     *mcpserver.Server
	session *mcp.ClientSession
}

// newHarness starts an embedded NATS server with the A2A stream, opens a
// direct stream/db handle for the test, builds an mcpserver named "codex",
// and connects an MCP client over in-memory transports.
func newHarness(t *testing.T) *harness {
	t.Helper()
	dataDir := t.TempDir()

	ns, err := transport.NewServer(filepath.Join(dataDir, "jetstream"))
	if err != nil {
		t.Fatalf("nats server: %v", err)
	}
	t.Cleanup(ns.Stop)

	stream, err := transport.NewManagedStream(ns.ClientURL(), transport.StreamOptions{})
	if err != nil {
		t.Fatalf("stream: %v", err)
	}
	t.Cleanup(stream.Close)

	db, err := state.Open(filepath.Join(dataDir, "state.db"))
	if err != nil {
		t.Fatalf("state: %v", err)
	}
	t.Cleanup(func() { db.Close() })

	srv := mcpserver.New(mcpserver.Options{
		Name:      "codex",
		Version:   "test",
		DaemonURL: func() (string, error) { return ns.ClientURL(), nil },
		DataDir:   func() (string, error) { return dataDir, nil },
	})
	t.Cleanup(srv.Close)

	serverTr, clientTr := mcp.NewInMemoryTransports()
	ctx := context.Background()
	if _, err := srv.Connect(ctx, serverTr); err != nil {
		t.Fatalf("server connect: %v", err)
	}
	client := mcp.NewClient(&mcp.Implementation{Name: "test-client", Version: "0"}, nil)
	session, err := client.Connect(ctx, clientTr, nil)
	if err != nil {
		t.Fatalf("client connect: %v", err)
	}
	t.Cleanup(func() { session.Close() })

	return &harness{url: ns.ClientURL(), dataDir: dataDir, stream: stream, db: db, srv: srv, session: session}
}

// callTool invokes a tool and decodes its structured content into out (if non-nil).
func callTool(t *testing.T, session *mcp.ClientSession, name string, args map[string]any, out any) *mcp.CallToolResult {
	t.Helper()
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()
	res, err := session.CallTool(ctx, &mcp.CallToolParams{Name: name, Arguments: args})
	if err != nil {
		t.Fatalf("CallTool %s: %v", name, err)
	}
	if out != nil && !res.IsError {
		raw, err := json.Marshal(res.StructuredContent)
		if err != nil {
			t.Fatalf("marshal structured content: %v", err)
		}
		if err := json.Unmarshal(raw, out); err != nil {
			t.Fatalf("unmarshal structured content: %v", err)
		}
	}
	return res
}

func textOf(t *testing.T, res *mcp.CallToolResult) string {
	t.Helper()
	var b strings.Builder
	for _, c := range res.Content {
		if tc, ok := c.(*mcp.TextContent); ok {
			b.WriteString(tc.Text)
		}
	}
	return b.String()
}

// restartHarness is newHarness with a swappable daemon URL and restartable
// NATS server, simulating `a2a stop && a2a start`: a fresh random port, same
// JetStream files and state.db.
type restartHarness struct {
	dataDir string
	jsDir   string
	ns      *transport.Server
	stream  *transport.Stream // direct handle for seeding and verification
	db      *state.DB
	session *mcp.ClientSession

	mu  sync.Mutex
	url string
}

func newRestartHarness(t *testing.T) *restartHarness {
	t.Helper()
	dataDir := t.TempDir()
	h := &restartHarness{dataDir: dataDir, jsDir: filepath.Join(dataDir, "jetstream")}

	ns, err := transport.NewServer(h.jsDir)
	if err != nil {
		t.Fatalf("nats server: %v", err)
	}
	stream, err := transport.NewManagedStream(ns.ClientURL(), transport.StreamOptions{})
	if err != nil {
		t.Fatalf("stream: %v", err)
	}
	h.ns, h.stream, h.url = ns, stream, ns.ClientURL()
	t.Cleanup(func() {
		h.stream.Close()
		h.ns.Stop()
	})

	db, err := state.Open(filepath.Join(dataDir, "state.db"))
	if err != nil {
		t.Fatalf("state: %v", err)
	}
	h.db = db
	t.Cleanup(func() { db.Close() })

	srv := mcpserver.New(mcpserver.Options{
		Name:    "codex",
		Version: "test",
		DaemonURL: func() (string, error) {
			h.mu.Lock()
			defer h.mu.Unlock()
			return h.url, nil
		},
		DataDir: func() (string, error) { return dataDir, nil },
	})
	t.Cleanup(srv.Close)

	serverTr, clientTr := mcp.NewInMemoryTransports()
	ctx := context.Background()
	if _, err := srv.Connect(ctx, serverTr); err != nil {
		t.Fatalf("server connect: %v", err)
	}
	client := mcp.NewClient(&mcp.Implementation{Name: "test-client", Version: "0"}, nil)
	session, err := client.Connect(ctx, clientTr, nil)
	if err != nil {
		t.Fatalf("client connect: %v", err)
	}
	h.session = session
	t.Cleanup(func() { session.Close() })

	return h
}

// restartDaemon stops the embedded NATS server and starts a new one on a new
// random port over the same JetStream files, updating the advertised URL.
func (h *restartHarness) restartDaemon(t *testing.T) {
	t.Helper()
	h.stream.Close()
	h.ns.Stop()

	ns, err := transport.NewServer(h.jsDir)
	if err != nil {
		t.Fatalf("restart nats server: %v", err)
	}
	stream, err := transport.NewManagedStream(ns.ClientURL(), transport.StreamOptions{})
	if err != nil {
		t.Fatalf("restart stream: %v", err)
	}
	h.ns, h.stream = ns, stream
	h.mu.Lock()
	h.url = ns.ClientURL()
	h.mu.Unlock()

	// Give the MCP server's old NATS client a moment to notice the drop.
	time.Sleep(500 * time.Millisecond)
}

func TestToolCallsRecoverAfterDaemonRestart(t *testing.T) {
	h := newRestartHarness(t)

	res := callTool(t, h.session, "send_message", map[string]any{"content": "before restart"}, nil)
	if res.IsError {
		t.Fatalf("send before restart failed: %s", textOf(t, res))
	}

	h.restartDaemon(t)

	var out struct {
		MessageID string `json:"message_id"`
	}
	res = callTool(t, h.session, "send_message", map[string]any{"content": "after restart"}, &out)
	if res.IsError {
		t.Fatalf("send after daemon restart failed: %s", textOf(t, res))
	}
	if out.MessageID == "" {
		t.Fatal("expected a message_id after restart")
	}

	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	msgs, err := h.stream.Tail(ctx, 10)
	if err != nil {
		t.Fatalf("tail: %v", err)
	}
	if len(msgs) != 2 || msgs[1].Content != "after restart" {
		t.Fatalf("expected both messages on the restarted stream, got %+v", msgs)
	}
}

func TestWaitCursorSurvivesDaemonRestart(t *testing.T) {
	h := newRestartHarness(t)

	// First tool call connects and pins the cursor at the current tail (seq 1).
	callTool(t, h.session, "send_message", map[string]any{"content": "pin cursor"}, nil)

	other, err := h.db.RegisterParticipant(model.Participant{Name: "claude", Kind: model.KindAgent})
	if err != nil {
		t.Fatalf("register: %v", err)
	}

	h.restartDaemon(t)

	// Published after the restart but before the client's next tool call: the
	// reconnect must not re-initialise the cursor to the new tail, or this
	// message would be silently skipped.
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	if err := h.stream.Publish(ctx, model.NewMessage(other, "while you were away", nil)); err != nil {
		t.Fatalf("publish: %v", err)
	}

	var out struct {
		Messages []struct {
			Author  string `json:"author"`
			Content string `json:"content"`
		} `json:"messages"`
	}
	res := callTool(t, h.session, "wait_for_messages", map[string]any{"timeout_seconds": 5}, &out)
	if res.IsError {
		t.Fatalf("wait after daemon restart failed: %s", textOf(t, res))
	}
	if len(out.Messages) != 1 || out.Messages[0].Content != "while you were away" {
		t.Fatalf("expected the message published across the restart, got %+v", out.Messages)
	}
}

// restartDaemonWiped simulates a restart after the JetStream files were
// removed: fresh dir, stream recreated, sequences restart at 1.
func (h *restartHarness) restartDaemonWiped(t *testing.T) {
	t.Helper()
	h.jsDir = filepath.Join(h.dataDir, "jetstream-wiped")
	h.restartDaemon(t)
}

func TestWaitRecoversAfterStreamReset(t *testing.T) {
	h := newRestartHarness(t)

	// Two sends plus an unfiltered read advance the cursor to 2 — beyond the
	// fresh stream's tail after the wipe.
	callTool(t, h.session, "send_message", map[string]any{"content": "one"}, nil)
	callTool(t, h.session, "send_message", map[string]any{"content": "two"}, nil)
	callTool(t, h.session, "read_messages", map[string]any{"last": 10}, nil)

	h.restartDaemonWiped(t)

	// This reconnects; the cursor must snap back to the recreated stream's
	// tail instead of pointing past its end forever.
	callTool(t, h.session, "read_messages", map[string]any{"last": 10}, nil)

	other, err := h.db.RegisterParticipant(model.Participant{Name: "claude", Kind: model.KindAgent})
	if err != nil {
		t.Fatalf("register: %v", err)
	}
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	if err := h.stream.Publish(ctx, model.NewMessage(other, "fresh start", nil)); err != nil {
		t.Fatalf("publish: %v", err)
	}

	var out struct {
		Messages []struct {
			Content string `json:"content"`
		} `json:"messages"`
	}
	res := callTool(t, h.session, "wait_for_messages", map[string]any{"timeout_seconds": 5}, &out)
	if res.IsError {
		t.Fatalf("wait after stream reset failed: %s", textOf(t, res))
	}
	if len(out.Messages) != 1 || out.Messages[0].Content != "fresh start" {
		t.Fatalf("cursor still points past the recreated stream: %+v", out.Messages)
	}
}

func TestNameOwnedByDaemonAgentIsRefused(t *testing.T) {
	h := newHarness(t)

	// A daemon agent's row: registered with provider/model set. The daemon
	// agent may have been removed from config since (so the startup config
	// check passes), but adopting its participant ID would make a re-added
	// agent skip the MCP client's messages as its own.
	if _, err := h.db.RegisterParticipant(model.Participant{Name: "codex", Kind: model.KindAgent, Provider: "openai", Model: "gpt-5"}); err != nil {
		t.Fatalf("register: %v", err)
	}

	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	res, err := h.session.CallTool(ctx, &mcp.CallToolParams{Name: "send_message", Arguments: map[string]any{"content": "hi"}})
	if err != nil {
		t.Fatalf("CallTool: %v", err)
	}
	if !res.IsError {
		t.Fatal("expected tool error for a name owned by a daemon agent")
	}
	if !strings.Contains(textOf(t, res), "--name") {
		t.Fatalf("error should tell the caller to pick a different --name, got: %s", textOf(t, res))
	}

	// The refusal must also leave the daemon agent's row untouched — the
	// get-or-create path would have blanked provider/model.
	p, err := h.db.GetParticipantByName("codex")
	if err != nil {
		t.Fatalf("participant: %v", err)
	}
	if p.Provider != "openai" || p.Model != "gpt-5" {
		t.Fatalf("daemon agent row was modified: %+v", p)
	}
}

func TestNameOwnedByHumanIsRefused(t *testing.T) {
	h := newHarness(t)

	if _, err := h.db.RegisterParticipant(model.Participant{Name: "codex", Kind: model.KindHuman}); err != nil {
		t.Fatalf("register: %v", err)
	}

	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	res, err := h.session.CallTool(ctx, &mcp.CallToolParams{Name: "status"})
	if err != nil {
		t.Fatalf("CallTool: %v", err)
	}
	if !res.IsError {
		t.Fatal("expected tool error for a name owned by a human participant")
	}
}

func TestStatusToolReportsAgents(t *testing.T) {
	h := newHarness(t)

	// Fake the daemon's a2a.status responder on the same NATS server.
	nc, err := nats.Connect(h.url)
	if err != nil {
		t.Fatalf("nats connect: %v", err)
	}
	defer nc.Close()
	sub, err := nc.Subscribe("a2a.status", func(m *nats.Msg) {
		payload, _ := json.Marshal([]agentStatusWire{{Name: "claude", State: "active", Active: true, Provider: "anthropic", Model: "claude-sonnet-4-6"}})
		m.Respond(payload)
	})
	if err != nil {
		t.Fatalf("subscribe: %v", err)
	}
	defer sub.Unsubscribe()

	var out struct {
		Agents []agentStatusWire `json:"agents"`
	}
	res := callTool(t, h.session, "status", nil, &out)
	if res.IsError {
		t.Fatalf("status returned tool error: %s", textOf(t, res))
	}
	if len(out.Agents) != 1 || out.Agents[0].Name != "claude" {
		t.Fatalf("unexpected agents: %+v", out.Agents)
	}
}

func TestDaemonDownReturnsRemedyError(t *testing.T) {
	srv := mcpserver.New(mcpserver.Options{
		Name:      "codex",
		Version:   "test",
		DaemonURL: func() (string, error) { return "", errors.New("daemon not running (no daemon.url)") },
		DataDir:   func() (string, error) { return t.TempDir(), nil },
	})
	t.Cleanup(srv.Close)

	serverTr, clientTr := mcp.NewInMemoryTransports()
	ctx := context.Background()
	if _, err := srv.Connect(ctx, serverTr); err != nil {
		t.Fatalf("server connect: %v", err)
	}
	client := mcp.NewClient(&mcp.Implementation{Name: "test-client", Version: "0"}, nil)
	session, err := client.Connect(ctx, clientTr, nil)
	if err != nil {
		t.Fatalf("client connect: %v", err)
	}
	t.Cleanup(func() { session.Close() })

	res, err := session.CallTool(ctx, &mcp.CallToolParams{Name: "status"})
	if err != nil {
		t.Fatalf("CallTool: %v", err)
	}
	if !res.IsError {
		t.Fatal("expected tool error when daemon is down")
	}
	if !strings.Contains(textOf(t, res), "a2a start") {
		t.Fatalf("error should name the remedy, got: %s", textOf(t, res))
	}
}

func TestSendMessagePublishesAsParticipant(t *testing.T) {
	h := newHarness(t)

	var out struct {
		MessageID string `json:"message_id"`
	}
	res := callTool(t, h.session, "send_message", map[string]any{"content": "hello from codex"}, &out)
	if res.IsError {
		t.Fatalf("send_message returned tool error: %s", textOf(t, res))
	}
	if out.MessageID == "" {
		t.Fatal("expected a message_id")
	}

	// The participant was registered as kind agent.
	p, err := h.db.GetParticipantByName("codex")
	if err != nil {
		t.Fatalf("participant not registered: %v", err)
	}
	if p.Kind != "agent" {
		t.Fatalf("expected kind agent, got %q", p.Kind)
	}

	// The message is on the stream, authored by that participant.
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	msgs, err := h.stream.Tail(ctx, 10)
	if err != nil {
		t.Fatalf("tail: %v", err)
	}
	if len(msgs) != 1 {
		t.Fatalf("expected 1 message, got %d", len(msgs))
	}
	if msgs[0].ID != out.MessageID || msgs[0].AuthorID != p.ID || msgs[0].Content != "hello from codex" {
		t.Fatalf("unexpected message: %+v", msgs[0])
	}
}

func TestSendMessageWithReplyTo(t *testing.T) {
	h := newHarness(t)

	var first struct {
		MessageID string `json:"message_id"`
	}
	callTool(t, h.session, "send_message", map[string]any{"content": "root"}, &first)

	var second struct {
		MessageID string `json:"message_id"`
	}
	res := callTool(t, h.session, "send_message", map[string]any{"content": "child", "reply_to": first.MessageID}, &second)
	if res.IsError {
		t.Fatalf("send_message returned tool error: %s", textOf(t, res))
	}

	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	msgs, err := h.stream.Tail(ctx, 10)
	if err != nil {
		t.Fatalf("tail: %v", err)
	}
	if len(msgs) != 2 {
		t.Fatalf("expected 2 messages, got %d", len(msgs))
	}
	child := msgs[1]
	if child.ReplyTo == nil || *child.ReplyTo != first.MessageID {
		t.Fatalf("expected reply_to %q, got %+v", first.MessageID, child.ReplyTo)
	}
}

func TestSendMessageRequiresContent(t *testing.T) {
	h := newHarness(t)
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	res, err := h.session.CallTool(ctx, &mcp.CallToolParams{Name: "send_message", Arguments: map[string]any{"content": ""}})
	if err != nil {
		t.Fatalf("CallTool: %v", err)
	}
	if !res.IsError {
		t.Fatal("expected tool error for empty content")
	}
}

func TestReadMessagesReturnsRecent(t *testing.T) {
	h := newHarness(t)

	callTool(t, h.session, "send_message", map[string]any{"content": "one"}, nil)
	callTool(t, h.session, "send_message", map[string]any{"content": "two"}, nil)

	var out struct {
		Messages []struct {
			ID        string `json:"id"`
			Author    string `json:"author"`
			Content   string `json:"content"`
			ReplyTo   string `json:"reply_to"`
			CreatedAt string `json:"created_at"`
		} `json:"messages"`
	}
	res := callTool(t, h.session, "read_messages", map[string]any{"last": 10}, &out)
	if res.IsError {
		t.Fatalf("read_messages returned tool error: %s", textOf(t, res))
	}
	if len(out.Messages) != 2 {
		t.Fatalf("expected 2 messages, got %d", len(out.Messages))
	}
	if out.Messages[0].Content != "one" || out.Messages[1].Content != "two" {
		t.Fatalf("wrong order/content: %+v", out.Messages)
	}
	if out.Messages[0].Author != "codex" {
		t.Fatalf("expected author codex, got %q", out.Messages[0].Author)
	}
}

func TestReadMessagesFiltersByAuthor(t *testing.T) {
	h := newHarness(t)

	callTool(t, h.session, "send_message", map[string]any{"content": "mine"}, nil)

	// Seed a message from another participant directly on the stream.
	other, err := h.db.RegisterParticipant(model.Participant{Name: "claude", Kind: model.KindAgent})
	if err != nil {
		t.Fatalf("register: %v", err)
	}
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	if err := h.stream.Publish(ctx, model.NewMessage(other, "theirs", nil)); err != nil {
		t.Fatalf("publish: %v", err)
	}

	var out struct {
		Messages []struct {
			Author  string `json:"author"`
			Content string `json:"content"`
		} `json:"messages"`
	}
	callTool(t, h.session, "read_messages", map[string]any{"last": 10, "by": "claude"}, &out)
	if len(out.Messages) != 1 || out.Messages[0].Content != "theirs" {
		t.Fatalf("expected only claude's message, got %+v", out.Messages)
	}
}

func TestReadMessagesRendersRedactions(t *testing.T) {
	h := newHarness(t)

	var sent struct {
		MessageID string `json:"message_id"`
	}
	callTool(t, h.session, "send_message", map[string]any{"content": "secret"}, &sent)

	if err := h.db.Redact(sent.MessageID, "operator-request", "test"); err != nil {
		t.Fatalf("redact: %v", err)
	}

	var out struct {
		Messages []struct {
			Content string `json:"content"`
		} `json:"messages"`
	}
	callTool(t, h.session, "read_messages", map[string]any{"last": 10}, &out)
	if len(out.Messages) != 1 {
		t.Fatalf("expected 1 message, got %d", len(out.Messages))
	}
	if out.Messages[0].Content != "[redacted: operator-request]" {
		t.Fatalf("expected redaction placeholder, got %q", out.Messages[0].Content)
	}
}

func TestReadMessagesCapsLastAt500(t *testing.T) {
	h := newHarness(t)

	other, err := h.db.RegisterParticipant(model.Participant{Name: "claude", Kind: model.KindAgent})
	if err != nil {
		t.Fatalf("register: %v", err)
	}
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()
	for i := 0; i < 510; i++ {
		if err := h.stream.Publish(ctx, model.NewMessage(other, fmt.Sprintf("m%d", i), nil)); err != nil {
			t.Fatalf("publish %d: %v", i, err)
		}
	}

	var out struct {
		Messages []struct {
			Content string `json:"content"`
		} `json:"messages"`
	}
	res := callTool(t, h.session, "read_messages", map[string]any{"last": 10000}, &out)
	if res.IsError {
		t.Fatalf("read_messages returned tool error: %s", textOf(t, res))
	}
	if len(out.Messages) != 500 {
		t.Fatalf("expected the cap of 500 messages, got %d", len(out.Messages))
	}
	if out.Messages[0].Content != "m10" || out.Messages[499].Content != "m509" {
		t.Fatalf("expected the newest 500 (m10..m509), got %q..%q", out.Messages[0].Content, out.Messages[499].Content)
	}
}

func TestWaitForMessagesReceivesOthersAndExcludesOwn(t *testing.T) {
	h := newHarness(t)

	// Own message published first: must NOT satisfy the wait.
	callTool(t, h.session, "send_message", map[string]any{"content": "my own"}, nil)

	other, err := h.db.RegisterParticipant(model.Participant{Name: "claude", Kind: model.KindAgent})
	if err != nil {
		t.Fatalf("register: %v", err)
	}

	done := make(chan struct{})
	var out struct {
		Messages []struct {
			Author  string `json:"author"`
			Content string `json:"content"`
		} `json:"messages"`
	}
	go func() {
		defer close(done)
		callTool(t, h.session, "wait_for_messages", map[string]any{"timeout_seconds": 30}, &out)
	}()

	// Give the wait a moment to start, then publish from the other identity.
	time.Sleep(500 * time.Millisecond)
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	if err := h.stream.Publish(ctx, model.NewMessage(other, "reply for codex", nil)); err != nil {
		t.Fatalf("publish: %v", err)
	}

	select {
	case <-done:
	case <-time.After(25 * time.Second):
		t.Fatal("wait_for_messages did not return after a new message")
	}
	if len(out.Messages) != 1 || out.Messages[0].Author != "claude" || out.Messages[0].Content != "reply for codex" {
		t.Fatalf("unexpected wait result: %+v", out.Messages)
	}
}

func TestWaitForMessagesTimesOutEmpty(t *testing.T) {
	h := newHarness(t)

	start := time.Now()
	var out struct {
		Messages []struct{} `json:"messages"`
	}
	res := callTool(t, h.session, "wait_for_messages", map[string]any{"timeout_seconds": 1}, &out)
	if res.IsError {
		t.Fatalf("timeout must be empty success, got tool error: %s", textOf(t, res))
	}
	if len(out.Messages) != 0 {
		t.Fatalf("expected no messages, got %d", len(out.Messages))
	}
	// The tool description promises the timeout as a bound; allow modest
	// scheduling slack but not a whole extra polling cycle.
	if elapsed := time.Since(start); elapsed > 1900*time.Millisecond {
		t.Fatalf("wait overshot its 1s timeout: %v", elapsed)
	}
}

func TestConcurrentWaitsDoNotDoubleDeliver(t *testing.T) {
	h := newHarness(t)

	// Pin the connection and cursor before publishing.
	callTool(t, h.session, "read_messages", map[string]any{"last": 10}, nil)

	other, err := h.db.RegisterParticipant(model.Participant{Name: "claude", Kind: model.KindAgent})
	if err != nil {
		t.Fatalf("register: %v", err)
	}

	type waitOut struct {
		Messages []struct {
			Content string `json:"content"`
		} `json:"messages"`
	}
	results := make([]waitOut, 2)
	var wg sync.WaitGroup
	for i := range results {
		wg.Add(1)
		go func(i int) {
			defer wg.Done()
			ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
			defer cancel()
			res, err := h.session.CallTool(ctx, &mcp.CallToolParams{Name: "wait_for_messages", Arguments: map[string]any{"timeout_seconds": 4}})
			if err != nil {
				t.Errorf("wait %d: %v", i, err)
				return
			}
			if res.IsError {
				t.Errorf("wait %d tool error: %s", i, textOf(t, res))
				return
			}
			raw, err := json.Marshal(res.StructuredContent)
			if err != nil {
				t.Errorf("wait %d: marshal: %v", i, err)
				return
			}
			if err := json.Unmarshal(raw, &results[i]); err != nil {
				t.Errorf("wait %d: unmarshal: %v", i, err)
			}
		}(i)
	}

	// Let both waits start, then publish exactly one foreign message.
	time.Sleep(500 * time.Millisecond)
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	if err := h.stream.Publish(ctx, model.NewMessage(other, "just once", nil)); err != nil {
		t.Fatalf("publish: %v", err)
	}
	wg.Wait()

	total := len(results[0].Messages) + len(results[1].Messages)
	if total != 1 {
		t.Fatalf("one message delivered %d times across concurrent waits, want exactly once: %+v", total, results)
	}
}

func TestWaitDeliversBacklogLargerThanBufferExactlyOnce(t *testing.T) {
	// A single-entry buffer makes every delivery hit the buffer-full path —
	// at the default 128 the drain loop usually keeps pace and the overflow
	// handling goes untested.
	defer mcpserver.SetWaitBufferSizeForTest(1)()

	h := newHarness(t)

	// Pin the connection and cursor at the (empty) tail.
	callTool(t, h.session, "read_messages", map[string]any{"last": 10}, nil)

	other, err := h.db.RegisterParticipant(model.Participant{Name: "claude", Kind: model.KindAgent})
	if err != nil {
		t.Fatalf("register: %v", err)
	}

	// Well past the buffer, so the subscription must apply backpressure
	// instead of dropping the overflow.
	const backlog = 300
	ctx, cancel := context.WithTimeout(context.Background(), 60*time.Second)
	defer cancel()
	for i := 0; i < backlog; i++ {
		if err := h.stream.Publish(ctx, model.NewMessage(other, fmt.Sprintf("m%d", i), nil)); err != nil {
			t.Fatalf("publish %d: %v", i, err)
		}
	}

	// Drain via successive waits until one comes back empty. The union must
	// contain every message exactly once — a lost or doubled entry violates
	// the cursor invariant.
	seen := map[string]int{}
	for calls := 0; ; calls++ {
		if calls > backlog {
			t.Fatalf("waits did not drain the backlog: %d of %d delivered", len(seen), backlog)
		}
		var out struct {
			Messages []struct {
				Content string `json:"content"`
			} `json:"messages"`
		}
		res := callTool(t, h.session, "wait_for_messages", map[string]any{"timeout_seconds": 2}, &out)
		if res.IsError {
			t.Fatalf("wait returned tool error: %s", textOf(t, res))
		}
		if len(out.Messages) == 0 {
			break
		}
		for _, m := range out.Messages {
			seen[m.Content]++
		}
	}
	if len(seen) != backlog {
		t.Fatalf("delivered %d distinct messages, want %d", len(seen), backlog)
	}
	for content, n := range seen {
		if n != 1 {
			t.Fatalf("%q delivered %d times, want exactly once", content, n)
		}
	}
}

func TestQueuedWaitRecoversAfterDaemonRestart(t *testing.T) {
	h := newRestartHarness(t)

	// Pin the connection and cursor at the (empty) tail.
	callTool(t, h.session, "read_messages", map[string]any{"last": 10}, nil)

	// Wait A holds the semaphore for its whole 4s window (its subscription
	// dies with the restart, so nothing releases it early).
	var wg sync.WaitGroup
	wg.Add(1)
	go func() {
		defer wg.Done()
		callTool(t, h.session, "wait_for_messages", map[string]any{"timeout_seconds": 4}, nil)
	}()
	time.Sleep(300 * time.Millisecond)

	// Wait B queues behind A with the pre-restart connection snapshot.
	type waitOut struct {
		Messages []struct {
			Content string `json:"content"`
		} `json:"messages"`
	}
	var out waitOut
	var bErr string
	wg.Add(1)
	go func() {
		defer wg.Done()
		res := callTool(t, h.session, "wait_for_messages", map[string]any{"timeout_seconds": 30}, &out)
		if res.IsError {
			bErr = textOf(t, res)
		}
	}()
	time.Sleep(300 * time.Millisecond)

	h.restartDaemon(t)

	other, err := h.db.RegisterParticipant(model.Participant{Name: "claude", Kind: model.KindAgent})
	if err != nil {
		t.Fatalf("register: %v", err)
	}
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	if err := h.stream.Publish(ctx, model.NewMessage(other, "after restart", nil)); err != nil {
		t.Fatalf("publish: %v", err)
	}

	wg.Wait()
	if bErr != "" {
		t.Fatalf("queued wait failed instead of reconnecting: %s", bErr)
	}
	if len(out.Messages) != 1 || out.Messages[0].Content != "after restart" {
		t.Fatalf("queued wait should deliver the post-restart message, got %+v", out.Messages)
	}
}

func TestWaitDoesNotRedeliverReadMessages(t *testing.T) {
	h := newHarness(t)

	// Pin the connection first: the cursor initialises at the (empty) tail,
	// so the message below lands beyond it and only read_messages' advance
	// keeps the wait from re-delivering it.
	callTool(t, h.session, "read_messages", map[string]any{"last": 10}, nil)

	other, err := h.db.RegisterParticipant(model.Participant{Name: "claude", Kind: model.KindAgent})
	if err != nil {
		t.Fatalf("register: %v", err)
	}
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	if err := h.stream.Publish(ctx, model.NewMessage(other, "already seen", nil)); err != nil {
		t.Fatalf("publish: %v", err)
	}

	// read_messages sees it and advances the cursor past it...
	var read struct {
		Messages []struct {
			Content string `json:"content"`
		} `json:"messages"`
	}
	callTool(t, h.session, "read_messages", map[string]any{"last": 10}, &read)
	if len(read.Messages) != 1 {
		t.Fatalf("expected 1 read message, got %d", len(read.Messages))
	}

	// ...so a short wait must come back empty, not re-deliver it.
	var out struct {
		Messages []struct{} `json:"messages"`
	}
	res := callTool(t, h.session, "wait_for_messages", map[string]any{"timeout_seconds": 1}, &out)
	if res.IsError {
		t.Fatalf("unexpected tool error: %s", textOf(t, res))
	}
	if len(out.Messages) != 0 {
		t.Fatalf("message re-delivered after read_messages: %+v", out.Messages)
	}
}
