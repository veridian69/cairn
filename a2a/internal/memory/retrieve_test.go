package memory

import (
	"strings"
	"testing"
	"time"
)

func TestRecallExcludesSourcesAndRendersPinnedFirstWithinBudget(t *testing.T) {
	store, _ := openTestStore(t, Options{})
	pinned, err := store.Insert(testItem("pinned-source", "fractal anchor"))
	if err != nil {
		t.Fatal(err)
	}
	if err := store.Pin(pinned.ID); err != nil {
		t.Fatal(err)
	}
	if _, err := store.Insert(testItem("visible-source", "fractal visible")); err != nil {
		t.Fatal(err)
	}
	if _, err := store.Insert(testItem("free-source", "fractal recalled")); err != nil {
		t.Fatal(err)
	}
	exclude := map[string]bool{"visible-source": true}
	pinnedItems, err := store.Pinned(5, exclude)
	if err != nil {
		t.Fatal(err)
	}
	recalled, err := store.Recall(RecallRequest{
		Query: "fractal", Limit: 3, QueryBytes: 1024, Exclude: exclude,
	})
	if err != nil {
		t.Fatal(err)
	}
	render := []RenderItem{{Item: pinnedItems[0], Pinned: true}}
	for _, item := range recalled {
		if item.ID != pinned.ID {
			render = append(render, RenderItem{Item: item})
		}
	}
	block, _ := RenderBlock(render, 8192, time.Now().UTC())
	if !strings.Contains(block, "(pinned)") ||
		!strings.Contains(block, "fractal recalled") ||
		strings.Contains(block, "fractal visible") ||
		len(block) > 8192 {
		t.Fatalf("bad block:\n%s", block)
	}
}

func TestRenderEscapesDelimitersAndDropsTailWithinByteBudget(t *testing.T) {
	now := time.Date(2026, 7, 26, 12, 0, 0, 0, time.UTC)
	items := []RenderItem{
		{Item: testItem("one", `keep [memory] and [/memory] literal`), Pinned: true},
		{Item: testItem("two", strings.Repeat("tail", 100))},
	}
	for index := range items {
		items[index].Item.SourceCreatedAt = now.Add(-time.Hour)
	}
	block, dropped := RenderBlock(items, 260, now)
	if dropped != 1 {
		t.Fatalf("dropped = %d, want 1", dropped)
	}
	if len(block) > 260 {
		t.Fatalf("block is %d bytes, budget is 260", len(block))
	}
	if strings.Contains(strings.TrimPrefix(block, "[memory]"), "[memory]") ||
		strings.Contains(strings.TrimSuffix(block, "[/memory]"), "[/memory]") {
		t.Fatalf("unescaped delimiter survived:\n%s", block)
	}
	if !strings.Contains(block, `[\memory]`) || !strings.Contains(block, `[\/memory]`) {
		t.Fatalf("escaped delimiters missing:\n%s", block)
	}
}
