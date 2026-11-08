package cmd

import (
	"context"
	"errors"
	"fmt"
	"path/filepath"
	"time"

	"github.com/spf13/cobra"
	"github.com/veridian69/cairn/garden/internal/model"
	"github.com/veridian69/cairn/garden/internal/remotecli"
	"github.com/veridian69/cairn/garden/internal/state"
	"github.com/veridian69/cairn/garden/internal/transport"
)

var (
	historyLast   int
	historyBy     string
	historyRaw    bool
	historyThread string
)

var historyCmd = &cobra.Command{
	Use:   "history",
	Short: "View conversation history",
	Long: "Replays past stream messages. --last, --by, and --raw filter a flat replay;\n" +
		"--thread instead walks the full reply chain (ancestors then descendants) from\n" +
		"a given message ID, ignoring --last/--by.",
	Example: "  garden history --last 200\n" +
		"  garden history --by claude\n" +
		"  garden history --thread 3fae21\n" +
		"  garden history --raw\n" +
		"  garden history --remote --last 100",
	PersistentPreRunE: remotePreRun,
	RunE: func(cmd *cobra.Command, args []string) error {
		remote, err := remoteRequested(cmd)
		if err != nil {
			return err
		}
		if remote {
			return historyRemote()
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

		ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
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

		reasons, err := db.RedactionReasons()
		if err != nil {
			return err
		}

		if historyThread != "" {
			msgs, err := replayAllMessages(ctx, stream)
			if err != nil {
				return err
			}
			return printThread(msgs, historyThread, reasons, historyRaw)
		}

		msgs, err := stream.Replay(ctx, 0, historyLast)
		if err != nil {
			return err
		}

		count := 0
		for _, msg := range msgs {
			if historyBy != "" && msg.AuthorName != historyBy {
				continue
			}
			fmt.Println(formatHistoryLine(msg, renderContent(msg, reasons, historyRaw)))
			count++
		}
		if count == 0 {
			fmt.Println("(no messages)")
		}
		return nil
	},
}

func init() {
	historyCmd.Flags().IntVar(&historyLast, "last", 50, "messages to show")
	historyCmd.Flags().StringVar(&historyBy, "by", "", "filter by author")
	historyCmd.Flags().BoolVar(&historyRaw, "raw", false, "show raw content for redacted messages")
	historyCmd.Flags().StringVar(&historyThread, "thread", "", "follow a reply chain from a message ID")
	addRemoteFlag(historyCmd)
	rootCmd.AddCommand(historyCmd)
}

// formatHistoryLine renders one history row: timestamp, short ID, author, optional reply, content.
func formatHistoryLine(msg model.Message, content string) string {
	ts := msg.CreatedAt.Local().Format("2006-01-02 15:04:05")
	replyInfo := ""
	if msg.ReplyTo != nil {
		replyInfo = fmt.Sprintf(" (re: %s)", model.ShortID(*msg.ReplyTo))
	}
	return fmt.Sprintf("[%s] %s %s%s: %s", ts, model.ShortID(msg.ID), msg.AuthorName, replyInfo, content)
}

// historyRemote shows the newest --last messages from the Garden gateway. The gateway's tail read
// is the only window remote mode has, so a reply chain and the unredacted view are local-only.
func historyRemote() error {
	if historyThread != "" {
		return errors.New("--thread needs the full history, which remote mode does not read; use local history")
	}
	if historyRaw {
		return errors.New("--raw is not available in remote mode; the gateway already renders redactions")
	}
	if historyLast < 1 || historyLast > remotecli.MaxTail {
		return fmt.Errorf("--last must be 1 to %d in remote mode", remotecli.MaxTail)
	}

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

	window, err := remotecli.Tail(ctx, session, historyLast)
	if err != nil {
		return err
	}
	count := 0
	for _, raw := range window.Messages {
		if historyBy != "" && raw.AuthorName != historyBy {
			continue
		}
		msg, err := remotecli.Convert(raw)
		if err != nil {
			return err
		}
		fmt.Println(formatHistoryLine(msg, msg.Content))
		count++
	}
	if count == 0 {
		fmt.Println("(no messages)")
	}
	return nil
}

func renderContent(msg model.Message, reasons map[string]string, raw bool) string {
	if raw {
		return msg.Content
	}
	if reason, ok := reasons[msg.ID]; ok {
		return fmt.Sprintf("[redacted: %s]", reason)
	}
	return msg.Content
}

func printThread(msgs []model.Message, threadID string, reasons map[string]string, raw bool) error {
	byID := make(map[string]model.Message, len(msgs))
	children := make(map[string][]model.Message)
	for _, msg := range msgs {
		byID[msg.ID] = msg
		if msg.ReplyTo != nil {
			children[*msg.ReplyTo] = append(children[*msg.ReplyTo], msg)
		}
	}

	target, ok := byID[threadID]
	if !ok {
		return fmt.Errorf("thread message %q not found", threadID)
	}

	var chain []model.Message
	cur := target
	for {
		chain = append([]model.Message{cur}, chain...)
		if cur.ReplyTo == nil {
			break
		}
		parent, ok := byID[*cur.ReplyTo]
		if !ok {
			break
		}
		cur = parent
	}

	for depth, msg := range chain {
		printThreadMessage(msg, depth, reasons, raw)
	}
	printThreadChildren(children, target.ID, len(chain), reasons, raw)
	return nil
}

func printThreadChildren(children map[string][]model.Message, parentID string, depth int, reasons map[string]string, raw bool) {
	for _, child := range children[parentID] {
		printThreadMessage(child, depth, reasons, raw)
		printThreadChildren(children, child.ID, depth+1, reasons, raw)
	}
}

func printThreadMessage(msg model.Message, depth int, reasons map[string]string, raw bool) {
	ts := msg.CreatedAt.Local().Format("2006-01-02 15:04:05")
	replyInfo := ""
	if msg.ReplyTo != nil {
		replyInfo = fmt.Sprintf(" (re: %s)", model.ShortID(*msg.ReplyTo))
	}
	indent := ""
	for i := 0; i < depth; i++ {
		indent += "  "
	}
	fmt.Printf("%s[%s] %s %s%s: %s\n",
		indent, ts, model.ShortID(msg.ID), msg.AuthorName, replyInfo, renderContent(msg, reasons, raw))
}

func replayAllMessages(ctx context.Context, stream *transport.Stream) ([]model.Message, error) {
	startSeq := uint64(0)
	const batchSize = 1000
	var all []model.Message

	for {
		entries, err := stream.ReplayWithSeq(ctx, startSeq, batchSize)
		if err != nil {
			return nil, err
		}
		if len(entries) == 0 {
			return all, nil
		}
		for _, entry := range entries {
			all = append(all, entry.Message)
		}
		if len(entries) < batchSize {
			return all, nil
		}
		startSeq = entries[len(entries)-1].Seq + 1
	}
}
