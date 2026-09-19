package memory

import (
	"context"
	"log"
	"time"

	"github.com/veridian69/cairn/a2a/internal/model"
	"github.com/veridian69/cairn/a2a/internal/provider"
)

type Injector struct {
	Store         *Store
	Embedder      provider.Embedder
	EmbedQueries  bool
	QueryTimeout  time.Duration
	RecallLimit   int
	MaxPinned     int
	QueryBytes    int
	MaxBlockBytes int
}

func (injector *Injector) InjectionBlock(
	ctx context.Context,
	trigger model.Message,
	visibleIDs, redactedIDs map[string]bool,
) string {
	if injector == nil || injector.Store == nil {
		return ""
	}
	exclude := make(map[string]bool, len(visibleIDs)+len(redactedIDs))
	for id := range visibleIDs {
		exclude[id] = true
	}
	for id := range redactedIDs {
		exclude[id] = true
	}
	var queryVector []float32
	var modelName string
	if injector.Embedder != nil && injector.EmbedQueries {
		embedCtx, cancel := context.WithTimeout(ctx, injector.QueryTimeout)
		vector, err := injector.Embedder.Embed(
			embedCtx, QueryPrefix(trigger.Content, injector.QueryBytes),
		)
		cancel()
		if err != nil {
			log.Printf("memory: query embedding failed, BM25-only this turn: %v", err)
		} else if len(vector) > 0 {
			queryVector = vector
			modelName = injector.Embedder.ModelName()
		}
	}
	pinned, err := injector.Store.Pinned(injector.MaxPinned, exclude)
	if err != nil {
		log.Printf("memory: pinned lookup failed: %v", err)
		pinned = nil
	}
	recalled, err := injector.Store.Recall(RecallRequest{
		Query: trigger.Content, QueryVector: queryVector, Model: modelName,
		Limit: injector.RecallLimit, QueryBytes: injector.QueryBytes,
		Exclude: exclude,
	})
	if err != nil {
		log.Printf("memory: recall failed, injecting pinned only: %v", err)
		recalled = nil
	}
	pinnedIDs := make(map[string]bool, len(pinned))
	items := make([]RenderItem, 0, len(pinned)+len(recalled))
	for _, item := range pinned {
		pinnedIDs[item.ID] = true
		items = append(items, RenderItem{Item: item, Pinned: true})
	}
	for _, item := range recalled {
		if !pinnedIDs[item.ID] {
			items = append(items, RenderItem{Item: item})
		}
	}
	block, dropped := RenderBlock(items, injector.MaxBlockBytes, time.Now().UTC())
	if dropped > 0 {
		log.Printf("memory: dropped %d items to fit block budget", dropped)
	}
	return block
}
