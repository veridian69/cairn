package daemon

import (
	"context"
	"log"
	"time"

	"github.com/veridian69/cairn/a2a/internal/model"
)

const (
	embedRatePerSecond = 10
	embedPageSize      = 500
)

func (d *Daemon) runEmbedWorker(ctx context.Context) {
	defer d.wg.Done()
	modelName := d.embedder.ModelName()
	attempted := make(map[string]bool)

	scan := func() {
		limiter := time.NewTicker(time.Second / embedRatePerSecond)
		defer limiter.Stop()
		var cursor int64
		for {
			items, nextCursor, err := d.memory.PendingEmbeddingsPage(
				modelName, cursor, embedPageSize,
			)
			if err != nil {
				log.Printf("embed scan: %v", err)
				return
			}
			if len(items) == 0 {
				return
			}
			for _, item := range items {
				if attempted[item.ID] {
					continue
				}
				attempted[item.ID] = true
				select {
				case <-ctx.Done():
					return
				case <-limiter.C:
				}
				callCtx, cancel := context.WithTimeout(ctx, d.embedTimeout)
				vector, err := d.embedder.Embed(callCtx, item.Content)
				cancel()
				if err != nil {
					log.Printf("embed %s: %v", model.ShortID(item.ID), err)
					continue
				}
				if err := d.memory.SetEmbedding(item.ID, vector, modelName); err != nil {
					log.Printf("embed store %s: %v", model.ShortID(item.ID), err)
				}
			}
			if len(items) < embedPageSize || nextCursor <= cursor {
				return
			}
			cursor = nextCursor
		}
	}

	scan()
	ticker := time.NewTicker(d.embedScanInterval)
	defer ticker.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-d.embedWake:
			scan()
		case <-ticker.C:
			scan()
		}
	}
}
