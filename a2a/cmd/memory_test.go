package cmd

import (
	"os"
	"path/filepath"
	"testing"
	"time"

	"github.com/veridian69/cairn/a2a/internal/memory"
	"github.com/veridian69/cairn/a2a/internal/state"
)

func setupMemoryHome(t *testing.T) (*memory.Store, *state.DB) {
	t.Helper()
	t.Setenv("HOME", t.TempDir())
	if err := ensureDataDir(DefaultDataDir()); err != nil {
		t.Fatal(err)
	}
	db, err := state.Open(filepath.Join(DefaultDataDir(), "state.db"))
	if err != nil {
		t.Fatal(err)
	}
	store, err := memory.Open(DefaultDataDir(), memory.Options{MaxItemBytes: 4096})
	if err != nil {
		db.Close()
		t.Fatal(err)
	}
	t.Cleanup(func() {
		store.Close()
		db.Close()
	})
	return store, db
}

func TestMemoryMutationAcceptsPrintedShortID(t *testing.T) {
	store, _ := setupMemoryHome(t)
	inserted, err := store.Insert(memory.Item{
		Content: "fact", AuthorID: "agent", AuthorName: "claude",
		SourceMessageID: "message", SourceCreatedAt: time.Now().UTC(),
		NominatedBy: "human",
	})
	if err != nil {
		t.Fatal(err)
	}
	if err := memoryPinCmd.RunE(memoryPinCmd, []string{inserted.ID[:8]}); err != nil {
		t.Fatal(err)
	}
	item, err := store.GetByID(inserted.ID)
	if err != nil || !item.Pinned {
		t.Fatalf("short-ID pin failed: item=%+v err=%v", item, err)
	}
}

func TestEnsureDataDirTightensExistingPermissions(t *testing.T) {
	dir := filepath.Join(t.TempDir(), "data")
	if err := os.Mkdir(dir, 0755); err != nil {
		t.Fatal(err)
	}
	if err := ensureDataDir(dir); err != nil {
		t.Fatal(err)
	}
	info, err := os.Stat(dir)
	if err != nil {
		t.Fatal(err)
	}
	if got := info.Mode().Perm(); got != 0700 {
		t.Fatalf("data directory mode = %o, want 700", got)
	}
}
