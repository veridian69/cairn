package memory

import "testing"

func TestSemanticNeighboursApplyRedactionBeforeLimit(t *testing.T) {
	store, _ := openTestStore(t, Options{})
	anchor, _ := store.Insert(testItem("anchor", "anchor"))
	hiddenOne, _ := store.Insert(testItem("hidden", "hidden one"))
	hiddenTwo, _ := store.Insert(testItem("hidden", "hidden two"))
	visible, _ := store.Insert(testItem("visible", "visible"))
	_ = store.SetEmbedding(anchor.ID, []float32{1, 0}, "model")
	_ = store.SetEmbedding(hiddenOne.ID, []float32{0.99, 0.01}, "model")
	_ = store.SetEmbedding(hiddenTwo.ID, []float32{0.98, 0.02}, "model")
	_ = store.SetEmbedding(visible.ID, []float32{0.8, 0.2}, "model")
	items, err := store.SemanticNeighbours(
		[]float32{1, 0}, "model", anchor.ID, 1, map[string]bool{"hidden": true},
	)
	if err != nil {
		t.Fatal(err)
	}
	if len(items) != 1 || items[0].ID != visible.ID {
		t.Fatalf("neighbours = %+v", items)
	}
}
