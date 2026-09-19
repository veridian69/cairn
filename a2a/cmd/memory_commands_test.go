package cmd

import (
	"context"
	"errors"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/veridian69/cairn/a2a/internal/memory"
	"github.com/veridian69/cairn/a2a/internal/model"
	"github.com/veridian69/cairn/a2a/internal/provider"
	"github.com/veridian69/cairn/a2a/internal/state"
	"github.com/veridian69/cairn/a2a/internal/transport"
)

type commandTestEmbedder struct {
	calls             int
	deadlineRemaining time.Duration
}

func (embedder *commandTestEmbedder) Embed(ctx context.Context, _ string) ([]float32, error) {
	embedder.calls++
	if deadline, ok := ctx.Deadline(); ok {
		embedder.deadlineRemaining = time.Until(deadline)
	}
	return []float32{1, 0}, nil
}

func (*commandTestEmbedder) ModelName() string { return "test-model" }

func setupCustomMemory(t *testing.T) (*memory.Store, *state.DB, string) {
	t.Helper()
	home := t.TempDir()
	t.Setenv("HOME", home)
	dataDir := filepath.Join(home, "custom-data")
	if err := os.MkdirAll(DefaultConfigDir(), 0700); err != nil {
		t.Fatal(err)
	}
	config := `agents: {}
stream:
  data_dir: ` + dataDir + `
memory:
  embedding:
    provider: openai
    model: test-model
    api_key: test-key
`
	if err := os.WriteFile(filepath.Join(DefaultConfigDir(), "config.yaml"), []byte(config), 0600); err != nil {
		t.Fatal(err)
	}
	if err := ensureDataDir(dataDir); err != nil {
		t.Fatal(err)
	}
	db, err := state.Open(filepath.Join(dataDir, "state.db"))
	if err != nil {
		t.Fatal(err)
	}
	store, err := memory.Open(dataDir, memory.Options{MaxItemBytes: 4096})
	if err != nil {
		db.Close()
		t.Fatal(err)
	}
	t.Cleanup(func() {
		_ = store.Close()
		_ = db.Close()
	})
	return store, db, dataDir
}

func preserveMemoryCommandGlobals(t *testing.T) {
	t.Helper()
	oldListBy, oldListLast, oldListPinned := memoryListBy, memoryListLast, memoryListPinned
	oldRememberPin := rememberPin
	oldRecallLimit := recallLimit
	oldPruneBefore, oldPruneBy, oldPruneYes := pruneBefore, pruneBy, pruneYes
	oldExportFormat, oldReindexRate, oldResetYes := exportFormat, reindexRate, resetYes
	oldEmbedder := newCLIEmbedder
	t.Cleanup(func() {
		memoryListBy, memoryListLast, memoryListPinned = oldListBy, oldListLast, oldListPinned
		rememberPin = oldRememberPin
		recallLimit = oldRecallLimit
		pruneBefore, pruneBy, pruneYes = oldPruneBefore, oldPruneBy, oldPruneYes
		exportFormat, reindexRate, resetYes = oldExportFormat, oldReindexRate, oldResetYes
		newCLIEmbedder = oldEmbedder
	})
}

func TestMemoryCommandsUseCustomDataDirAndMutateExpectedItems(t *testing.T) {
	preserveMemoryCommandGlobals(t)
	recallCmd.SetContext(context.Background())
	memoryReindexCmd.SetContext(context.Background())
	store, db, dataDir := setupCustomMemory(t)
	alpha, err := db.RegisterParticipant(model.Participant{Name: "alpha", Kind: model.KindAgent})
	if err != nil {
		t.Fatal(err)
	}
	beta, err := db.RegisterParticipant(model.Participant{Name: "beta", Kind: model.KindAgent})
	if err != nil {
		t.Fatal(err)
	}
	alphaItem, err := store.Insert(memory.Item{
		Content: "fractal alpha", AuthorID: alpha.ID, AuthorName: alpha.Name,
		SourceMessageID: "alpha-message", SourceCreatedAt: time.Now().UTC(),
		NominatedBy: "human",
	})
	if err != nil {
		t.Fatal(err)
	}
	betaItem, err := store.Insert(memory.Item{
		Content: "obsolete beta", AuthorID: beta.ID, AuthorName: beta.Name,
		SourceMessageID: "beta-message", SourceCreatedAt: time.Now().UTC(),
		NominatedBy: "human",
	})
	if err != nil {
		t.Fatal(err)
	}

	memoryListBy, memoryListLast, memoryListPinned = "alpha", 0, false
	listOutput := captureStdout(t, func() {
		if err := memoryListCmd.RunE(memoryListCmd, nil); err != nil {
			t.Fatal(err)
		}
	})
	if !strings.Contains(listOutput, "fractal alpha") || strings.Contains(listOutput, "obsolete beta") {
		t.Fatalf("filtered list output:\n%s", listOutput)
	}

	recallLimit = 10
	fakeEmbedder := &commandTestEmbedder{}
	newCLIEmbedder = func(string, string, string, string) (provider.Embedder, error) {
		return fakeEmbedder, nil
	}
	recallOutput := captureStdout(t, func() {
		if err := recallCmd.RunE(recallCmd, []string{"fractal"}); err != nil {
			t.Fatal(err)
		}
	})
	if !strings.Contains(recallOutput, "fractal alpha") || fakeEmbedder.calls != 1 {
		t.Fatalf("recall output=%q embedding calls=%d", recallOutput, fakeEmbedder.calls)
	}
	if fakeEmbedder.deadlineRemaining < 10*time.Second {
		t.Fatalf("CLI recall used query timeout: %v", fakeEmbedder.deadlineRemaining)
	}

	if err := memoryPinCmd.RunE(memoryPinCmd, []string{alphaItem.ID[:8]}); err != nil {
		t.Fatal(err)
	}
	if item, err := store.GetByID(alphaItem.ID); err != nil || !item.Pinned {
		t.Fatalf("pin item=%+v err=%v", item, err)
	}
	if err := memoryUnpinCmd.RunE(memoryUnpinCmd, []string{alphaItem.ID[:8]}); err != nil {
		t.Fatal(err)
	}
	if item, err := store.GetByID(alphaItem.ID); err != nil || item.Pinned {
		t.Fatalf("unpin item=%+v err=%v", item, err)
	}

	exportFormat = "md"
	exportOutput := captureStdout(t, func() {
		if err := memoryExportCmd.RunE(memoryExportCmd, nil); err != nil {
			t.Fatal(err)
		}
	})
	if !strings.Contains(exportOutput, "fractal alpha") || !strings.Contains(exportOutput, "obsolete beta") {
		t.Fatalf("export output:\n%s", exportOutput)
	}

	pruneBy, pruneBefore, pruneYes = "beta", "", false
	if err := memoryPruneCmd.RunE(memoryPruneCmd, nil); err == nil {
		t.Fatal("non-interactive prune without --yes should fail")
	}
	pruneYes = true
	if err := memoryPruneCmd.RunE(memoryPruneCmd, nil); err != nil {
		t.Fatal(err)
	}
	if _, err := store.GetByID(betaItem.ID); !errors.Is(err, memory.ErrNotFound) {
		t.Fatalf("pruned item lookup error = %v", err)
	}

	reindexRate = 100
	if err := memoryReindexCmd.RunE(memoryReindexCmd, nil); err != nil {
		t.Fatal(err)
	}
	vector, embeddingModel, err := store.EmbeddingOf(alphaItem.ID)
	if err != nil || len(vector) != 2 || embeddingModel != "test-model" {
		t.Fatalf("reindex vector=%v model=%q err=%v", vector, embeddingModel, err)
	}

	if err := memoryForgetCmd.RunE(memoryForgetCmd, []string{alphaItem.ID[:8]}); err != nil {
		t.Fatal(err)
	}
	if _, err := store.GetByID(alphaItem.ID); !errors.Is(err, memory.ErrNotFound) {
		t.Fatalf("forgotten item lookup error = %v", err)
	}

	if err := store.Close(); err != nil {
		t.Fatal(err)
	}
	resetYes = true
	if err := memoryResetCmd.RunE(memoryResetCmd, nil); err != nil {
		t.Fatal(err)
	}
	if _, err := os.Stat(filepath.Join(dataDir, "memory.db")); !os.IsNotExist(err) {
		t.Fatalf("custom memory.db survived reset: %v", err)
	}
	if _, err := os.Stat(filepath.Join(DefaultDataDir(), "memory.db")); !os.IsNotExist(err) {
		t.Fatalf("command wrote to default data directory: %v", err)
	}
}

func TestRememberPromotesStreamMessageAndPinsExistingItem(t *testing.T) {
	preserveMemoryCommandGlobals(t)
	rememberCmd.SetContext(context.Background())
	store, _, _ := setupCustomMemory(t)
	server, err := transport.NewServer(t.TempDir())
	if err != nil {
		t.Fatal(err)
	}
	defer server.Stop()
	stream, err := transport.NewStream(server.ClientURL())
	if err != nil {
		t.Fatal(err)
	}
	defer stream.Close()
	if err := os.WriteFile(DaemonURLPath(), []byte(server.ClientURL()), 0600); err != nil {
		t.Fatal(err)
	}
	source := model.NewMessage(
		model.Participant{ID: "agent", Name: "claude", Kind: model.KindAgent},
		"source text unchanged", nil,
	)
	if err := stream.Publish(context.Background(), source); err != nil {
		t.Fatal(err)
	}

	rememberPin = false
	if err := rememberCmd.RunE(rememberCmd, []string{source.ID}); err != nil {
		t.Fatal(err)
	}
	rememberPin = true
	output := captureStdout(t, func() {
		if err := rememberCmd.RunE(rememberCmd, []string{source.ID}); err != nil {
			t.Fatal(err)
		}
	})
	if !strings.Contains(output, "already in memory") || !strings.Contains(output, "pinned") {
		t.Fatalf("second promotion output:\n%s", output)
	}
	items, err := store.List(memory.ListFilter{}, map[string]bool{})
	if err != nil {
		t.Fatal(err)
	}
	if len(items) != 1 || !items[0].Pinned || items[0].Content != source.Content ||
		items[0].AuthorID != source.AuthorID || items[0].SourceMessageID != source.ID {
		t.Fatalf("remembered items = %+v", items)
	}
}
