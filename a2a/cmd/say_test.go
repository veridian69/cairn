package cmd

import (
	"context"
	"os"
	"path/filepath"
	"testing"

	"github.com/veridian69/cairn/a2a/internal/model"
	"github.com/veridian69/cairn/a2a/internal/state"
	"github.com/veridian69/cairn/a2a/internal/transport"
)

func TestSayCommandPublishesHumanMessageAndRegistersSender(t *testing.T) {
	home, stream, db := setupCommandStateAndStream(t)

	output := captureStdout(t, func() {
		if err := sayCmd.RunE(sayCmd, []string{"hello", "world"}); err != nil {
			t.Fatalf("sayCmd.RunE returned error: %v", err)
		}
	})
	if output == "" {
		t.Fatal("say command should print a confirmation line")
	}

	msgs, err := stream.Replay(context.Background(), 0, 10)
	if err != nil {
		t.Fatalf("replaying messages: %v", err)
	}
	if len(msgs) != 1 {
		t.Fatalf("expected 1 message, got %d", len(msgs))
	}
	if msgs[0].Content != "hello world" {
		t.Fatalf("message content = %q, want %q", msgs[0].Content, "hello world")
	}
	if msgs[0].AuthorName == "" {
		t.Fatal("human author name should not be empty")
	}
	if _, err := db.GetParticipantByName(msgs[0].AuthorName); err != nil {
		t.Fatalf("expected human participant to be registered: %v", err)
	}
	if got := home; got == "" {
		t.Fatal("state db path sanity check failed")
	}
}

func TestSayCommandAsAgentAddsSeededMetadata(t *testing.T) {
	_, stream, db := setupCommandStateAndStream(t)
	agent, err := db.RegisterParticipant(model.Participant{
		Name:     "claude",
		Kind:     model.KindAgent,
		Provider: "anthropic",
		Model:    "claude-4",
	})
	if err != nil {
		t.Fatalf("registering agent: %v", err)
	}

	originalSayAs := sayAs
	sayAs = "claude"
	defer func() { sayAs = originalSayAs }()

	if err := sayCmd.RunE(sayCmd, []string{"seeded", "thought"}); err != nil {
		t.Fatalf("sayCmd.RunE returned error: %v", err)
	}

	msgs, err := stream.Replay(context.Background(), 0, 10)
	if err != nil {
		t.Fatalf("replaying messages: %v", err)
	}
	if len(msgs) != 1 {
		t.Fatalf("expected 1 message, got %d", len(msgs))
	}
	msg := msgs[0]
	if msg.AuthorID != agent.ID || msg.AuthorName != "claude" {
		t.Fatalf("message author = (%q, %q), want agent claude", msg.AuthorID, msg.AuthorName)
	}
	if got := msg.Metadata["authorship_mode"]; got != "seeded" {
		t.Fatalf("authorship_mode = %#v, want %q", got, "seeded")
	}
	seededBy, ok := msg.Metadata["seeded_by"].(string)
	if !ok || seededBy == "" {
		t.Fatalf("seeded_by metadata missing or invalid: %#v", msg.Metadata["seeded_by"])
	}
	if _, err := db.GetParticipantByID(seededBy); err != nil {
		t.Fatalf("expected seeded_by participant to exist: %v", err)
	}
}

func TestSayCommandFailsForUnknownAgent(t *testing.T) {
	setupCommandStateAndStream(t)

	originalSayAs := sayAs
	sayAs = "missing-agent"
	defer func() { sayAs = originalSayAs }()

	if err := sayCmd.RunE(sayCmd, []string{"hello"}); err == nil {
		t.Fatal("sayCmd.RunE should fail for an unknown agent identity")
	}
}

func setupCommandStateAndStream(t *testing.T) (string, *transport.Stream, *state.DB) {
	t.Helper()
	home := t.TempDir()
	t.Setenv("HOME", home)

	server, err := transport.NewServer(t.TempDir())
	if err != nil {
		t.Fatalf("starting transport server: %v", err)
	}
	t.Cleanup(server.Stop)

	stream, err := transport.NewManagedStream(server.ClientURL(), transport.StreamOptions{})
	if err != nil {
		t.Fatalf("opening managed stream: %v", err)
	}
	t.Cleanup(stream.Close)

	if err := os.MkdirAll(filepath.Join(home, ".a2a"), 0755); err != nil {
		t.Fatalf("creating config dir: %v", err)
	}
	if err := os.MkdirAll(DefaultDataDir(), 0755); err != nil {
		t.Fatalf("creating data dir: %v", err)
	}
	if err := os.WriteFile(DaemonURLPath(), []byte(server.ClientURL()), 0600); err != nil {
		t.Fatalf("writing daemon url: %v", err)
	}

	db, err := state.Open(filepath.Join(DefaultDataDir(), "state.db"))
	if err != nil {
		t.Fatalf("opening state db: %v", err)
	}
	t.Cleanup(func() { _ = db.Close() })

	return home, stream, db
}
