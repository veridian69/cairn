package mcpserver

import (
	"context"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/veridian69/cairn/a2a/internal/model"
	"github.com/veridian69/cairn/a2a/internal/transport"
)

// newTestServer starts an embedded NATS server on a temp dir, builds an
// mcpserver named "codex" against it, and connects it (ensure) so the cursor
// is pinned at the current — empty — stream tail.
func newTestServer(t *testing.T) (*Server, context.Context) {
	t.Helper()
	dataDir := t.TempDir()

	ns, err := transport.NewServer(filepath.Join(dataDir, "jetstream"))
	if err != nil {
		t.Fatalf("nats server: %v", err)
	}
	t.Cleanup(ns.Stop)

	srv := New(Options{
		Name:      "codex",
		Version:   "test",
		DaemonURL: func() (string, error) { return ns.ClientURL(), nil },
		DataDir:   func() (string, error) { return dataDir, nil },
	})
	t.Cleanup(srv.Close)

	ctx, cancel := context.WithTimeout(context.Background(), 60*time.Second)
	t.Cleanup(cancel)
	if _, _, _, err := srv.ensure(ctx); err != nil {
		t.Fatalf("ensure: %v", err)
	}
	return srv, ctx
}

// TestReadDoesNotAdvanceCursorPastUnreturnedBacklog is a regression test for a
// bug where read_messages advanced the cursor to the newest message of the
// window it returned, even when more messages had arrived than `last` — the
// older entries were then returned by neither read_messages nor
// wait_for_messages, and were lost permanently.
func TestReadDoesNotAdvanceCursorPastUnreturnedBacklog(t *testing.T) {
	srv, ctx := newTestServer(t)

	claude, err := srv.db.RegisterParticipant(model.Participant{Name: "claude", Kind: model.KindAgent})
	if err != nil {
		t.Fatalf("register claude: %v", err)
	}
	for _, content := range []string{"a", "b", "c", "d", "e"} {
		if err := srv.stream.Publish(ctx, model.NewMessage(claude, content, nil)); err != nil {
			t.Fatalf("publish %q: %v", content, err)
		}
	}

	before := srv.cursorValue()

	// A narrow read only sees the newest two — d and e.
	_, read, err := srv.handleRead(ctx, nil, readArgs{Last: 2})
	if err != nil {
		t.Fatalf("read: %v", err)
	}
	if len(read.Messages) != 2 || read.Messages[0].Content != "d" || read.Messages[1].Content != "e" {
		t.Fatalf("expected [d e], got %+v", read.Messages)
	}

	// a, b and c were never delivered, so the cursor must not have moved past
	// them. The window is non-contiguous with the cursor, so it must not move
	// at all.
	if got := srv.cursorValue(); got != before {
		t.Fatalf("read consumed undelivered backlog: cursor before=%d after=%d", before, got)
	}

	// And the proof that matters: wait_for_messages still delivers them. The
	// push-based wait returns as soon as anything is deliverable, so one call
	// may legitimately carry fewer than five — collect across calls, which
	// also proves nothing is lost or duplicated between them.
	var got []string
	deadline := time.Now().Add(15 * time.Second)
	for len(got) < 5 && time.Now().Before(deadline) {
		_, waited, err := srv.handleWait(ctx, nil, waitArgs{TimeoutSeconds: 2})
		if err != nil {
			t.Fatalf("wait: %v", err)
		}
		for _, m := range waited.Messages {
			got = append(got, m.Content)
		}
	}
	if strings.Join(got, "") != "abcde" {
		t.Fatalf("expected all five messages exactly once in order, got %v", got)
	}
}

// TestWaitDoesNotAdvanceCursorOnRenderFailure covers the ordering rule: an
// entry's cursor advance happens only after that entry has been rendered and
// appended, so a failed call can be retried without losing messages.
func TestWaitDoesNotAdvanceCursorOnRenderFailure(t *testing.T) {
	srv, ctx := newTestServer(t)

	claude, err := srv.db.RegisterParticipant(model.Participant{Name: "claude", Kind: model.KindAgent})
	if err != nil {
		t.Fatalf("register claude: %v", err)
	}
	if err := srv.stream.Publish(ctx, model.NewMessage(claude, "undeliverable", nil)); err != nil {
		t.Fatalf("publish: %v", err)
	}

	before := srv.cursorValue()

	// Closing the state DB the server holds makes the redaction lookup fail,
	// which is the only render error path.
	srv.db.Close()

	_, result, err := srv.handleWait(ctx, nil, waitArgs{TimeoutSeconds: 5})
	if err == nil {
		t.Fatalf("expected a render failure, got %+v", result.Messages)
	}
	if got := srv.cursorValue(); got != before {
		t.Fatalf("cursor advanced past an undelivered message: before=%d after=%d", before, got)
	}
}

// TestShutdownReleasesWait covers the SIGINT path: the stdio transport does
// not cancel handler contexts, so a blocking wait must observe the server's
// own shutdown signal or Ctrl-C hangs for up to the wait timeout while
// holding the runtime lease.
func TestShutdownReleasesWait(t *testing.T) {
	srv, ctx := newTestServer(t)

	type waitResult struct {
		res messagesResult
		err error
	}
	done := make(chan waitResult, 1)
	go func() {
		_, res, err := srv.handleWait(ctx, nil, waitArgs{TimeoutSeconds: 300})
		done <- waitResult{res, err}
	}()

	time.Sleep(200 * time.Millisecond) // let the wait loop get going
	start := time.Now()
	srv.Shutdown()

	select {
	case got := <-done:
		if elapsed := time.Since(start); elapsed > 15*time.Second {
			t.Fatalf("wait took %v to observe shutdown", elapsed)
		}
		if got.err != nil {
			t.Fatalf("shutdown must be an empty success, got error: %v", got.err)
		}
		if len(got.res.Messages) != 0 {
			t.Fatalf("expected no messages, got %+v", got.res.Messages)
		}
	case <-time.After(15 * time.Second):
		t.Fatal("wait_for_messages did not return after Shutdown")
	}
}

// TestFilteredReadDoesNotAdvanceCursor is a regression test for a bug where a
// filtered read_messages call advanced the cursor past entries the filter
// skipped, permanently hiding them from wait_for_messages. It is an
// in-package test (package mcpserver, not mcpserver_test) so it can call
// handleRead and cursorValue directly, without going through the MCP
// transport.
func TestFilteredReadDoesNotAdvanceCursor(t *testing.T) {
	srv, ctx := newTestServer(t)

	claude, err := srv.db.RegisterParticipant(model.Participant{Name: "claude", Kind: model.KindAgent})
	if err != nil {
		t.Fatalf("register claude: %v", err)
	}
	gpt, err := srv.db.RegisterParticipant(model.Participant{Name: "gpt", Kind: model.KindAgent})
	if err != nil {
		t.Fatalf("register gpt: %v", err)
	}

	publish := func(author model.Participant, content string) {
		t.Helper()
		if err := srv.stream.Publish(ctx, model.NewMessage(author, content, nil)); err != nil {
			t.Fatalf("publish %q: %v", content, err)
		}
	}
	// Interleave authors so the "claude" filter skips "b" in the middle of
	// the window.
	publish(claude, "a")
	publish(gpt, "b")
	publish(claude, "c")

	before := srv.cursorValue()

	_, filtered, err := srv.handleRead(ctx, nil, readArgs{Last: 10, By: "claude"})
	if err != nil {
		t.Fatalf("filtered read: %v", err)
	}
	if len(filtered.Messages) != 2 || filtered.Messages[0].Content != "a" || filtered.Messages[1].Content != "c" {
		t.Fatalf("expected [a c], got %+v", filtered.Messages)
	}

	if got := srv.cursorValue(); got != before {
		t.Fatalf("filtered read must not advance the cursor: before=%d after=%d", before, got)
	}

	// An unfiltered read is the catch-up path: it must still see "b" (proof
	// nothing was dropped from history) and now advances the cursor.
	_, unfiltered, err := srv.handleRead(ctx, nil, readArgs{Last: 10})
	if err != nil {
		t.Fatalf("unfiltered read: %v", err)
	}
	if len(unfiltered.Messages) != 3 || unfiltered.Messages[1].Content != "b" {
		t.Fatalf("expected skipped message %q still present in unfiltered read, got %+v", "b", unfiltered.Messages)
	}

	if got := srv.cursorValue(); got == before {
		t.Fatalf("unfiltered read should advance the cursor past %d, got %d", before, got)
	}
}
