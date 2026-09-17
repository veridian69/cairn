package cmd

import (
	"context"
	"errors"
	"fmt"
	"time"

	"github.com/spf13/cobra"
	"github.com/veridian69/cairn/a2a/internal/memory"
	"github.com/veridian69/cairn/a2a/internal/model"
	"github.com/veridian69/cairn/a2a/internal/transport"
)

const (
	relatedDialTimeout = 2 * time.Second
	causalDepth        = 8
	temporalWindow     = 30 * time.Minute
	semanticK          = 5
)

var relatedCmd = &cobra.Command{
	Use: "related <id>", Short: "Show related memory items",
	Long: "Finds memory items related to a message or memory ID across three relations:\n" +
		"causal (reply-chain ancestors/descendants), temporal (within a 30-minute\n" +
		"window), and semantic (embedding similarity, when configured). Works against\n" +
		"stored items alone if the daemon is unreachable, with reduced causal reach.",
	Example: "  a2a related 3fae21",
	Args:    cobra.ExactArgs(1),
	RunE: func(cmd *cobra.Command, args []string) error {
		store, dataDir, err := openMemoryStore()
		if err != nil {
			return err
		}
		defer store.Close()
		redacted, err := loadRedactedIDs(dataDir)
		if err != nil {
			return err
		}
		memoryConfig, err := loadMemoryConfig()
		if err != nil {
			return err
		}
		currentEmbeddingModel := ""
		if memoryConfig.Embedding != nil && memoryConfig.Embedding.Provider != "" {
			currentEmbeddingModel = memoryConfig.Embedding.Model
		}
		var stream *transport.Stream
		if url, readErr := ReadDaemonURL(); readErr == nil {
			stream, _ = transport.NewStreamWithTimeout(url, relatedDialTimeout)
			if stream != nil {
				defer stream.Close()
			}
		}
		if stream == nil {
			fmt.Println("(daemon unreachable: offline mode, causal reach limited to stored items)")
		}

		inputID := args[0]
		anchorMessageID := inputID
		var anchor *memory.Item
		resolved, resolveErr := store.ResolveID(inputID)
		switch {
		case resolveErr == nil:
			item, getErr := store.GetByID(resolved)
			if getErr != nil {
				return getErr
			}
			anchor = &item
			anchorMessageID = item.SourceMessageID
		case errors.Is(resolveErr, memory.ErrAmbiguousID):
			return resolveErr
		case !errors.Is(resolveErr, memory.ErrNotFound):
			return resolveErr
		default:
			items, sourceErr := store.ItemsBySource(inputID, redacted)
			if sourceErr != nil {
				return sourceErr
			}
			if len(items) > 0 {
				anchor = &items[0]
			}
		}

		type hit struct {
			item     memory.Item
			relation string
		}
		var hits []hit
		add := func(items []memory.Item, relation string) {
			for _, item := range items {
				hits = append(hits, hit{item: item, relation: relation})
			}
		}
		descendants, err := store.Descendants(anchorMessageID, redacted)
		if err != nil {
			return err
		}
		add(descendants, "causal")

		replyTo := anchorReplyTo(cmd.Context(), anchor, stream, anchorMessageID)
		for depth := 0; replyTo != nil && depth < causalDepth; depth++ {
			items, err := store.ItemsBySource(*replyTo, redacted)
			if err != nil {
				return err
			}
			add(items, "causal")
			replyTo = nextReplyTo(cmd.Context(), items, stream, *replyTo)
		}

		var anchorTime *time.Time
		if anchor != nil {
			anchorTime = &anchor.SourceCreatedAt
		} else if stream != nil {
			fetchCtx, cancel := context.WithTimeout(cmd.Context(), 10*time.Second)
			message, fetchErr := stream.GetByID(fetchCtx, anchorMessageID)
			cancel()
			if fetchErr == nil && message != nil {
				anchorTime = &message.CreatedAt
			}
		}
		if anchorTime != nil {
			excludeID := ""
			if anchor != nil {
				excludeID = anchor.ID
			}
			items, err := store.Temporal(*anchorTime, temporalWindow, excludeID, redacted)
			if err != nil {
				return err
			}
			add(items, "temporal")
		}
		if anchor != nil {
			vector, modelName, embedErr := store.EmbeddingOf(anchor.ID)
			if embedErr != nil {
				return embedErr
			}
			if vector != nil && modelName == currentEmbeddingModel {
				items, err := store.SemanticNeighbours(
					vector, modelName, anchor.ID, semanticK, redacted,
				)
				if err != nil {
					return err
				}
				add(items, "semantic")
			}
		}

		labels := make(map[string][]string)
		itemsByID := make(map[string]memory.Item)
		var order []string
		for _, hit := range hits {
			if _, exists := itemsByID[hit.item.ID]; !exists {
				itemsByID[hit.item.ID] = hit.item
				order = append(order, hit.item.ID)
			}
			labels[hit.item.ID] = appendUnique(labels[hit.item.ID], hit.relation)
		}
		if len(order) == 0 {
			if anchor == nil && stream == nil {
				fmt.Printf("%s is not in memory and the daemon is unreachable — nothing to relate it to\n",
					model.ShortID(inputID))
			} else {
				fmt.Println("no related items")
			}
			return nil
		}
		for _, id := range order {
			fmt.Printf("[%s] ", joinLabels(labels[id]))
			printItem(itemsByID[id])
		}
		return nil
	},
}

func anchorReplyTo(
	ctx context.Context,
	anchor *memory.Item,
	stream *transport.Stream,
	messageID string,
) *string {
	if anchor != nil {
		return anchor.ReplyTo
	}
	return streamReplyTo(ctx, stream, messageID)
}

func nextReplyTo(
	ctx context.Context,
	items []memory.Item,
	stream *transport.Stream,
	messageID string,
) *string {
	if len(items) > 0 {
		return items[0].ReplyTo
	}
	return streamReplyTo(ctx, stream, messageID)
}

func streamReplyTo(ctx context.Context, stream *transport.Stream, messageID string) *string {
	if stream == nil {
		return nil
	}
	ctx, cancel := context.WithTimeout(ctx, 10*time.Second)
	defer cancel()
	message, err := stream.GetByID(ctx, messageID)
	if err != nil || message == nil {
		return nil
	}
	return message.ReplyTo
}

func appendUnique(values []string, value string) []string {
	for _, existing := range values {
		if existing == value {
			return values
		}
	}
	return append(values, value)
}

func joinLabels(values []string) string {
	result := values[0]
	for _, value := range values[1:] {
		result += "+" + value
	}
	return result
}

func init() {
	rootCmd.AddCommand(relatedCmd)
}
