package state

import (
	"path/filepath"
	"testing"

	"github.com/veridian69/cairn/a2a/internal/model"
)

func TestOpenAndMigrate(t *testing.T) {
	dir := t.TempDir()
	db, err := Open(filepath.Join(dir, "state.db"))
	if err != nil {
		t.Fatalf("Open: %v", err)
	}
	defer db.Close()
}

func TestOpenEnablesWALBusyTimeoutAndForeignKeys(t *testing.T) {
	db, err := Open(filepath.Join(t.TempDir(), "state.db"))
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()
	var mode string
	var timeout, foreignKeys int
	if err := db.db.QueryRow(`PRAGMA journal_mode`).Scan(&mode); err != nil {
		t.Fatal(err)
	}
	if err := db.db.QueryRow(`PRAGMA busy_timeout`).Scan(&timeout); err != nil {
		t.Fatal(err)
	}
	if err := db.db.QueryRow(`PRAGMA foreign_keys`).Scan(&foreignKeys); err != nil {
		t.Fatal(err)
	}
	if mode != "wal" || timeout != 5000 || foreignKeys != 1 {
		t.Fatalf("pragmas: journal=%q timeout=%d foreign_keys=%d", mode, timeout, foreignKeys)
	}
}

func TestNewStateDatabaseDoesNotCreateRetiredGardenTable(t *testing.T) {
	db, err := Open(filepath.Join(t.TempDir(), "state.db"))
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()
	var count int
	if err := db.db.QueryRow(`SELECT COUNT(*) FROM sqlite_master
		WHERE type = 'table' AND name = 'garden_state'`).Scan(&count); err != nil {
		t.Fatal(err)
	}
	if count != 0 {
		t.Fatal("new state database still contains retired garden_state table")
	}
}

func TestParticipantCRUD(t *testing.T) {
	dir := t.TempDir()
	db, err := Open(filepath.Join(dir, "state.db"))
	if err != nil {
		t.Fatalf("Open: %v", err)
	}
	defer db.Close()

	p := model.Participant{
		Name:     "claude",
		Kind:     model.KindAgent,
		Provider: "anthropic",
		Model:    "claude-opus-4-6",
	}

	created, err := db.RegisterParticipant(p)
	if err != nil {
		t.Fatalf("Register: %v", err)
	}
	if created.ID == "" {
		t.Error("expected generated ID")
	}
	if created.Name != "claude" {
		t.Errorf("Name = %q", created.Name)
	}

	// Register again with same name — should return existing
	again, err := db.RegisterParticipant(p)
	if err != nil {
		t.Fatalf("Register again: %v", err)
	}
	if again.ID != created.ID {
		t.Errorf("expected same ID, got %q vs %q", again.ID, created.ID)
	}

	// Get by name
	got, err := db.GetParticipantByName("claude")
	if err != nil {
		t.Fatalf("GetByName: %v", err)
	}
	if got.ID != created.ID {
		t.Errorf("GetByName ID mismatch")
	}

	// List all
	all, err := db.ListParticipants()
	if err != nil {
		t.Fatalf("List: %v", err)
	}
	if len(all) != 1 {
		t.Errorf("got %d participants, want 1", len(all))
	}

	updated, err := db.RegisterParticipant(model.Participant{
		Name:     "claude",
		Kind:     model.KindAgent,
		Provider: "anthropic",
		Model:    "claude-opus-4-7",
	})
	if err != nil {
		t.Fatalf("Register update: %v", err)
	}
	if updated.ID != created.ID {
		t.Fatalf("updated ID = %q, want %q", updated.ID, created.ID)
	}
	if updated.Model != "claude-opus-4-7" {
		t.Fatalf("updated model = %q, want %q", updated.Model, "claude-opus-4-7")
	}
}

func TestRedactionCRUD(t *testing.T) {
	dir := t.TempDir()
	db, err := Open(filepath.Join(dir, "state.db"))
	if err != nil {
		t.Fatalf("Open: %v", err)
	}
	defer db.Close()

	if err := db.Redact("msg-1", "secret", "admin"); err != nil {
		t.Fatalf("Redact: %v", err)
	}

	redacted, err := db.IsRedacted("msg-1")
	if err != nil {
		t.Fatalf("IsRedacted msg-1: %v", err)
	}
	if !redacted {
		t.Error("msg-1 should be redacted")
	}
	redacted, err = db.IsRedacted("msg-2")
	if err != nil {
		t.Fatalf("IsRedacted msg-2: %v", err)
	}
	if redacted {
		t.Error("msg-2 should not be redacted")
	}

	ids, err := db.RedactedIDs()
	if err != nil {
		t.Fatalf("RedactedIDs: %v", err)
	}
	if len(ids) != 1 || !ids["msg-1"] {
		t.Errorf("RedactedIDs = %v", ids)
	}

	reasons, err := db.RedactionReasons()
	if err != nil {
		t.Fatalf("RedactionReasons: %v", err)
	}
	if reasons["msg-1"] != "secret" {
		t.Fatalf("reason = %q, want %q", reasons["msg-1"], "secret")
	}

	reason, ok, err := db.RedactionReason("msg-1")
	if err != nil {
		t.Fatalf("RedactionReason msg-1: %v", err)
	}
	if !ok || reason != "secret" {
		t.Fatalf("RedactionReason msg-1 = (%q, %v), want (%q, true)", reason, ok, "secret")
	}

	reason, ok, err = db.RedactionReason("msg-2")
	if err != nil {
		t.Fatalf("RedactionReason msg-2: %v", err)
	}
	if ok || reason != "" {
		t.Fatalf("RedactionReason msg-2 = (%q, %v), want (\"\", false)", reason, ok)
	}
}

func TestCheckpointCRUD(t *testing.T) {
	dir := t.TempDir()
	db, err := Open(filepath.Join(dir, "state.db"))
	if err != nil {
		t.Fatalf("Open: %v", err)
	}
	defer db.Close()

	// Register participant first
	p, _ := db.RegisterParticipant(model.Participant{Name: "claude", Kind: "agent"})

	// Initial checkpoint
	cp, err := db.GetCheckpoint(p.ID, 0.5)
	if err != nil {
		t.Fatalf("GetCheckpoint: %v", err)
	}
	if cp.LastSeenSeq != 0 {
		t.Errorf("initial LastSeenSeq = %d", cp.LastSeenSeq)
	}

	// Update
	cp.LastSeenSeq = 42
	cp.LastProcessedID = "msg-1"
	cp.HourlyCount = 5
	cp.Responsiveness = 0.7
	if err := db.SaveCheckpoint(cp); err != nil {
		t.Fatalf("SaveCheckpoint: %v", err)
	}

	got, _ := db.GetCheckpoint(p.ID, 0.5)
	if got.LastSeenSeq != 42 {
		t.Errorf("LastSeenSeq = %d, want 42", got.LastSeenSeq)
	}
	if got.Responsiveness != 0.7 {
		t.Errorf("Responsiveness = %f, want 0.7", got.Responsiveness)
	}
	if got.LastProcessedID != "msg-1" {
		t.Errorf("LastProcessedID = %q, want %q", got.LastProcessedID, "msg-1")
	}
	if got.HourlyResetAt.IsZero() {
		t.Error("HourlyResetAt should be populated")
	}
}

func TestDeleteParticipantCascadesCheckpoint(t *testing.T) {
	dir := t.TempDir()
	db, err := Open(filepath.Join(dir, "state.db"))
	if err != nil {
		t.Fatalf("Open: %v", err)
	}
	defer db.Close()

	p, err := db.RegisterParticipant(model.Participant{Name: "claude", Kind: model.KindAgent})
	if err != nil {
		t.Fatalf("Register: %v", err)
	}
	cp, err := db.GetCheckpoint(p.ID, 0.5)
	if err != nil {
		t.Fatalf("GetCheckpoint: %v", err)
	}
	cp.LastSeenSeq = 12
	if err := db.SaveCheckpoint(cp); err != nil {
		t.Fatalf("SaveCheckpoint: %v", err)
	}

	if err := db.DeleteParticipant("claude"); err != nil {
		t.Fatalf("DeleteParticipant: %v", err)
	}

	if _, err := db.GetCheckpoint(p.ID, 0.5); err == nil {
		t.Fatal("GetCheckpoint after participant deletion should fail")
	}
}

func TestGetCheckpointUsesInitialResponsivenessOnlyWhenCreatingRow(t *testing.T) {
	dir := t.TempDir()
	db, err := Open(filepath.Join(dir, "state.db"))
	if err != nil {
		t.Fatalf("Open: %v", err)
	}
	defer db.Close()

	p, err := db.RegisterParticipant(model.Participant{Name: "gpt", Kind: model.KindAgent})
	if err != nil {
		t.Fatalf("Register: %v", err)
	}

	cp, err := db.GetCheckpoint(p.ID, 0.9)
	if err != nil {
		t.Fatalf("GetCheckpoint: %v", err)
	}
	if cp.ParticipantID != p.ID {
		t.Fatalf("ParticipantID = %q, want %q", cp.ParticipantID, p.ID)
	}
	if cp.Responsiveness != 0.9 {
		t.Fatalf("Responsiveness = %f, want 0.9", cp.Responsiveness)
	}

	again, err := db.GetCheckpoint(p.ID, 0.2)
	if err != nil {
		t.Fatalf("GetCheckpoint again: %v", err)
	}
	if again.Responsiveness != 0.9 {
		t.Fatalf("Responsiveness on second read = %f, want persisted 0.9", again.Responsiveness)
	}
}
