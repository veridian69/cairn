package cmd

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"os/signal"
	"path/filepath"
	"strconv"
	"syscall"
	"time"

	"github.com/spf13/cobra"
	"github.com/veridian69/cairn/a2a/internal/model"
	"github.com/veridian69/cairn/a2a/internal/state"
	"github.com/veridian69/cairn/a2a/internal/transport"
)

var (
	accountantLast   int
	accountantJSON   bool
	accountantFollow bool
)

var accountantCmd = &cobra.Command{
	Use:   "accountant",
	Short: "View structured proposal records",
	Long:  "Shows accountant records attached to proposal messages without conversational text.",
	Example: "  a2a accountant\n" +
		"  a2a accountant --last 200\n" +
		"  a2a accountant --follow\n" +
		"  a2a accountant --json",
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

		baseCtx := cmd.Context()
		if baseCtx == nil {
			baseCtx = context.Background()
		}
		ctx, cancel := context.WithTimeout(baseCtx, 10*time.Second)
		entries, err := stream.TailWithSeq(ctx, accountantLast)
		defer cancel()
		if err != nil {
			return err
		}

		dataDir, err := ResolveDataDir()
		if err != nil {
			return err
		}
		db, err := state.Open(filepath.Join(dataDir, "state.db"))
		if err != nil {
			return err
		}
		defer db.Close()
		reasons, err := db.RedactionReasons()
		if err != nil {
			return err
		}

		count := 0
		var lastSeq uint64
		for _, entry := range entries {
			lastSeq = entry.Seq
			if entry.Message.Accountant == nil {
				continue
			}
			if _, redacted := reasons[entry.Message.ID]; redacted {
				continue
			}
			if err := writeAccountant(cmd, entry.Message); err != nil {
				return err
			}
			count++
		}
		if !accountantFollow {
			if count == 0 && !accountantJSON {
				fmt.Fprintln(cmd.OutOrStdout(), "(no accountant records)")
			}
			return nil
		}

		followCtx, stop := signal.NotifyContext(baseCtx, os.Interrupt, syscall.SIGTERM)
		defer stop()
		if err := stream.SubscribeFromSeq(followCtx, lastSeq+1, func(message model.Message, _ uint64) error {
			if message.Accountant == nil {
				return nil
			}
			if _, redacted, err := db.RedactionReason(message.ID); err != nil {
				return err
			} else if redacted {
				return nil
			}
			return writeAccountant(cmd, message)
		}); err != nil {
			if errors.Is(err, context.Canceled) && followCtx.Err() != nil {
				return nil
			}
			return err
		}
		<-followCtx.Done()
		return nil
	},
}

func writeAccountant(cmd *cobra.Command, message model.Message) error {
	if accountantJSON {
		return writeAccountantJSON(cmd, message)
	}
	writeAccountantText(cmd, message)
	return nil
}

/*
Keep text rendering intentionally compact. The conversation explains the
proposal; this view is the decision ledger.
*/
func writeAccountantText(cmd *cobra.Command, message model.Message) {
	record := message.Accountant
	header := fmt.Sprintf("[%s] %s %s [%s]: %s\n",
		message.CreatedAt.Local().Format("2006-01-02 15:04:05"),
		model.ShortID(message.ID),
		message.AuthorName,
		record.Probability,
		record.Idea,
	)
	fmt.Fprint(cmd.OutOrStdout(), header)
	if record.CapitalRequiredCHF != nil {
		fmt.Fprintf(cmd.OutOrStdout(), "  capital: CHF %s\n",
			strconv.FormatFloat(*record.CapitalRequiredCHF, 'f', -1, 64))
	}
	if record.NextExperiment != "" {
		fmt.Fprintf(cmd.OutOrStdout(), "  next: %s\n", record.NextExperiment)
	}
}

type accountantLogEntry struct {
	MessageID  string                  `json:"message_id"`
	Author     string                  `json:"author"`
	CreatedAt  time.Time               `json:"created_at"`
	ReplyTo    *string                 `json:"reply_to,omitempty"`
	Accountant *model.AccountantRecord `json:"accountant"`
}

func writeAccountantJSON(cmd *cobra.Command, message model.Message) error {
	return json.NewEncoder(cmd.OutOrStdout()).Encode(accountantLogEntry{
		MessageID:  message.ID,
		Author:     message.AuthorName,
		CreatedAt:  message.CreatedAt,
		ReplyTo:    message.ReplyTo,
		Accountant: message.Accountant,
	})
}

func init() {
	accountantCmd.Flags().IntVar(&accountantLast, "last", 50, "messages to inspect")
	accountantCmd.Flags().BoolVar(&accountantJSON, "json", false, "write newline-delimited JSON")
	accountantCmd.Flags().BoolVar(&accountantFollow, "follow", false, "follow new accountant records")
	rootCmd.AddCommand(accountantCmd)
}
