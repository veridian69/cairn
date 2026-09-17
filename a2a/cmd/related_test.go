package cmd

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/veridian69/cairn/a2a/internal/memory"
)

func TestRelatedOfflineFindsStoredDescendant(t *testing.T) {
	store, _ := setupMemoryHome(t)
	parent := "parent-message"
	if _, err := store.Insert(memory.Item{
		Content: "child fact", AuthorID: "agent", AuthorName: "claude",
		SourceMessageID: "child-message", SourceCreatedAt: time.Now().UTC(),
		ReplyTo: &parent, NominatedBy: "human",
	}); err != nil {
		t.Fatal(err)
	}
	output := captureStdout(t, func() {
		if err := relatedCmd.RunE(relatedCmd, []string{parent}); err != nil {
			t.Fatal(err)
		}
	})
	if !strings.Contains(output, "offline") ||
		!strings.Contains(output, "causal") ||
		!strings.Contains(output, "child fact") {
		t.Fatalf("output:\n%s", output)
	}
}

func TestRelatedSkipsAnchorVectorFromStaleEmbeddingModel(t *testing.T) {
	store, _ := setupMemoryHome(t)
	if err := os.MkdirAll(DefaultConfigDir(), 0700); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(DefaultConfigDir(), "config.yaml"), []byte(`agents: {}
memory:
  embedding:
    provider: openai
    model: current-model
    api_key: test
`), 0600); err != nil {
		t.Fatal(err)
	}
	now := time.Now().UTC()
	anchor, err := store.Insert(memory.Item{
		Content: "anchor", AuthorID: "agent", AuthorName: "claude",
		SourceMessageID: "anchor-message", SourceCreatedAt: now,
		NominatedBy: "human",
	})
	if err != nil {
		t.Fatal(err)
	}
	neighbour, err := store.Insert(memory.Item{
		Content: "semantic-only neighbour", AuthorID: "agent", AuthorName: "claude",
		SourceMessageID: "neighbour-message", SourceCreatedAt: now.Add(2 * time.Hour),
		NominatedBy: "human",
	})
	if err != nil {
		t.Fatal(err)
	}
	if err := store.SetEmbedding(anchor.ID, []float32{1, 0}, "old-model"); err != nil {
		t.Fatal(err)
	}
	if err := store.SetEmbedding(neighbour.ID, []float32{0.9, 0.1}, "old-model"); err != nil {
		t.Fatal(err)
	}
	output := captureStdout(t, func() {
		if err := relatedCmd.RunE(relatedCmd, []string{anchor.ID}); err != nil {
			t.Fatal(err)
		}
	})
	if strings.Contains(output, "semantic-only neighbour") || strings.Contains(output, "semantic") {
		t.Fatalf("stale embedding model was used:\n%s", output)
	}
}

func TestRelatedTemporalUsesSourceTimeRatherThanCurationTime(t *testing.T) {
	store, _ := setupMemoryHome(t)
	old := time.Now().UTC().AddDate(0, -1, 0)
	anchor, err := store.Insert(memory.Item{
		Content: "old source", AuthorID: "agent", AuthorName: "claude",
		SourceMessageID: "old-message", SourceCreatedAt: old,
		NominatedBy: "human",
	})
	if err != nil {
		t.Fatal(err)
	}
	if _, err := store.Insert(memory.Item{
		Content: "curated today", AuthorID: "agent", AuthorName: "claude",
		SourceMessageID: "today-message", SourceCreatedAt: time.Now().UTC(),
		NominatedBy: "human",
	}); err != nil {
		t.Fatal(err)
	}
	output := captureStdout(t, func() {
		if err := relatedCmd.RunE(relatedCmd, []string{anchor.ID}); err != nil {
			t.Fatal(err)
		}
	})
	if strings.Contains(output, "curated today") {
		t.Fatalf("curation time was used as temporal source:\n%s", output)
	}
}

func TestRelatedOfflineUnknownBareMessageExplainsLimitedResult(t *testing.T) {
	setupMemoryHome(t)
	output := captureStdout(t, func() {
		if err := relatedCmd.RunE(relatedCmd, []string{"unknown-message"}); err != nil {
			t.Fatal(err)
		}
	})
	if !strings.Contains(output, "not in memory") ||
		!strings.Contains(output, "daemon is unreachable") {
		t.Fatalf("missing offline bare-ID explanation:\n%s", output)
	}
}
