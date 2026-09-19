package cmd

import (
	"context"
	"fmt"
	"os/user"
	"path/filepath"
	"time"

	"github.com/spf13/cobra"
	"github.com/veridian69/cairn/a2a/internal/memory"
	"github.com/veridian69/cairn/a2a/internal/model"
	"github.com/veridian69/cairn/a2a/internal/state"
	"github.com/veridian69/cairn/a2a/internal/transport"
)

var rememberPin bool

var rememberCmd = &cobra.Command{
	Use: "remember <message-id>", Short: "Promote a message into shared memory",
	Long: "Fetches a message from the stream and inserts it as a curated memory item.\n" +
		"Needs the daemon running, since that's where the source message is fetched\n" +
		"from. --pin marks the item to always be injected into agent context, ahead of\n" +
		"ordinary recall.",
	Example: "  a2a remember 3fae21\n  a2a remember 3fae21 --pin",
	Args:    cobra.ExactArgs(1),
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
		ctx, cancel := context.WithTimeout(cmd.Context(), 10*time.Second)
		defer cancel()
		message, err := stream.GetByID(ctx, args[0])
		if err != nil {
			return fmt.Errorf("fetching message: %w", err)
		}
		if message == nil {
			return fmt.Errorf("message %s not found in stream", args[0])
		}
		store, dataDir, err := openMemoryStore()
		if err != nil {
			return err
		}
		defer store.Close()
		db, err := state.Open(filepath.Join(dataDir, "state.db"))
		if err != nil {
			return fmt.Errorf("state: %w", err)
		}
		current, _ := user.Current()
		name := "human"
		if current != nil && current.Username != "" {
			name = current.Username
		}
		nominator, err := db.RegisterParticipant(model.Participant{Name: name, Kind: model.KindHuman})
		_ = db.Close()
		if err != nil {
			return fmt.Errorf("register nominator: %w", err)
		}
		result, err := store.Insert(memory.Item{
			Content: message.Content, AuthorID: message.AuthorID,
			AuthorName: message.AuthorName, SourceMessageID: message.ID,
			SourceCreatedAt: message.CreatedAt, ReplyTo: message.ReplyTo,
			NominatedBy: nominator.ID, Pinned: rememberPin,
		})
		if err != nil {
			return err
		}
		switch {
		case result.Existed && rememberPin:
			fmt.Printf("already in memory as %s — pinned\n", model.ShortID(result.ID))
		case result.Existed:
			fmt.Printf("already in memory as %s\n", model.ShortID(result.ID))
		default:
			fmt.Printf("remembered %s (from %s)\n", model.ShortID(result.ID), message.AuthorName)
		}
		return nil
	},
}

func init() {
	rememberCmd.Flags().BoolVar(&rememberPin, "pin", false, "pin the item so it is always injected")
	rootCmd.AddCommand(rememberCmd)
}
