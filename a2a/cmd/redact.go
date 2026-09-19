package cmd

import (
	"fmt"
	"os/user"
	"path/filepath"

	"github.com/spf13/cobra"
	"github.com/veridian69/cairn/a2a/internal/model"
	"github.com/veridian69/cairn/a2a/internal/state"
)

var redactReason string

var redactCmd = &cobra.Command{
	Use:   "redact [message_id]",
	Short: "Redact a message",
	Long: "Marks a message redacted in the state DB. This hides its content from watch,\n" +
		"history, chat, and memory recall/export — it does not delete the underlying\n" +
		"stream record or any memory item already derived from it. `history --raw`\n" +
		"deliberately bypasses redaction to show the original content.",
	Example: "  a2a redact 3fae21\n  a2a redact 3fae21 --reason operator-request",
	Args:    cobra.ExactArgs(1),
	RunE: func(cmd *cobra.Command, args []string) error {
		dataDir, err := ResolveDataDir()
		if err != nil {
			return err
		}
		db, err := state.Open(filepath.Join(dataDir, "state.db"))
		if err != nil {
			return err
		}
		defer db.Close()

		u, _ := user.Current()
		name := "operator"
		if u != nil && u.Username != "" {
			name = u.Username
		}
		actor, err := db.RegisterParticipant(model.Participant{Name: name, Kind: model.KindHuman})
		if err != nil {
			return err
		}

		if err := db.Redact(args[0], redactReason, actor.ID); err != nil {
			return err
		}
		fmt.Printf("redacted %s (%s)\n", args[0], redactReason)
		return nil
	},
}

func init() {
	redactCmd.Flags().StringVar(&redactReason, "reason", "operator-request", "reason for redaction")
	rootCmd.AddCommand(redactCmd)
}
