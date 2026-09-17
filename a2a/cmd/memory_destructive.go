package cmd

import (
	"context"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"syscall"
	"time"

	"github.com/spf13/cobra"
	"github.com/veridian69/cairn/a2a/internal/memory"
	"github.com/veridian69/cairn/a2a/internal/model"
	"golang.org/x/term"
)

var (
	pruneBefore string
	pruneBy     string
	pruneYes    bool
)

func parseBefore(value string) (time.Time, error) {
	if parsed, err := time.Parse(time.RFC3339, value); err == nil {
		return parsed, nil
	}
	if parsed, err := time.ParseInLocation("2006-01-02", value, time.UTC); err == nil {
		return parsed, nil
	}
	return time.Time{}, fmt.Errorf("--before must be RFC 3339 or YYYY-MM-DD, got %q", value)
}

func confirmBulk(action string, count int, yes bool) (bool, error) {
	if yes {
		return true, nil
	}
	if !term.IsTerminal(int(os.Stdin.Fd())) {
		return false, fmt.Errorf("refusing to %s %d items without --yes (stdin is not a terminal)", action, count)
	}
	fmt.Printf("%s %d memory items? [y/N] ", action, count)
	var answer string
	_, _ = fmt.Scanln(&answer)
	if answer != "y" && answer != "Y" {
		fmt.Println("aborted")
		return false, nil
	}
	return true, nil
}

func confirmReset(yes bool) (bool, error) {
	if yes {
		return true, nil
	}
	if !term.IsTerminal(int(os.Stdin.Fd())) {
		return false, fmt.Errorf("refusing to delete the entire memory database without --yes (stdin is not a terminal)")
	}
	fmt.Print("delete the entire memory database? [y/N] ")
	var answer string
	_, _ = fmt.Scanln(&answer)
	if answer != "y" && answer != "Y" {
		fmt.Println("aborted")
		return false, nil
	}
	return true, nil
}

var memoryPruneCmd = &cobra.Command{
	Use: "prune", Short: "Bulk-delete memory items by curation date or author",
	Long: "Deletes multiple memory items matched by --before and/or --by. Always confirms\n" +
		"interactively unless --yes is given, and refuses to run non-interactively\n" +
		"(e.g. from a script or cron) without --yes.",
	Example: "  a2a memory prune --before 2026-01-01\n  a2a memory prune --by claude --yes",
	RunE: func(cmd *cobra.Command, args []string) error {
		var filter memory.PruneFilter
		if pruneBefore != "" {
			parsed, err := parseBefore(pruneBefore)
			if err != nil {
				return err
			}
			filter.Before = parsed
		}
		store, dataDir, err := openMemoryStore()
		if err != nil {
			return err
		}
		defer store.Close()
		if pruneBy != "" {
			filter.ByAuthorID, err = participantID(dataDir, pruneBy)
			if err != nil {
				return err
			}
		}
		candidates, err := store.PruneCandidates(filter)
		if err != nil {
			return err
		}
		if len(candidates) == 0 {
			fmt.Println("nothing to prune")
			return nil
		}
		confirmed, err := confirmBulk("delete", len(candidates), pruneYes)
		if err != nil || !confirmed {
			return err
		}
		deleted, err := store.PruneIDs(candidates)
		if err != nil {
			return err
		}
		fmt.Printf("pruned %d items\n", deleted)
		return nil
	},
}

var exportFormat string

var memoryExportCmd = &cobra.Command{
	Use: "export", Short: "Export redaction-filtered memory to stdout",
	Long:    "Dumps every non-redacted memory item to stdout, as JSON or Markdown.",
	Example: "  a2a memory export --format json > memory.json\n  a2a memory export --format md",
	RunE: func(cmd *cobra.Command, args []string) error {
		if exportFormat != "json" && exportFormat != "md" {
			return fmt.Errorf("--format must be json or md")
		}
		store, dataDir, err := openMemoryStore()
		if err != nil {
			return err
		}
		defer store.Close()
		redacted, err := loadRedactedIDs(dataDir)
		if err != nil {
			return err
		}
		items, err := store.List(memory.ListFilter{}, redacted)
		if err != nil {
			return err
		}
		if exportFormat == "json" {
			encoder := json.NewEncoder(os.Stdout)
			encoder.SetIndent("", "  ")
			return encoder.Encode(items)
		}
		for _, item := range items {
			pinned := ""
			if item.Pinned {
				pinned = ", pinned"
			}
			fmt.Printf("- **%s** (%s%s): %s\n", item.AuthorName,
				item.SourceCreatedAt.Format("2006-01-02"), pinned, item.Content)
		}
		return nil
	},
}

var reindexRate int

var memoryReindexCmd = &cobra.Command{
	Use: "reindex", Short: "Embed items with missing or stale vectors",
	Long: "Embeds memory items with a missing or stale vector using the memory.embedding\n" +
		"provider configured in config.yaml. Fails if no embedding provider is configured.",
	Example: "  a2a memory reindex\n  a2a memory reindex --rate 25",
	RunE: func(cmd *cobra.Command, args []string) error {
		if reindexRate < 1 || reindexRate > 100 {
			return fmt.Errorf("--rate must be between 1 and 100")
		}
		memoryConfig, err := loadMemoryConfig()
		if err != nil {
			return err
		}
		embedding := memoryConfig.Embedding
		if embedding == nil || embedding.Provider == "" {
			return fmt.Errorf("no embedding provider configured (memory.embedding)")
		}
		embedder, err := newCLIEmbedder(
			embedding.Provider, embedding.EffectiveAPIKey(), embedding.Model, "",
		)
		if err != nil {
			return err
		}
		timeout, err := embedding.TimeoutDuration()
		if err != nil {
			return err
		}
		store, _, err := openMemoryStore()
		if err != nil {
			return err
		}
		defer store.Close()
		limiter := time.NewTicker(time.Second / time.Duration(reindexRate))
		defer limiter.Stop()
		done, failed := 0, 0
		var cursor int64
		for {
			pending, nextCursor, err := store.PendingEmbeddingsPage(
				embedder.ModelName(), cursor, 500,
			)
			if err != nil {
				return err
			}
			if len(pending) == 0 {
				break
			}
			for _, item := range pending {
				select {
				case <-cmd.Context().Done():
					return cmd.Context().Err()
				case <-limiter.C:
				}
				callCtx, cancel := context.WithTimeout(cmd.Context(), timeout)
				vector, embedErr := embedder.Embed(callCtx, item.Content)
				cancel()
				if embedErr != nil {
					failed++
					fmt.Printf("failed %s: %v\n", model.ShortID(item.ID), embedErr)
					continue
				}
				if err := store.SetEmbedding(item.ID, vector, embedder.ModelName()); err != nil {
					failed++
					fmt.Printf("failed %s: %v\n", model.ShortID(item.ID), err)
					continue
				}
				done++
			}
			if len(pending) < 500 || nextCursor <= cursor {
				break
			}
			cursor = nextCursor
		}
		if done+failed == 0 {
			fmt.Println("nothing to reindex")
			return nil
		}
		fmt.Printf("reindexed %d items (%d failed)\n", done, failed)
		return nil
	},
}

var resetYes bool

var memoryResetCmd = &cobra.Command{
	Use: "reset", Short: "Delete the entire memory database",
	Long: "Deletes memory.db and its WAL/SHM files entirely. Requires the daemon and\n" +
		"every other memory-touching command to be stopped first: it takes an exclusive\n" +
		"lock on memory.db.lock and fails loudly if a shared lock is held.",
	Example: "  a2a memory reset --yes",
	RunE: func(cmd *cobra.Command, args []string) error {
		dataDir, err := ResolveDataDir()
		if err != nil {
			return err
		}
		if err := ensureDataDir(dataDir); err != nil {
			return err
		}
		confirmed, err := confirmReset(resetYes)
		if err != nil || !confirmed {
			return err
		}
		lockPath := filepath.Join(dataDir, "memory.db.lock")
		lock, err := os.OpenFile(lockPath, os.O_CREATE|os.O_RDWR, 0600)
		if err != nil {
			return err
		}
		defer lock.Close()
		if err := syscall.Flock(int(lock.Fd()), syscall.LOCK_EX|syscall.LOCK_NB); err != nil {
			return fmt.Errorf("memory database is in use (shared lock held on %s): stop the daemon and any memory commands first", lockPath)
		}
		defer syscall.Flock(int(lock.Fd()), syscall.LOCK_UN)
		for _, suffix := range []string{"", "-wal", "-shm"} {
			path := filepath.Join(dataDir, "memory.db"+suffix)
			if err := os.Remove(path); err != nil && !os.IsNotExist(err) {
				return err
			}
		}
		fmt.Println("memory reset")
		return nil
	},
}

func init() {
	memoryPruneCmd.Flags().StringVar(&pruneBefore, "before", "", "delete items curated before this date")
	memoryPruneCmd.Flags().StringVar(&pruneBy, "by", "", "delete items authored by this participant")
	memoryPruneCmd.Flags().BoolVar(&pruneYes, "yes", false, "skip confirmation")
	memoryExportCmd.Flags().StringVar(&exportFormat, "format", "json", "output format: json or md")
	memoryReindexCmd.Flags().IntVar(&reindexRate, "rate", 10, "embedding calls per second (1-100)")
	memoryResetCmd.Flags().BoolVar(&resetYes, "yes", false, "skip confirmation")
	memoryCmd.AddCommand(memoryPruneCmd, memoryExportCmd, memoryReindexCmd, memoryResetCmd)
}
