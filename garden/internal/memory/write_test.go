package memory

import (
	"errors"
	"sync"
	"testing"
	"time"
)

func testItem(sourceID, content string) Item {
	return Item{
		Content:         content,
		AuthorID:        "author-1",
		AuthorName:      "claude",
		SourceMessageID: sourceID,
		SourceCreatedAt: time.Date(2026, 7, 1, 12, 0, 0, 0, time.UTC),
		NominatedBy:     "operator-1",
	}
}

func TestConcurrentInsertIsIdempotentAndPreservesContent(t *testing.T) {
	dir := t.TempDir()
	a, err := Open(dir, Options{MaxItemBytes: 4096})
	if err != nil {
		t.Fatal(err)
	}
	defer a.Close()
	b, err := Open(dir, Options{MaxItemBytes: 4096})
	if err != nil {
		t.Fatal(err)
	}
	defer b.Close()

	stores := []*Store{a, b}
	results := make([]InsertResult, 2)
	errs := make([]error, 2)
	var wg sync.WaitGroup
	for i := range stores {
		wg.Add(1)
		go func(i int) {
			defer wg.Done()
			results[i], errs[i] = stores[i].Insert(testItem("message-1", "  line one\n\n  line two  "))
		}(i)
	}
	wg.Wait()
	for _, err := range errs {
		if err != nil {
			t.Fatal(err)
		}
	}
	if results[0].ID != results[1].ID || results[0].Existed == results[1].Existed {
		t.Fatalf("results = %+v", results)
	}
	item, err := a.GetByID(results[0].ID)
	if err != nil {
		t.Fatal(err)
	}
	if item.Content != "line one\n\n  line two" {
		t.Fatalf("content changed: %q", item.Content)
	}
}

func TestInsertRejectsMissingProvenance(t *testing.T) {
	store, _ := openTestStore(t, Options{})
	item := testItem("", "fact")
	if _, err := store.Insert(item); !errors.Is(err, ErrInvalidProvenance) {
		t.Fatalf("error = %v, want ErrInvalidProvenance", err)
	}
}

func TestInsertRejectsBlankAndOversizedAndKeepsDistinctProvenance(t *testing.T) {
	store, _ := openTestStore(t, Options{MaxItemBytes: 8})
	if _, err := store.Insert(testItem("blank", " \n\t ")); !errors.Is(err, ErrEmptyContent) {
		t.Fatalf("blank error = %v", err)
	}
	if _, err := store.Insert(testItem("large", "123456789")); !errors.Is(err, ErrTooLarge) {
		t.Fatalf("oversized error = %v", err)
	}
	first, err := store.Insert(testItem("source-one", "same"))
	if err != nil {
		t.Fatal(err)
	}
	second, err := store.Insert(testItem("source-two", "same"))
	if err != nil {
		t.Fatal(err)
	}
	if first.ID == second.ID {
		t.Fatal("identical content from distinct source messages was deduplicated")
	}
}

func TestResolveIDTreatsPrefixAsLiteralAndRejectsEmpty(t *testing.T) {
	store, _ := openTestStore(t, Options{})
	inserted, err := store.Insert(testItem("message", "fact"))
	if err != nil {
		t.Fatal(err)
	}
	resolved, err := store.ResolveID(inserted.ID[:8])
	if err != nil || resolved != inserted.ID {
		t.Fatalf("ResolveID = %q, %v", resolved, err)
	}
	for _, unsafe := range []string{"", "%", "_"} {
		if _, err := store.ResolveID(unsafe); !errors.Is(err, ErrNotFound) {
			t.Fatalf("ResolveID(%q) error = %v, want ErrNotFound", unsafe, err)
		}
	}
}

func TestPruneIDsDeletesOnlyConfirmedSnapshot(t *testing.T) {
	store, _ := openTestStore(t, Options{})
	first, err := store.Insert(testItem("first", "first"))
	if err != nil {
		t.Fatal(err)
	}
	candidates, err := store.PruneCandidates(PruneFilter{})
	if err != nil {
		t.Fatal(err)
	}
	second, err := store.Insert(testItem("second", "second"))
	if err != nil {
		t.Fatal(err)
	}
	deleted, err := store.PruneIDs(candidates)
	if err != nil {
		t.Fatal(err)
	}
	if deleted != 1 {
		t.Fatalf("deleted = %d, want 1", deleted)
	}
	if _, err := store.GetByID(first.ID); !errors.Is(err, ErrNotFound) {
		t.Fatalf("confirmed item survived: %v", err)
	}
	if _, err := store.GetByID(second.ID); err != nil {
		t.Fatalf("post-confirmation item was deleted: %v", err)
	}
}
