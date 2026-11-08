package cmd

import (
	"context"
	"fmt"
	"os"
	"os/signal"
	"path/filepath"
	"syscall"

	"github.com/spf13/cobra"
	"github.com/veridian69/cairn/garden/internal/garden"
	"github.com/veridian69/cairn/garden/internal/model"
	"github.com/veridian69/cairn/garden/internal/remotecli"
	"github.com/veridian69/cairn/garden/internal/state"
	"github.com/veridian69/cairn/garden/internal/transport"
)

// watchRemoteBacklog is how many recent messages remote watch shows before it starts following.
const watchRemoteBacklog = 50

var watchCmd = &cobra.Command{
	Use:   "watch",
	Short: "Tail the stream live",
	Long: "Tails the message stream from the current position until Ctrl-C. Redacted\n" +
		"messages render as \"[redacted: reason]\" instead of their content. With --remote,\n" +
		"the last 50 messages are shown first, then new ones are polled from the Garden gateway.",
	Example:           "  garden watch\n  garden watch --remote",
	PersistentPreRunE: remotePreRun,
	RunE: func(cmd *cobra.Command, args []string) error {
		remote, err := remoteRequested(cmd)
		if err != nil {
			return err
		}
		if remote {
			return watchRemote()
		}

		url, err := ReadDaemonURL()
		if err != nil {
			return err
		}

		stream, err := transport.NewStream(url)
		if err != nil {
			return fmt.Errorf("connecting: %w", err)
		}
		defer stream.Close()

		ctx, cancel := context.WithCancel(context.Background())
		defer cancel()

		dataDir, err := ResolveDataDir()
		if err != nil {
			return err
		}
		db, err := state.Open(filepath.Join(dataDir, "state.db"))
		if err != nil {
			return err
		}
		defer db.Close()

		err = stream.Subscribe(ctx, func(msg model.Message, seq uint64) error {
			ts := msg.CreatedAt.Local().Format("15:04:05")
			replyInfo := ""
			if msg.ReplyTo != nil {
				r := model.ShortID(*msg.ReplyTo)
				replyInfo = fmt.Sprintf(" (re: %s)", r)
			}
			content := msg.Content
			reason, redacted, redactionErr := db.RedactionReason(msg.ID)
			if redactionErr != nil {
				return redactionErr
			}
			if redacted {
				content = fmt.Sprintf("[redacted: %s]", reason)
			}
			fmt.Printf("[%s] %s%s: %s\n", ts, msg.AuthorName, replyInfo, content)
			return nil
		})
		if err != nil {
			return err
		}

		fmt.Println("watching... (Ctrl+C to stop)")
		sigCh := make(chan os.Signal, 1)
		signal.Notify(sigCh, syscall.SIGINT, syscall.SIGTERM)
		<-sigCh
		fmt.Println()
		return nil
	},
}

// watchRemote shows the recent messages, then follows the room through the Garden gateway until
// Ctrl-C. Notices and link changes go to stderr so the stream stays readable.
func watchRemote() error {
	ctx, cancel := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer cancel()

	session, err := openRemote(ctx)
	if err != nil {
		return err
	}
	defer session.Close()

	window, err := remotecli.Tail(ctx, session, watchRemoteBacklog)
	if err != nil {
		return err
	}
	for _, raw := range window.Messages {
		if err := printWatchRemote(raw); err != nil {
			return err
		}
	}
	fmt.Println("watching... (Ctrl+C to stop)")

	follower := remotecli.Follower{
		Reader:  session,
		Message: printWatchRemote,
		Notice: func(text string) {
			fmt.Fprintln(os.Stderr, text)
		},
		Link: func(up bool, err error) {
			if up {
				fmt.Fprintln(os.Stderr, "reconnected")
				return
			}
			fmt.Fprintf(os.Stderr, "connection lost, retrying: %v\n", err)
		},
	}
	err = follower.Run(ctx, window)
	fmt.Println()
	return err
}

func printWatchRemote(raw garden.Message) error {
	msg, err := remotecli.Convert(raw)
	if err != nil {
		return err
	}
	ts := msg.CreatedAt.Local().Format("15:04:05")
	replyInfo := ""
	if msg.ReplyTo != nil {
		replyInfo = fmt.Sprintf(" (re: %s)", model.ShortID(*msg.ReplyTo))
	}
	fmt.Printf("[%s] %s%s: %s\n", ts, msg.AuthorName, replyInfo, msg.Content)
	return nil
}

func init() {
	addRemoteFlag(watchCmd)
	rootCmd.AddCommand(watchCmd)
}
