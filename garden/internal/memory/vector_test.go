package memory

import (
	"context"
	"fmt"
	"testing"
	"time"
)

func TestVectorCacheNoticesAnotherStoreDeletingAnItem(t *testing.T) {
	dir := t.TempDir()
	daemon, err := Open(dir, Options{})
	if err != nil {
		t.Fatal(err)
	}
	defer daemon.Close()
	inserted, err := daemon.Insert(testItem("message-1", "remembered"))
	if err != nil {
		t.Fatal(err)
	}
	if err := daemon.SetEmbedding(inserted.ID, []float32{1, 0}, "model"); err != nil {
		t.Fatal(err)
	}
	if items, ok, err := daemon.searchVector([]float32{1, 0}, "model", 10); err != nil || !ok || len(items) != 1 {
		t.Fatalf("warm cache: items=%d ok=%v err=%v", len(items), ok, err)
	}
	cli, err := Open(dir, Options{})
	if err != nil {
		t.Fatal(err)
	}
	if err := cli.Forget(inserted.ID); err != nil {
		t.Fatal(err)
	}
	_ = cli.Close()
	items, ok, err := daemon.searchVector([]float32{1, 0}, "model", 10)
	if err != nil || !ok || len(items) != 0 {
		t.Fatalf("stale cache: items=%d ok=%v err=%v", len(items), ok, err)
	}
}

func TestPendingEmbeddingsPageAdvancesPastEarlierFailures(t *testing.T) {
	store, _ := openTestStore(t, Options{})
	for i := range 5 {
		if _, err := store.Insert(testItem(
			string(rune('a'+i)), "pending "+string(rune('a'+i)),
		)); err != nil {
			t.Fatal(err)
		}
	}
	first, cursor, err := store.PendingEmbeddingsPage("model", 0, 2)
	if err != nil {
		t.Fatal(err)
	}
	second, _, err := store.PendingEmbeddingsPage("model", cursor, 2)
	if err != nil {
		t.Fatal(err)
	}
	if len(first) != 2 || len(second) != 2 || first[0].ID == second[0].ID {
		t.Fatalf("pages overlap or starve: first=%d second=%d", len(first), len(second))
	}
}

func TestVectorCacheRetriesOnceWhenGenerationChangesDuringLoad(t *testing.T) {
	dir := t.TempDir()
	daemon, err := Open(dir, Options{})
	if err != nil {
		t.Fatal(err)
	}
	defer daemon.Close()
	writer, err := Open(dir, Options{})
	if err != nil {
		t.Fatal(err)
	}
	defer writer.Close()
	first, err := daemon.Insert(testItem("first", "first"))
	if err != nil {
		t.Fatal(err)
	}
	if err := daemon.SetEmbedding(first.ID, []float32{1, 0}, "model"); err != nil {
		t.Fatal(err)
	}
	loads := 0
	daemon.afterVectorLoad = func() {
		loads++
		if loads != 1 {
			return
		}
		second, insertErr := writer.Insert(testItem("second", "second"))
		if insertErr != nil {
			t.Fatal(insertErr)
		}
		if embedErr := writer.SetEmbedding(second.ID, []float32{0.9, 0.1}, "model"); embedErr != nil {
			t.Fatal(embedErr)
		}
	}
	items, ok, err := daemon.searchVector([]float32{1, 0}, "model", 10)
	if err != nil || !ok || loads != 2 || len(items) != 2 {
		t.Fatalf("items=%d ok=%v loads=%d err=%v", len(items), ok, loads, err)
	}
}

func TestVectorCacheSkipsArmAfterSecondConcurrentChange(t *testing.T) {
	dir := t.TempDir()
	daemon, err := Open(dir, Options{})
	if err != nil {
		t.Fatal(err)
	}
	defer daemon.Close()
	writer, err := Open(dir, Options{})
	if err != nil {
		t.Fatal(err)
	}
	defer writer.Close()
	first, err := daemon.Insert(testItem("first", "first"))
	if err != nil {
		t.Fatal(err)
	}
	if err := daemon.SetEmbedding(first.ID, []float32{1, 0}, "model"); err != nil {
		t.Fatal(err)
	}
	loads := 0
	daemon.afterVectorLoad = func() {
		loads++
		source := fmt.Sprintf("churn-%d", loads)
		inserted, insertErr := writer.Insert(testItem(source, source))
		if insertErr != nil {
			t.Fatal(insertErr)
		}
		if embedErr := writer.SetEmbedding(inserted.ID, []float32{0.5, 0.5}, "model"); embedErr != nil {
			t.Fatal(embedErr)
		}
	}
	items, ok, err := daemon.searchVector([]float32{1, 0}, "model", 10)
	if err != nil || ok || loads != 2 || len(items) != 0 {
		t.Fatalf("items=%d ok=%v loads=%d err=%v", len(items), ok, loads, err)
	}
}

func TestGenerationUsesReservedConnection(t *testing.T) {
	store, _ := openTestStore(t, Options{})
	store.db.SetMaxOpenConns(2)
	ordinary, err := store.db.Conn(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	defer ordinary.Close()
	result := make(chan error, 1)
	go func() {
		_, generationErr := store.generation()
		result <- generationErr
	}()
	select {
	case err := <-result:
		if err != nil {
			t.Fatal(err)
		}
	case <-time.After(time.Second):
		t.Fatal("generation read waited for the ordinary connection pool")
	}
}
