package mcpserver

import (
	"context"
	"encoding/json"
	"fmt"
	"time"

	"github.com/modelcontextprotocol/go-sdk/mcp"
	"github.com/veridian69/cairn/a2a/internal/model"
	"github.com/veridian69/cairn/a2a/internal/state"
)

// agentStatus mirrors the JSON served by the daemon on a2a.status
// (daemon.AgentStatus) without importing the daemon package.
type agentStatus struct {
	Name           string  `json:"name" jsonschema:"agent name"`
	Provider       string  `json:"provider,omitempty" jsonschema:"LLM provider"`
	Model          string  `json:"model,omitempty" jsonschema:"model identifier"`
	Active         bool    `json:"active" jsonschema:"whether the agent is active"`
	State          string  `json:"state,omitempty" jsonschema:"runtime state"`
	Responsiveness float64 `json:"responsiveness" jsonschema:"probability of responding to a message"`
	QueueDepth     int     `json:"queue_depth" jsonschema:"pending inbox messages"`
	LastSeenSeq    uint64  `json:"last_seen_seq" jsonschema:"last processed stream sequence"`
	HourlyCount    int     `json:"hourly_count" jsonschema:"messages sent this hour"`
}

type statusResult struct {
	Agents []agentStatus `json:"agents" jsonschema:"configured daemon agents and their runtime state"`
}

type sendArgs struct {
	Content string `json:"content" jsonschema:"the message text to post to the shared stream"`
	ReplyTo string `json:"reply_to,omitempty" jsonschema:"optional ID of the message this replies to"`
}

type sendResult struct {
	MessageID string `json:"message_id" jsonschema:"ID of the published message"`
}

type messageOut struct {
	ID        string `json:"id" jsonschema:"message ID (use as reply_to to thread)"`
	Author    string `json:"author" jsonschema:"participant name that wrote the message"`
	Content   string `json:"content" jsonschema:"message text; redacted messages read [redacted: reason]"`
	ReplyTo   string `json:"reply_to,omitempty" jsonschema:"ID of the message this replies to, if any"`
	CreatedAt string `json:"created_at" jsonschema:"RFC3339 creation time"`
}

type messagesResult struct {
	Messages []messageOut `json:"messages" jsonschema:"messages in chronological order"`
}

type readArgs struct {
	Last int    `json:"last,omitempty" jsonschema:"how many recent messages to return; default 50, maximum 500"`
	By   string `json:"by,omitempty" jsonschema:"only return messages by this participant name"`
}

type waitArgs struct {
	TimeoutSeconds int `json:"timeout_seconds,omitempty" jsonschema:"seconds to wait for new messages; default 60, maximum 300"`
}

// waitBufferSize is the wait subscription's channel capacity. A variable so
// tests can shrink it to force the buffer-full backpressure path.
var waitBufferSize = 128

func (s *Server) registerTools() {
	mcp.AddTool(s.mcp, &mcp.Tool{
		Name: "status",
		Description: "List the a2a daemon's configured LLM agents and their runtime state. " +
			"Only daemon agents are listed — other MCP sessions (Claude Code, Codex) are " +
			"invisible here, so an absent name does not mean nobody else is connected.",
	}, s.handleStatus)

	mcp.AddTool(s.mcp, &mcp.Tool{
		Name: "send_message",
		Description: "Post a message to the shared a2a conversation stream. Other participants " +
			"(agents and humans) will see it. After sending, call wait_for_messages to receive replies.",
	}, s.handleSend)

	mcp.AddTool(s.mcp, &mcp.Tool{
		Name: "read_messages",
		Description: "Read the most recent messages from the shared a2a stream (chronological order). " +
			"Use this to catch up on history; use wait_for_messages to block for new ones.",
	}, s.handleRead)

	mcp.AddTool(s.mcp, &mcp.Tool{
		Name: "wait_for_messages",
		Description: "Block until new messages arrive from other participants, or the timeout expires. " +
			"Returns an empty list on timeout — that is normal, call it again to keep listening. " +
			"Your own messages are never returned. Use this after send_message to receive replies.",
	}, s.handleWait)
}

// toMessageOut renders one stream entry for the wire, applying redactions.
func toMessageOut(db *state.DB, m model.Message) (messageOut, error) {
	content, err := renderContent(db, m)
	if err != nil {
		return messageOut{}, err
	}
	out := messageOut{
		ID:        m.ID,
		Author:    m.AuthorName,
		Content:   content,
		CreatedAt: m.CreatedAt.Format(time.RFC3339),
	}
	if m.ReplyTo != nil {
		out.ReplyTo = *m.ReplyTo
	}
	return out, nil
}

func (s *Server) handleStatus(ctx context.Context, req *mcp.CallToolRequest, args struct{}) (*mcp.CallToolResult, statusResult, error) {
	stream, _, _, err := s.ensure(ctx)
	if err != nil {
		return nil, statusResult{}, err
	}
	reqCtx, cancel := context.WithTimeout(ctx, 2*time.Second)
	defer cancel()
	msg, err := stream.NATSConn().RequestWithContext(reqCtx, "a2a.status", nil)
	if err != nil {
		return nil, statusResult{}, fmt.Errorf("daemon status request failed: %w", err)
	}
	var agents []agentStatus
	if err := json.Unmarshal(msg.Data, &agents); err != nil {
		return nil, statusResult{}, fmt.Errorf("decoding status: %w", err)
	}
	return nil, statusResult{Agents: agents}, nil
}

func (s *Server) handleSend(ctx context.Context, req *mcp.CallToolRequest, args sendArgs) (*mcp.CallToolResult, sendResult, error) {
	if args.Content == "" {
		return nil, sendResult{}, fmt.Errorf("content must not be empty")
	}
	stream, _, self, err := s.ensure(ctx)
	if err != nil {
		return nil, sendResult{}, err
	}
	var replyTo *string
	if args.ReplyTo != "" {
		replyTo = &args.ReplyTo
	}
	msg := model.NewMessage(self, args.Content, replyTo)
	pubCtx, cancel := context.WithTimeout(ctx, 5*time.Second)
	defer cancel()
	if err := stream.Publish(pubCtx, msg); err != nil {
		return nil, sendResult{}, fmt.Errorf("publish: %w", err)
	}
	return nil, sendResult{MessageID: msg.ID}, nil
}

func (s *Server) handleRead(ctx context.Context, req *mcp.CallToolRequest, args readArgs) (*mcp.CallToolResult, messagesResult, error) {
	stream, db, _, err := s.ensure(ctx)
	if err != nil {
		return nil, messagesResult{}, err
	}
	limit := args.Last
	if limit <= 0 {
		limit = 50
	}
	if limit > 500 {
		limit = 500
	}
	entries, err := stream.TailWithSeq(ctx, limit)
	if err != nil {
		return nil, messagesResult{}, fmt.Errorf("reading stream: %w", err)
	}

	result := messagesResult{Messages: []messageOut{}}
	maxSeq := uint64(0)
	for _, entry := range entries {
		if entry.Seq > maxSeq {
			maxSeq = entry.Seq
		}
		if args.By != "" && entry.Message.AuthorName != args.By {
			continue
		}
		out, err := toMessageOut(db, entry.Message)
		if err != nil {
			return nil, messagesResult{}, err
		}
		result.Messages = append(result.Messages, out)
	}
	// Only an unfiltered read is a catch-up: it advances the cursor past the
	// newest message returned, so wait_for_messages won't re-deliver it. A
	// filtered read is a targeted query — entries the filter skipped were
	// never delivered to anyone, so the cursor must stay put or they'd be
	// silently and permanently lost to wait_for_messages.
	//
	// Even unfiltered, the tail is only the newest `limit` entries: if more
	// arrived than that, the window skips messages between the cursor and
	// entries[0], and advancing past them would lose them the same way.
	if args.By == "" && len(entries) > 0 {
		s.advanceCursorIfContiguous(entries[0].Seq, maxSeq)
	}
	return nil, result, nil
}

func (s *Server) handleWait(ctx context.Context, req *mcp.CallToolRequest, args waitArgs) (*mcp.CallToolResult, messagesResult, error) {
	stream, db, self, err := s.ensure(ctx)
	if err != nil {
		return nil, messagesResult{}, err
	}

	timeout := time.Duration(args.TimeoutSeconds) * time.Second
	if timeout <= 0 {
		timeout = 60 * time.Second
	}
	if timeout > 300*time.Second {
		timeout = 300 * time.Second
	}
	timer := time.NewTimer(timeout)
	defer timer.Stop()

	result := messagesResult{Messages: []messageOut{}}

	// The timer spans queueing and waiting: a wait that spends its whole
	// window queued behind another simply saw no messages in its window.
	select {
	case s.waitSem <- struct{}{}:
		defer func() { <-s.waitSem }()
	case <-s.done:
		// Shutdown: an empty success, not an error. The stdio transport
		// never cancels handler contexts, so this is the only way out.
		return nil, result, nil
	case <-ctx.Done():
		return nil, messagesResult{}, ctx.Err()
	case <-timer.C:
		return nil, result, nil
	}

	// Re-snapshot after queueing: the daemon may have restarted — or another
	// tool call may have re-dialled and closed the pre-queue handles — while
	// this wait sat behind the semaphore. A no-op when still connected.
	stream, db, self, err = s.ensure(ctx)
	if err != nil {
		return nil, messagesResult{}, err
	}

	// One push consumer for the whole wait (the chat/watch pattern), stopped
	// on return when subCtx is cancelled — not a 2s-polled ephemeral consumer
	// per fetch. Delivery is push, so a new message returns immediately. The
	// Ephemeral variant retains nothing on the Stream: one wait call per few
	// seconds for a whole editor session must not grow Stream state.
	subCtx, cancel := context.WithCancel(ctx)
	defer cancel()
	type seqEntry struct {
		msg model.Message
		seq uint64
	}
	entries := make(chan seqEntry, waitBufferSize)
	err = stream.SubscribeFromSeqEphemeral(subCtx, s.cursorValue()+1, func(m model.Message, seq uint64) error {
		// Block when the buffer is full: Consume delivers serially, so this
		// is clean backpressure. A Nak instead would let later entries be
		// buffered ahead of the redelivery — the batch loop would advance the
		// cursor past the gap and the redelivered entry would then be dropped
		// as a duplicate, consumed without ever being delivered.
		select {
		case entries <- seqEntry{m, seq}:
			return nil
		case <-subCtx.Done():
			return subCtx.Err()
		}
	})
	if err != nil {
		return nil, messagesResult{}, fmt.Errorf("reading stream: %w", err)
	}

	for {
		select {
		case <-s.done:
			return nil, result, nil
		case <-ctx.Done():
			return nil, messagesResult{}, ctx.Err()
		case <-timer.C:
			return nil, result, nil // empty success — "no reply yet" is normal
		case first := <-entries:
			batch := []seqEntry{first}
		drain:
			for {
				select {
				case e := <-entries:
					batch = append(batch, e)
				default:
					break drain
				}
			}
			for _, e := range batch {
				if e.seq <= s.cursorValue() {
					continue // redelivery, or read_messages advanced past it
				}
				if e.msg.AuthorID == self.ID {
					s.advanceCursor(e.seq) // own messages were handled, just not delivered
					continue
				}
				// Advance only after the entry is rendered and appended: a
				// render failure aborts the call, and the caller must still
				// be able to get these entries on the retry.
				out, err := toMessageOut(db, e.msg)
				if err != nil {
					return nil, messagesResult{}, err
				}
				result.Messages = append(result.Messages, out)
				s.advanceCursor(e.seq)
			}
			if len(result.Messages) > 0 {
				return nil, result, nil
			}
		}
	}
}
