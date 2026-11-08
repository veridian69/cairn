package cmd

import (
	"context"
	"errors"
	"fmt"
	"os"
	"os/user"
	"path/filepath"
	"strings"
	"time"

	"github.com/spf13/cobra"
	"github.com/veridian69/cairn/garden/internal/garden"
	"github.com/veridian69/cairn/garden/internal/model"
	"github.com/veridian69/cairn/garden/internal/state"
	"github.com/veridian69/cairn/garden/internal/transport"
)

var (
	sayAs string
	sayTo []string
)

var sayCmd = &cobra.Command{
	Use:   "say [message]",
	Short: "Send a message to the stream",
	Long: "Publishes a message to the stream as your human participant. With --as, sends\n" +
		"it as an existing agent instead (useful for seeding a conversation on an\n" +
		"agent's behalf) — the message is tagged seeded_by your own identity.",
	Example: "  garden say \"hello everyone\"\n  garden say --as claude \"seed this idea\"\n" +
		"  garden say --remote --to claude \"over the Garden gateway\"",
	Args:              cobra.MinimumNArgs(1),
	PersistentPreRunE: remotePreRun,
	RunE: func(cmd *cobra.Command, args []string) error {
		remote, err := remoteRequested(cmd)
		if err != nil {
			return err
		}
		if remote {
			return sayRemote(args)
		}
		if len(sayTo) > 0 {
			return errors.New("--to applies only with --remote")
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

// sayRemote posts the message through the Garden gateway as the session's own identity.
func sayRemote(args []string) error {
	if sayAs != "" {
		return errors.New("--as is not available in remote mode; remote messages are sent as your Garden identity")
	}
	content := strings.Join(args, " ")

	// The session opens without the call deadline: unlocking a sealed identity waits for the
	// passphrase, which must not use up the time for the call (Val's review, 8 November 2026).
	// Each gateway request is still bounded by the client's own HTTP timeout.
	open, stop := context.WithCancel(context.Background())
	defer stop()
	session, err := openRemote(open)
	if err != nil {
		return err
	}
	defer session.Close()
	ctx, cancel := context.WithTimeout(open, remoteCallTimeout)
	defer cancel()

	result, err := session.Send(ctx, garden.SendArgs{Content: content, Recipients: sayTo})
	if err != nil {
		return err
	}
	fmt.Printf("[%s] %s\n", session.Identity, content)
	fmt.Printf("stored as %s, sequence %d\n", model.ShortID(result.ID), result.Sequence)
	for _, delivery := range result.Deliveries {
		line := fmt.Sprintf("  %s: %s", delivery.Recipient, delivery.Delivery)
		if delivery.Reason != "" {
			line += " (" + delivery.Reason + ")"
		}
		fmt.Println(line)
	}
	if result.Notice != "" {
		fmt.Fprintln(os.Stderr, result.Notice)
	}
	return nil
}

func init() {
	sayCmd.Flags().StringVar(&sayAs, "as", "", "speak as a specific agent")
	sayCmd.Flags().StringArrayVar(&sayTo, "to", nil, "recipient for a remote message (repeatable; --remote only)")
	addRemoteFlag(sayCmd)
	rootCmd.AddCommand(sayCmd)
}
