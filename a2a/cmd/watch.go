package cmd

import (
	"context"
	"fmt"
	"os"
	"os/signal"
	"path/filepath"
	"syscall"

	"github.com/spf13/cobra"
	"github.com/veridian69/cairn/a2a/internal/model"
	"github.com/veridian69/cairn/a2a/internal/state"
	"github.com/veridian69/cairn/a2a/internal/transport"
)

var watchCmd = &cobra.Command{
	Use:   "watch",
	Short: "Tail the stream live",
	Long: "Tails the message stream from the current position until Ctrl-C. Redacted\n" +
		"messages render as \"[redacted: reason]\" instead of their content.",
	Example: "  a2a watch",
	RunE: func(cmd *cobra.Command, args []string) error {
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

func init() {
	rootCmd.AddCommand(watchCmd)
}
