package cmd

import (
	"errors"
	"fmt"
	"os"
	"path/filepath"

	"github.com/spf13/cobra"
	"github.com/veridian69/cairn/a2a/internal/config"
	"github.com/veridian69/cairn/a2a/internal/memory"
	"github.com/veridian69/cairn/a2a/internal/model"
	"github.com/veridian69/cairn/a2a/internal/provider"
	"github.com/veridian69/cairn/a2a/internal/state"
)

var newCLIEmbedder = provider.NewEmbedder

func loadMemoryConfig() (config.MemoryConfig, error) {
	cfg, err := config.Load(filepath.Join(DefaultConfigDir(), "config.yaml"))
	if err != nil {
		if errors.Is(err, os.ErrNotExist) {
			return config.MemoryConfig{}, nil
		}
		return config.MemoryConfig{}, err
	}
	return cfg.Memory, nil
}

func openMemoryStore() (*memory.Store, string, error) {
	dataDir, err := ResolveDataDir()
	if err != nil {
		return nil, "", err
	}
	if err := ensureDataDir(dataDir); err != nil {
		return nil, "", err
	}
	memoryConfig, err := loadMemoryConfig()
	if err != nil {
		return nil, "", err
	}
	store, err := memory.Open(dataDir, memory.Options{
		MaxItemBytes: memoryConfig.EffectiveMaxItemBytes(),
	})
	if err != nil {
		return nil, "", err
	}
	return store, dataDir, nil
}

func loadRedactedIDs(dataDir string) (map[string]bool, error) {
	db, err := state.Open(filepath.Join(dataDir, "state.db"))
	if err != nil {
		return nil, fmt.Errorf("state: %w", err)
	}
	defer db.Close()
	return db.RedactedIDs()
}

func participantID(dataDir, name string) (string, error) {
	db, err := state.Open(filepath.Join(dataDir, "state.db"))
	if err != nil {
		return "", fmt.Errorf("state: %w", err)
	}
	defer db.Close()
	participant, err := db.GetParticipantByName(name)
	if err != nil {
		return "", fmt.Errorf("unknown participant %q", name)
	}
	return participant.ID, nil
}

func printItem(item memory.Item) {
	pin := ""
	if item.Pinned {
		pin = " (pinned)"
	}
	fmt.Printf("%s%s  %s  %s\n    %s\n",
		model.ShortID(item.ID), pin, item.AuthorName,
		item.SourceCreatedAt.Format("2006-01-02 15:04"), item.Content)
}

var memoryCmd = &cobra.Command{
	Use:   "memory",
	Short: "Inspect and curate the shared memory",
	Long: "List, pin, unpin, or forget individual memory items, or bulk prune/export/\n" +
		"reindex/reset the whole store. See `a2a remember`, `a2a recall`, and\n" +
		"`a2a related` for adding items and searching across them.",
}

var (
	memoryListBy     string
	memoryListLast   int
	memoryListPinned bool
)

var memoryListCmd = &cobra.Command{
	Use:   "list",
	Short: "List memory items",
	Example: "  a2a memory list\n" +
		"  a2a memory list --by claude --last 20\n" +
		"  a2a memory list --pinned",
	RunE: func(cmd *cobra.Command, args []string) error {
		store, dataDir, err := openMemoryStore()
		if err != nil {
			return err
		}
		defer store.Close()
		filter := memory.ListFilter{Last: memoryListLast, PinnedOnly: memoryListPinned}
		if memoryListBy != "" {
			filter.ByAuthorID, err = participantID(dataDir, memoryListBy)
			if err != nil {
				return err
			}
		}
		redacted, err := loadRedactedIDs(dataDir)
		if err != nil {
			return err
		}
		items, err := store.List(filter, redacted)
		if err != nil {
			return err
		}
		if len(items) == 0 {
			fmt.Println("memory is empty")
			return nil
		}
		for _, item := range items {
			printItem(item)
		}
		return nil
	},
}

func memoryIDCommand(
	use, short string,
	action func(*memory.Store, string) error,
	past string,
) *cobra.Command {
	return &cobra.Command{
		Use: use, Short: short, Args: cobra.ExactArgs(1),
		RunE: func(cmd *cobra.Command, args []string) error {
			store, _, err := openMemoryStore()
			if err != nil {
				return err
			}
			defer store.Close()
			id, err := store.ResolveID(args[0])
			if err != nil {
				return err
			}
			if err := action(store, id); err != nil {
				return err
			}
			fmt.Printf("%s %s\n", past, model.ShortID(id))
			return nil
		},
	}
}

var (
	memoryPinCmd    = memoryIDCommand("pin <id>", "Pin a memory item", (*memory.Store).Pin, "pinned")
	memoryUnpinCmd  = memoryIDCommand("unpin <id>", "Unpin a memory item", (*memory.Store).Unpin, "unpinned")
	memoryForgetCmd = memoryIDCommand("forget <id>", "Permanently remove a memory item", (*memory.Store).Forget, "forgot")
)

func init() {
	memoryListCmd.Flags().StringVar(&memoryListBy, "by", "", "filter by source author name")
	memoryListCmd.Flags().IntVar(&memoryListLast, "last", 0, "show only the N most recently curated items")
	memoryListCmd.Flags().BoolVar(&memoryListPinned, "pinned", false, "show only pinned items")
	memoryPinCmd.Example = "  a2a memory pin 3fae21"
	memoryUnpinCmd.Example = "  a2a memory unpin 3fae21"
	memoryForgetCmd.Example = "  a2a memory forget 3fae21"
	memoryCmd.AddCommand(memoryListCmd, memoryPinCmd, memoryUnpinCmd, memoryForgetCmd)
	rootCmd.AddCommand(memoryCmd)
}
