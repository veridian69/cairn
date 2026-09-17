package memory

import (
	"context"
	"strings"
	"testing"
	"time"
	"unicode/utf8"

	"github.com/veridian69/cairn/a2a/internal/model"
)

type slowEmbedder struct{}

func (slowEmbedder) ModelName() string { return "model" }
func (slowEmbedder) Embed(ctx context.Context, text string) ([]float32, error) {
	<-ctx.Done()
	return nil, ctx.Err()
}

type recordingEmbedder struct {
	inputs []string
}

func (*recordingEmbedder) ModelName() string { return "model" }
func (embedder *recordingEmbedder) Embed(ctx context.Context, text string) ([]float32, error) {
	embedder.inputs = append(embedder.inputs, text)
	return []float32{1, 0}, nil
}

func TestInjectorEmbeddingTimeoutFallsBackToText(t *testing.T) {
	store, _ := openTestStore(t, Options{})
	if _, err := store.Insert(testItem("source-1", "fallback fact")); err != nil {
		t.Fatal(err)
	}
	injector := &Injector{
		Store: store, Embedder: slowEmbedder{}, EmbedQueries: true,
		QueryTimeout: 20 * time.Millisecond, RecallLimit: 3,
		MaxPinned: 5, QueryBytes: 1024, MaxBlockBytes: 8192,
	}
	start := time.Now()
	block := injector.InjectionBlock(context.Background(), model.Message{
		ID: "trigger", Content: "fallback", CreatedAt: time.Now().UTC(),
	}, nil, nil)
	if elapsed := time.Since(start); elapsed > time.Second {
		t.Fatalf("timeout fallback took %v", elapsed)
	}
	if !strings.Contains(block, "fallback fact") {
		t.Fatalf("text fallback missing:\n%s", block)
	}
}

func TestInjectorBoundsEmbeddingInputAndHonoursDisable(t *testing.T) {
	store, _ := openTestStore(t, Options{})
	recorder := &recordingEmbedder{}
	injector := &Injector{
		Store: store, Embedder: recorder, EmbedQueries: true,
		QueryTimeout: time.Second, RecallLimit: 3, MaxPinned: 5,
		QueryBytes: 5, MaxBlockBytes: 8192,
	}
	injector.InjectionBlock(context.Background(), model.Message{
		ID: "trigger", Content: strings.Repeat("é", 10),
	}, map[string]bool{}, map[string]bool{})
	if len(recorder.inputs) != 1 || len(recorder.inputs[0]) > 5 ||
		!utf8.ValidString(recorder.inputs[0]) {
		t.Fatalf("embedding inputs = %#v", recorder.inputs)
	}

	injector.EmbedQueries = false
	injector.InjectionBlock(context.Background(), model.Message{
		ID: "second", Content: "must not embed",
	}, map[string]bool{}, map[string]bool{})
	if len(recorder.inputs) != 1 {
		t.Fatalf("embed_queries=false made %d calls", len(recorder.inputs)-1)
	}
}
