package cmd

import (
	"context"
	"os/user"
	"path/filepath"
	"sort"
	"strings"
	"time"

	"github.com/spf13/cobra"
	"github.com/veridian69/cairn/a2a/internal/chat"
	"github.com/veridian69/cairn/a2a/internal/config"
)

var chatAs string

var chatCmd = &cobra.Command{
	Use:   "chat",
	Short: "Open the terminal chat UI",
	Long: "Open the terminal chat UI for the running daemon.\n\n" +
		"Tab/Shift+Tab cycle focus through configured agents and the chat pane. " +
		"The focused agent is highlighted in the status bar. " +
		"On an agent, Enter selects the sending identity and opens the composer; " +
		"Space or p pauses/resumes it. In chat, Enter sends and Esc toggles the " +
		"composer and stream. In the stream, j/k or arrows select, PgUp/PgDn " +
		"scroll, End follows the latest message, t opens a thread, m/M remembers " +
		"or pins. In a thread, j/k, arrows, and PgUp/PgDn scroll; Esc closes it. " +
		"q quits whenever the composer is not active; Ctrl-C quits anywhere. " +
		"Composer commands: /pause name, " +
		"/resume name, /quit, /q.",
	Example: "  a2a chat\n  a2a chat --as claude",
	RunE: func(cmd *cobra.Command, args []string) error {
		url, err := ReadDaemonURL()
		if err != nil {
			return err
		}

		cfg, err := config.Load(filepath.Join(DefaultConfigDir(), "config.yaml"))
		if err != nil {
			return err
		}
		agents := agentNames(cfg)

		dataDir, err := ResolveDataDir()
		if err != nil {
			return err
		}
		memoryConfig, err := loadMemoryConfig()
		if err != nil {
			return err
		}
		svc, err := chat.NewService(
			url, filepath.Join(dataDir, "state.db"),
			memoryConfig.EffectiveMaxItemBytes(),
		)
		if err != nil {
			return err
		}
		defer svc.Close()

		initialIdentity := strings.TrimSpace(chatAs)
		ctx, cancel := context.WithCancel(context.Background())
		defer cancel()

		view, err := chat.NewView(ctx, svc, chat.ViewConfig{
			HumanIdentity:   chatHumanIdentity(initialIdentity, agents),
			AgentNames:      agents,
			InitialIdentity: defaultIdentity(initialIdentity),
			PollInterval:    3 * time.Second,
			TailLimit:       50,
		})
		if err != nil {
			return err
		}
		return view.Run(ctx)
	},
}

func init() {
	chatCmd.Flags().StringVar(&chatAs, "as", "", "initial sending identity")
	rootCmd.AddCommand(chatCmd)
}

func agentNames(cfg *config.Config) []string {
	names := make([]string, 0, len(cfg.Agents))
	for name := range cfg.Agents {
		names = append(names, name)
	}
	sort.Strings(names)
	return names
}

func defaultIdentity(explicit string) string {
	if strings.TrimSpace(explicit) != "" {
		return strings.TrimSpace(explicit)
	}
	return humanIdentity("")
}

func chatHumanIdentity(explicit string, agents []string) string {
	if strings.TrimSpace(explicit) == "" {
		return humanIdentity("")
	}
	for _, agent := range agents {
		if agent == strings.TrimSpace(explicit) {
			return humanIdentity("")
		}
	}
	return strings.TrimSpace(explicit)
}

func humanIdentity(explicit string) string {
	if strings.TrimSpace(explicit) != "" {
		return strings.TrimSpace(explicit)
	}
	current, err := user.Current()
	if err == nil && current != nil && current.Username != "" {
		return current.Username
	}
	return "human"
}
