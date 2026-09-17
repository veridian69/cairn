package garden

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"slices"
	"strings"
	"time"
	"unicode/utf8"

	"github.com/google/uuid"
	"github.com/nats-io/nats.go/jetstream"
	"github.com/veridian69/cairn/a2a/internal/model"
)

func (s *Server) render(m model.Message, seq uint64) (Message, error) {
	if m.ID == "" || len(m.Content) > 64*1024 || !utf8.ValidString(m.Content) {
		return Message{}, failure("retention_gap", "Unreadable Garden message; operator recovery required")
	}
	content := m.Content
	reason, redacted, err := s.db.RedactionReason(m.ID)
	if err != nil {
		return Message{}, err
	}
	if redacted {
		content = fmt.Sprintf("[redacted: %s]", reason)
	}
	recipients, err := messageRecipients(m)
	if err != nil {
		return Message{}, err
	}
	out := Message{ID: m.ID, Sequence: seq, AuthorID: m.AuthorID, AuthorName: m.AuthorName, Content: content, Recipients: recipients, CreatedAt: m.CreatedAt.UTC().Format(time.RFC3339Nano), Binding: s.binding}
	if m.ReplyTo != nil {
		out.ReplyTo = *m.ReplyTo
	}
	return out, nil
}
func messageRecipients(m model.Message) ([]string, error) {
	recipients := []string{}
	raw, ok := m.Metadata["garden_recipients"]
	if !ok {
		return recipients, nil
	}
	switch value := raw.(type) {
	case []string:
		recipients = append(recipients, value...)
	case []any:
		for _, item := range value {
			name, ok := item.(string)
			if !ok {
				return nil, failure("retention_gap", "Malformed Garden recipient metadata")
			}
			recipients = append(recipients, name)
		}
	default:
		return nil, failure("retention_gap", "Malformed Garden recipient metadata")
	}
	return recipients, nil
}
func (s *Server) exact(ctx context.Context, seq uint64) (model.Message, error) {
	m, err := s.stream.Exact(ctx, seq)
	if err != nil {
		if errors.Is(err, jetstream.ErrMsgNotFound) {
			return m, failure("retention_gap", "Garden message is no longer retained; operator recovery required")
		}
		return m, failure("unavailable", "Garden message unavailable or malformed")
	}
	if !canonicalUUID(m.ID) || m.AuthorName == "" || m.CreatedAt.IsZero() || len(m.Content) > 64*1024 || !utf8.ValidString(m.Content) {
		return m, failure("retention_gap", "Malformed Garden message; operator recovery required")
	}
	return m, nil
}
func (s *Server) send(ctx context.Context, principal string, a SendArgs) (Message, error) {
	if strings.TrimSpace(a.Content) == "" || len(a.Content) > 64*1024 || !utf8.ValidString(a.Content) || len(a.Recipients) > 100 || (a.ReplyTo != "" && !canonicalUUID(a.ReplyTo)) {
		return Message{}, failure("invalid_argument", "Invalid message content, recipients or reply ID")
	}
	names := map[string]bool{}
	for _, name := range s.cfg.Principals {
		names[name] = true
	}
	seen := map[string]bool{}
	for _, name := range a.Recipients {
		if !names[name] || seen[name] {
			return Message{}, failure("invalid_argument", "Recipients must be unique configured participants")
		}
		seen[name] = true
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	_, b, err := s.position(ctx, principal)
	if err != nil {
		return Message{}, err
	}
	var reply *string
	if a.ReplyTo != "" {
		reply = &a.ReplyTo
	}
	m := model.NewMessage(model.Participant{ID: b.participant, Name: b.name, Kind: model.KindAgent}, a.Content, reply)
	m.Metadata["garden_recipients"] = append([]string{}, a.Recipients...)
	seq, err := s.stream.PublishSequence(ctx, m)
	if err != nil {
		return Message{}, failure("unavailable", "Publish outcome uncertain; do not automatically retry")
	}
	return s.render(m, seq)
}
func (s *Server) read(ctx context.Context, principal string, a ReadArgs) (ReadResult, error) {
	if a.Limit < 0 || a.Limit > 100 || (a.AfterSeq > 0 && a.Generation == "") {
		return ReadResult{}, failure("invalid_argument", "History requires limit 1..100 and a generation with after_seq")
	}
	if a.Limit == 0 {
		a.Limit = 50
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	p, b, err := s.position(ctx, principal)
	if err != nil {
		return ReadResult{}, err
	}
	if a.Generation != "" && a.Generation != b.generation {
		return ReadResult{}, failure("stream_reset", "History cursor belongs to another stream generation")
	}
	after := a.AfterSeq
	if after > p.Last {
		return ReadResult{}, failure("stream_reset", "History cursor is beyond the stream tail")
	}
	if after == 0 && p.First > 0 {
		after = p.First - 1
	}
	if after+1 < p.First {
		return ReadResult{}, failure("retention_gap", "History cursor precedes retained messages")
	}
	out := ReadResult{Messages: []Message{}, Generation: b.generation, NextSeq: after}
	// Reserve envelope space and cap the JSON records themselves. MCP repeats
	// this JSON inside a text-content string, which can at most double escaping;
	// 2 MiB structured output therefore stays comfortably under the 8 MiB wire cap.
	const historyJSONBudget = 2 * 1024 * 1024
	encodedBytes := 1024
	for seq := after + 1; seq <= p.Last && len(out.Messages) < a.Limit; seq++ {
		m, e := s.exact(ctx, seq)
		if e != nil {
			return ReadResult{}, e
		}
		msg, e := s.render(m, seq)
		if e != nil {
			return ReadResult{}, e
		}
		encoded, err := json.Marshal(msg)
		if err != nil {
			return ReadResult{}, err
		}
		if encodedBytes+len(encoded)+1 > historyJSONBudget {
			if len(out.Messages) == 0 {
				return ReadResult{}, failure("unavailable", "Garden message exceeds the history response budget")
			}
			break
		}
		encodedBytes += len(encoded) + 1
		out.Messages = append(out.Messages, msg)
		out.NextSeq = seq
	}
	out.More = out.NextSeq < p.Last
	return out, nil
}
func (s *Server) poll(ctx context.Context, principal string, a PollArgs) (PollResult, error) {
	if !canonicalUUID(a.ConsumerID) || a.WaitSeconds < 0 || a.WaitSeconds > 25 {
		return PollResult{}, failure("invalid_argument", "Poll requires a consumer UUID and wait_seconds 0..25")
	}
	deadline := time.Now().Add(time.Duration(a.WaitSeconds) * time.Second)
	for {
		out, err := s.pollOnce(ctx, principal, a.ConsumerID)
		if err != nil || out.Message != nil || !time.Now().Before(deadline) {
			return out, err
		}
		timer := time.NewTimer(200 * time.Millisecond)
		select {
		case <-ctx.Done():
			timer.Stop()
			return PollResult{}, ctx.Err()
		case <-s.done:
			timer.Stop()
			return PollResult{}, failure("unavailable", "Garden service stopped")
		case <-timer.C:
		}
	}
}
func (s *Server) pollOnce(ctx context.Context, principal, consumer string) (PollResult, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	p, b, err := s.position(ctx, principal)
	if err != nil {
		return PollResult{}, err
	}
	now := time.Now()
	if b.consumer != "" && b.consumer != consumer && b.leaseUntil > now.UnixNano() {
		return PollResult{}, failure("inbox_busy", "Another adapter owns this inbox lease")
	}
	b.consumer = consumer
	b.leaseUntil = now.Add(120 * time.Second).UnixNano()
	out := PollResult{Generation: b.generation, LeaseExpiresAt: time.Unix(0, b.leaseUntil).UTC().Format(time.RFC3339Nano)}
	if b.pending != 0 {
		m, e := s.exact(ctx, b.pending)
		if e != nil {
			return out, e
		}
		if m.ID != b.pendingID {
			return out, failure("stream_reset", "Pending message identity changed")
		}
		msg, e := s.render(m, b.pending)
		if e != nil {
			return out, e
		}
		out.Message = &msg
		out.Receipt = b.receipt
	} else {
		if b.cursor > p.Last {
			return out, failure("stream_reset", "Inbox position is beyond stream tail")
		}
		if b.cursor+1 < p.First {
			return out, failure("retention_gap", "Inbox position precedes retained messages")
		}
		for scanned, seq := 0, b.cursor+1; seq <= p.Last && scanned < 256; seq, scanned = seq+1, scanned+1 {
			m, e := s.exact(ctx, seq)
			if e != nil {
				return out, e
			}
			recipients, e := messageRecipients(m)
			if e != nil {
				return out, e
			}
			if m.AuthorID != b.participant && slices.Contains(recipients, b.name) {
				msg, e := s.render(m, seq)
				if e != nil {
					return out, e
				}
				b.pending = seq
				b.pendingID = m.ID
				b.receipt = uuid.NewString()
				out.Message = &msg
				out.Receipt = b.receipt
				break
			}
			b.cursor = seq
		}
	}
	if err = s.saveInbox(ctx, b); err != nil {
		return PollResult{}, err
	}
	return out, nil
}
func (s *Server) ack(ctx context.Context, principal string, a AckArgs) error {
	if !canonicalUUID(a.ConsumerID) || !canonicalUUID(a.Receipt) {
		return failure("invalid_argument", "Acknowledgement requires consumer and receipt UUIDs")
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	_, b, err := s.position(ctx, principal)
	if err != nil {
		return err
	}
	if b.consumer != a.ConsumerID || b.leaseUntil <= time.Now().UnixNano() {
		return failure("invalid_receipt", "Receipt does not belong to this active adapter")
	}
	if b.lastReceipt == a.Receipt {
		return nil
	}
	if b.receipt != a.Receipt || b.pending == 0 {
		return failure("invalid_receipt", "Receipt is not pending for this principal")
	}
	m, err := s.exact(ctx, b.pending)
	if err != nil {
		return err
	}
	if m.ID != b.pendingID {
		return failure("stream_reset", "Pending message identity changed")
	}
	b.cursor = b.pending
	b.pending = 0
	b.pendingID = ""
	b.lastReceipt = b.receipt
	b.receipt = ""
	return s.saveInbox(ctx, b)
}
