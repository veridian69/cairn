package snapshot

import (
	"context"
	"database/sql"
	"fmt"
	"os"
	"path/filepath"
	"testing"
	"time"

	"github.com/veridian69/cairn/a2a/internal/memory"
	"github.com/veridian69/cairn/a2a/internal/model"
	"github.com/veridian69/cairn/a2a/internal/state"
	"github.com/veridian69/cairn/a2a/internal/transport"
)

func TestResetAndRestoreCompleteRuntimeLifecycle(t *testing.T) {
	root := t.TempDir()
	configDir := filepath.Join(root, ".a2a")
	dataDir := filepath.Join(root, "runtime")
	if err := os.MkdirAll(configDir, 0700); err != nil {
		t.Fatal(err)
	}
	configBytes := []byte(fmt.Sprintf(
		"agents: {}\nstream:\n  data_dir: %s\nmemory:\n  enabled: true\n",
		dataDir,
	))
	if err := os.WriteFile(filepath.Join(configDir, "config.yaml"), configBytes, 0600); err != nil {
		t.Fatal(err)
	}

	participant, firstMessage := seedRuntimeVersion(t, dataDir)
	manager := NewManager(configDir, "test-version")
	first, err := manager.Create(context.Background(), "version-a")
	if err != nil {
		t.Fatal(err)
	}

	secondMessage := model.NewMessage(participant, "second message", nil)
	publishMessages(t, dataDir, secondMessage)
	db, err := state.Open(filepath.Join(dataDir, "state.db"))
	if err != nil {
		t.Fatal(err)
	}
	if err := db.SaveCheckpoint(state.Checkpoint{
		ParticipantID: participant.ID, LastSeenSeq: 2,
		LastProcessedID: secondMessage.ID, HourlyCount: 2,
		HourlyResetAt: time.Now().UTC(), Responsiveness: 0.9,
	}); err != nil {
		t.Fatal(err)
	}
	if err := db.Close(); err != nil {
		t.Fatal(err)
	}
	store, err := memory.Open(dataDir, memory.Options{MaxItemBytes: 4096})
	if err != nil {
		t.Fatal(err)
	}
	if _, err := store.Insert(memory.Item{
		Content: "second memory", AuthorID: participant.ID, AuthorName: participant.Name,
		SourceMessageID: secondMessage.ID, SourceCreatedAt: secondMessage.CreatedAt,
		NominatedBy: participant.ID,
	}); err != nil {
		t.Fatal(err)
	}
	if err := store.Close(); err != nil {
		t.Fatal(err)
	}
	second, err := manager.Create(context.Background(), "version-b")
	if err != nil {
		t.Fatal(err)
	}

	if err := manager.Reset(context.Background()); err != nil {
		t.Fatal(err)
	}
	if _, err := os.Stat(filepath.Join(dataDir, "state.db")); !os.IsNotExist(err) {
		t.Fatalf("state database survived reset: %v", err)
	}
	if _, err := os.Stat(filepath.Join(dataDir, "memory.db")); !os.IsNotExist(err) {
		t.Fatalf("memory database survived reset: %v", err)
	}
	if _, err := os.Stat(filepath.Join(dataDir, "jetstream")); !os.IsNotExist(err) {
		t.Fatalf("JetStream survived reset: %v", err)
	}

	if _, err := manager.Restore(context.Background(), first.ID); err != nil {
		t.Fatal(err)
	}
	assertRuntimeVersion(t, dataDir, firstMessage.ID, 1, 1, 1)

	if _, err := manager.Restore(context.Background(), second.ID); err != nil {
		t.Fatal(err)
	}
	assertRuntimeVersion(t, dataDir, firstMessage.ID, 2, 2, 2)
}

func seedRuntimeVersion(t *testing.T, dataDir string) (model.Participant, model.Message) {
	t.Helper()
	if err := os.MkdirAll(dataDir, 0700); err != nil {
		t.Fatal(err)
	}
	db, err := state.Open(filepath.Join(dataDir, "state.db"))
	if err != nil {
		t.Fatal(err)
	}
	participant, err := db.RegisterParticipant(model.Participant{
		Name: "val", Kind: model.KindAgent, Provider: "openai", Model: "test",
	})
	if err != nil {
		t.Fatal(err)
	}
	message := model.NewMessage(participant, "first message", nil)
	if err := db.SaveCheckpoint(state.Checkpoint{
		ParticipantID: participant.ID, LastSeenSeq: 1,
		LastProcessedID: message.ID, HourlyCount: 1,
		HourlyResetAt: time.Now().UTC(), Responsiveness: 0.9,
	}); err != nil {
		t.Fatal(err)
	}
	if err := db.Redact(message.ID, "test redaction", participant.ID); err != nil {
		t.Fatal(err)
	}
	if err := db.Close(); err != nil {
		t.Fatal(err)
	}
	store, err := memory.Open(dataDir, memory.Options{MaxItemBytes: 4096})
	if err != nil {
		t.Fatal(err)
	}
	if _, err := store.Insert(memory.Item{
		Content: "first memory", AuthorID: participant.ID, AuthorName: participant.Name,
		SourceMessageID: message.ID, SourceCreatedAt: message.CreatedAt,
		NominatedBy: participant.ID, Pinned: true,
	}); err != nil {
		t.Fatal(err)
	}
	if err := store.Close(); err != nil {
		t.Fatal(err)
	}
	publishMessages(t, dataDir, message)
	return participant, message
}

func publishMessages(t *testing.T, dataDir string, messages ...model.Message) {
	t.Helper()
	server, err := transport.NewServer(dataDir)
	if err != nil {
		t.Fatal(err)
	}
	stream, err := transport.NewManagedStream(server.ClientURL(), transport.StreamOptions{})
	if err != nil {
		server.Stop()
		t.Fatal(err)
	}
	for _, message := range messages {
		if err := stream.Publish(context.Background(), message); err != nil {
			stream.Close()
			server.Stop()
			t.Fatal(err)
		}
	}
	stream.Close()
	server.Stop()
}

func assertRuntimeVersion(
	t *testing.T,
	dataDir string,
	redactedMessageID string,
	wantMessages, wantMemories int,
	wantLastSeen uint64,
) {
	t.Helper()
	server, err := transport.NewServer(dataDir)
	if err != nil {
		t.Fatal(err)
	}
	stream, err := transport.NewStream(server.ClientURL())
	if err != nil {
		server.Stop()
		t.Fatal(err)
	}
	messages, err := stream.Replay(context.Background(), 0, 10)
	stream.Close()
	server.Stop()
	if err != nil {
		t.Fatal(err)
	}
	if len(messages) != wantMessages {
		t.Fatalf("messages = %d, want %d", len(messages), wantMessages)
	}
	db, err := state.Open(filepath.Join(dataDir, "state.db"))
	if err != nil {
		t.Fatal(err)
	}
	participant, err := db.GetParticipantByName("val")
	if err != nil {
		t.Fatal(err)
	}
	redacted, err := db.IsRedacted(redactedMessageID)
	if err != nil {
		t.Fatal(err)
	}
	if err := db.Close(); err != nil {
		t.Fatal(err)
	}
	raw, err := sql.Open("sqlite", filepath.Join(dataDir, "state.db"))
	if err != nil {
		t.Fatal(err)
	}
	var lastSeen uint64
	if err := raw.QueryRow(
		"SELECT last_seen_seq FROM checkpoints WHERE participant_id = ?",
		participant.ID,
	).Scan(&lastSeen); err != nil {
		_ = raw.Close()
		t.Fatal(err)
	}
	if err := raw.Close(); err != nil {
		t.Fatal(err)
	}
	if lastSeen != wantLastSeen || !redacted {
		t.Fatalf("last_seen=%d redacted=%v", lastSeen, redacted)
	}

	store, err := memory.Open(dataDir, memory.Options{MaxItemBytes: 4096})
	if err != nil {
		t.Fatal(err)
	}
	items, err := store.List(memory.ListFilter{}, nil)
	if err != nil {
		t.Fatal(err)
	}
	if err := store.Close(); err != nil {
		t.Fatal(err)
	}
	if len(items) != wantMemories {
		t.Fatalf("memories = %d, want %d", len(items), wantMemories)
	}
}
