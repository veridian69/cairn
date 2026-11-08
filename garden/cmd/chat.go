package cmd

import (
	"context"
	"fmt"
	"os/user"
	"path/filepath"
	"sort"
	"strings"
	"time"

	"github.com/spf13/cobra"
	"github.com/veridian69/cairn/garden/internal/chat"
	"github.com/veridian69/cairn/garden/internal/config"
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
		"/resume name, /quit, /q. With --remote the screen follows the Garden gateway named\n" +
		"in garden.json as your cli identity; pause, resume and promote are local-only.",
	Example:           "  garden chat\n  garden chat --as claude\n  garden chat --remote",
	PersistentPreRunE: remotePreRun,
	RunE: func(cmd *cobra.Command, args []string) error {
		remote, err := remoteRequested(cmd)
		if err != nil {
			return err
		}
		if remote {
			return chatRemote()
		}

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
	addRemoteFlag(chatCmd)
	rootCmd.AddCommand(chatCmd)
}

// chatRemote opens the chat screen over the Garden gateway. A remote session sends only as its own
// identity, so the other participants are listed for presence and focusing one cannot send as it.
func chatRemote() error {
	asIdentity := strings.TrimSpace(chatAs)
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()

	session, err := openRemote(ctx)
	if err != nil {
		return err
	}
	if asIdentity != "" && asIdentity != session.Identity {
		_ = session.Close()
		return fmt.Errorf("--as in remote mode can only name your own identity, %s", session.Identity)
	}
	svc := chat.NewRemoteService(session, session.Identity)
	defer svc.Close()

	who, err := session.Who(ctx)
	if err != nil {
		return err
	}
	others := make([]string, 0, len(who.Participants))
	for _, participant := range who.Participants {
		if participant.Name != session.Identity {
			others = append(others, participant.Name)
		}
	}
	view, err := chat.NewView(ctx, svc, chat.ViewConfig{
		HumanIdentity:   session.Identity,
		AgentNames:      others,
		InitialIdentity: session.Identity,
		PollInterval:    3 * time.Second,
		TailLimit:       50,
	})
	if err != nil {
		return err
	}
	return view.Run(ctx)
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
