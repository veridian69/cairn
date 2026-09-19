package cmd

import (
	"context"
	"fmt"
	"os/user"
	"path/filepath"
	"strings"
	"time"

	"github.com/spf13/cobra"
	"github.com/veridian69/cairn/a2a/internal/model"
	"github.com/veridian69/cairn/a2a/internal/state"
	"github.com/veridian69/cairn/a2a/internal/transport"
)

var sayAs string

var sayCmd = &cobra.Command{
	Use:   "say [message]",
	Short: "Send a message to the stream",
	Long: "Publishes a message to the stream as your human participant. With --as, sends\n" +
		"it as an existing agent instead (useful for seeding a conversation on an\n" +
		"agent's behalf) — the message is tagged seeded_by your own identity.",
	Example: "  a2a say \"hello everyone\"\n  a2a say --as claude \"seed this idea\"",
	Args:    cobra.MinimumNArgs(1),
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

		// Resolve author identity
		dataDir, err := ResolveDataDir()
		if err != nil {
			return err
		}
		db, err := state.Open(filepath.Join(dataDir, "state.db"))
		if err != nil {
			return fmt.Errorf("state: %w", err)
		}
		defer db.Close()

		var author model.Participant
		if sayAs != "" {
			author, err = db.GetParticipantByName(sayAs)
			if err != nil {
				return fmt.Errorf("unknown agent %q", sayAs)
			}
		} else {
			u, _ := user.Current()
			name := "human"
			if u != nil {
				name = u.Username
			}
			author, err = db.RegisterParticipant(model.Participant{Name: name, Kind: model.KindHuman})
			if err != nil {
				return fmt.Errorf("register human: %w", err)
			}
		}

		content := strings.Join(args, " ")
		msg := model.NewMessage(author, content, nil)

		// If --as is used, record seeded metadata
		if sayAs != "" {
			humanUser, _ := user.Current()
			humanName := "unknown"
			if humanUser != nil {
				humanName = humanUser.Username
			}
			humanParticipant, regErr := db.RegisterParticipant(model.Participant{Name: humanName, Kind: model.KindHuman})
			if regErr != nil {
				return regErr
			}
			msg.Metadata["seeded_by"] = humanParticipant.ID
			msg.Metadata["authorship_mode"] = "seeded"
		}

		ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
		defer cancel()

		if err := stream.Publish(ctx, msg); err != nil {
			return err
		}
		fmt.Printf("[%s] %s\n", author.Name, content)
		return nil
	},
}

func init() {
	sayCmd.Flags().StringVar(&sayAs, "as", "", "speak as a specific agent")
	rootCmd.AddCommand(sayCmd)
}
