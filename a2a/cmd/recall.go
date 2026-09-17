package cmd

import (
	"context"
	"fmt"
	"strings"

	"github.com/spf13/cobra"
	"github.com/veridian69/cairn/a2a/internal/memory"
)

var recallLimit int

var recallCmd = &cobra.Command{
	Use: "recall <query>", Short: "Search shared memory",
	Long: "Searches shared memory with SQLite FTS5/BM25 text matching, plus vector\n" +
		"similarity when memory.embedding is configured. Falls back to text-only search\n" +
		"if the embedder is unavailable.",
	Example: "  a2a recall \"deployment strategy\"\n  a2a recall blue-green releases --limit 5",
	Args:    cobra.MinimumNArgs(1),
	RunE: func(cmd *cobra.Command, args []string) error {
		if recallLimit < 1 || recallLimit > 100 {
			return fmt.Errorf("--limit must be between 1 and 100")
		}
		query := strings.Join(args, " ")
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
		var queryVector []float32
		var modelName string
		if embedding := memoryConfig.Embedding; embedding != nil && embedding.Provider != "" {
			embedder, embedderErr := newCLIEmbedder(
				embedding.Provider, embedding.EffectiveAPIKey(), embedding.Model, "",
			)
			if embedderErr == nil {
				timeout, _ := embedding.TimeoutDuration()
				embedCtx, cancel := context.WithTimeout(cmd.Context(), timeout)
				queryVector, embedderErr = embedder.Embed(
					embedCtx, memory.QueryPrefix(query, memoryConfig.EffectiveQueryBytes()),
				)
				cancel()
				if embedderErr == nil {
					modelName = embedder.ModelName()
				}
			}
			if embedderErr != nil {
				fmt.Printf("(embedding unavailable, text search only: %v)\n", embedderErr)
				queryVector = nil
			}
		}
		items, err := store.Recall(memory.RecallRequest{
			Query: query, QueryVector: queryVector, Model: modelName,
			Limit: recallLimit, QueryBytes: memoryConfig.EffectiveQueryBytes(),
			Exclude: redacted,
		})
		if err != nil {
			return err
		}
		if len(items) == 0 {
			fmt.Println("no matches")
			return nil
		}
		for _, item := range items {
			printItem(item)
		}
		return nil
	},
}

func init() {
	recallCmd.Flags().IntVar(&recallLimit, "limit", 10, "maximum results (1-100)")
	rootCmd.AddCommand(recallCmd)
}
