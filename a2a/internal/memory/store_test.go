package memory

import (
	"errors"
	"os"
	"path/filepath"
	"sync"
	"testing"
)

func openTestStore(t *testing.T, options Options) (*Store, string) {
	t.Helper()
	dir := t.TempDir()
	store, err := Open(dir, options)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = store.Close() })
	return store, dir
}

func TestConcurrentFirstOpenCreatesOneWALSchema(t *testing.T) {
	dir := filepath.Join(t.TempDir(), "nested", "data")
	var wg sync.WaitGroup
	errs := make(chan error, 4)
	for range 4 {
		wg.Add(1)
		go func() {
			defer wg.Done()
			store, err := Open(dir, Options{})
			if err == nil {
				err = store.Close()
			}
			errs <- err
		}()
	}
	wg.Wait()
	close(errs)
	for err := range errs {
		if err != nil {
			t.Fatalf("concurrent open: %v", err)
		}
	}
	store, err := Open(dir, Options{})
	if err != nil {
		t.Fatal(err)
	}
	defer store.Close()
	var versions, timeout int
	var mode string
	if err := store.db.QueryRow(`SELECT COUNT(*) FROM schema_version`).Scan(&versions); err != nil {
		t.Fatal(err)
	}
	if err := store.db.QueryRow(`PRAGMA journal_mode`).Scan(&mode); err != nil {
		t.Fatal(err)
	}
	if err := store.db.QueryRow(`PRAGMA busy_timeout`).Scan(&timeout); err != nil {
		t.Fatal(err)
	}
	if versions != 1 || mode != "wal" || timeout != 5000 {
		t.Fatalf("schema/pragmas: versions=%d mode=%q timeout=%d", versions, mode, timeout)
	}
}

func TestOpenRejectsFutureSchema(t *testing.T) {
	store, dir := openTestStore(t, Options{})
	if _, err := store.db.Exec(`UPDATE schema_version SET version = 999 WHERE id = 1`); err != nil {
		t.Fatal(err)
	}
	if err := store.Close(); err != nil {
		t.Fatal(err)
	}
	if _, err := Open(dir, Options{}); !errors.Is(err, ErrFutureSchema) {
		t.Fatalf("Open error = %v, want ErrFutureSchema", err)
	}
}

func TestDaemonOpenRebuildsFTSAndDatabaseMode(t *testing.T) {
	store, dir := openTestStore(t, Options{})
	inserted, err := store.Insert(testItem("source", "repairable index text"))
	if err != nil {
		t.Fatal(err)
	}
	if _, err := store.db.Exec(`DELETE FROM memories_fts WHERE memory_id = ?`, inserted.ID); err != nil {
		t.Fatal(err)
	}
	if err := store.Close(); err != nil {
		t.Fatal(err)
	}

	store, err = Open(dir, Options{})
	if err != nil {
		t.Fatal(err)
	}
	found, err := store.Recall(RecallRequest{
		Query: "repairable", Limit: 3, QueryBytes: 1024, Exclude: map[string]bool{},
	})
	if err != nil {
		t.Fatal(err)
	}
	if len(found) != 0 {
		t.Fatalf("ordinary CLI-style open unexpectedly rebuilt FTS: %+v", found)
	}
	if err := store.Close(); err != nil {
		t.Fatal(err)
	}

	store, err = Open(dir, Options{RebuildFTS: true})
	if err != nil {
		t.Fatal(err)
	}
	defer store.Close()
	found, err = store.Recall(RecallRequest{
		Query: "repairable", Limit: 3, QueryBytes: 1024, Exclude: map[string]bool{},
	})
	if err != nil {
		t.Fatal(err)
	}
	if len(found) != 1 || found[0].ID != inserted.ID {
		t.Fatalf("FTS rebuild returned %+v", found)
	}
	info, err := os.Stat(filepath.Join(dir, "memory.db"))
	if err != nil {
		t.Fatal(err)
	}
	if got := info.Mode().Perm(); got != 0600 {
		t.Fatalf("memory.db mode = %o, want 600", got)
	}
}

func TestReadOnlySecondStoreDoesNotChangeDataVersion(t *testing.T) {
	observer, dir := openTestStore(t, Options{})
	before, err := observer.generation()
	if err != nil {
		t.Fatal(err)
	}
	reader, err := Open(dir, Options{})
	if err != nil {
		t.Fatal(err)
	}
	if _, err := reader.List(ListFilter{}, map[string]bool{}); err != nil {
		t.Fatal(err)
	}
	if err := reader.Close(); err != nil {
		t.Fatal(err)
	}
	after, err := observer.generation()
	if err != nil {
		t.Fatal(err)
	}
	if before != after {
		t.Fatalf("read-only open changed data_version: before=%d after=%d", before, after)
	}
}
