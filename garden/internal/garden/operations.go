package garden

import (
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"fmt"
	"slices"
	"sort"
	"strings"
	"time"
	"unicode/utf8"

	"github.com/google/uuid"
	"github.com/nats-io/nats.go/jetstream"
	"github.com/veridian69/cairn/garden/internal/model"
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
	return messageNames(m, "garden_recipients")
}

// messageNames reads a metadata list of participant names; a missing key is an empty list.
func messageNames(m model.Message, key string) ([]string, error) {
	names := []string{}
	raw, ok := m.Metadata[key]
	if !ok {
		return names, nil
	}
	switch value := raw.(type) {
	case []string:
		names = append(names, value...)
	case []any:
		for _, item := range value {
			name, ok := item.(string)
			if !ok {
				return nil, failure("retention_gap", "Malformed Garden recipient metadata")
			}
			names = append(names, name)
		}
	default:
		return nil, failure("retention_gap", "Malformed Garden recipient metadata")
	}
	return names, nil
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
func (s *Server) send(ctx context.Context, principal string, a SendArgs) (SendResult, error) {
	if strings.TrimSpace(a.Content) == "" || len(a.Content) > 64*1024 || !utf8.ValidString(a.Content) || len(a.Recipients) > 100 || (a.ReplyTo != "" && !canonicalUUID(a.ReplyTo)) {
		return SendResult{}, failure("invalid_argument", "Invalid message content, recipients or reply ID")
	}
	names := map[string]bool{}
	for _, name := range s.cfg.Principals {
		names[name] = true
	}
	seen := map[string]bool{}
	for _, name := range a.Recipients {
		if !names[name] || seen[name] {
			return SendResult{}, failure("invalid_argument", "Recipients must be unique configured participants")
		}
		seen[name] = true
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	p, b, err := s.position(ctx, principal)
	if err != nil {
		return SendResult{}, err
	}
	now := s.now()
	var reply *string
	depth := 1
	if a.ReplyTo != "" {
		reply = &a.ReplyTo
		parent, found, e := s.messageDepth(ctx, a.ReplyTo, p.First)
		if e != nil {
			return SendResult{}, e
		}
		// An unknown parent, such as one from before the upgrade, counts as a root.
		if found {
			depth = parent + 1
		}
	}
	presence, err := s.presenceByName(ctx, now)
	if err != nil {
		return SendResult{}, err
	}
	limits := s.cfg.LoopGuard.resolved()
	window := time.Duration(limits.PairWindowSeconds) * time.Second
	deliveries := make([]Delivery, 0, len(a.Recipients))
	var suppressed, attended []string
	for _, r := range a.Recipients {
		// pollOnce never leases a participant its own message, so self-attention cannot happen.
		if r == b.name {
			deliveries = append(deliveries, Delivery{Recipient: r, Delivery: "suppressed", Reason: "self"})
			continue
		}
		if depth > limits.ThreadDepth {
			deliveries = append(deliveries, Delivery{Recipient: r, Delivery: "suppressed", Reason: "thread_depth"})
			suppressed = append(suppressed, r)
			continue
		}
		count, e := s.pairCount(ctx, b.name, r, now.Add(-window))
		if e != nil {
			return SendResult{}, e
		}
		if count >= limits.PairRate {
			deliveries = append(deliveries, Delivery{Recipient: r, Delivery: "suppressed", Reason: "pair_rate"})
			suppressed = append(suppressed, r)
			continue
		}
		attended = append(attended, r)
		if p := presence[r]; p.Online && p.Deliverable {
			deliveries = append(deliveries, Delivery{Recipient: r, Delivery: "notified"})
		} else {
			deliveries = append(deliveries, Delivery{Recipient: r, Delivery: "queued"})
		}
	}
	notice := ""
	if len(suppressed) > 0 {
		notice = fmt.Sprintf("Loop guard suppressed attention for %s (%s). This exchange has gone too far without a human; involve the human before continuing.", strings.Join(suppressed, ", "), suppressionReasons(deliveries))
	}
	m := model.NewMessage(model.Participant{ID: b.participant, Name: b.name, Kind: model.KindAgent}, a.Content, reply)
	m.Metadata["garden_recipients"] = append([]string{}, a.Recipients...)
	if len(suppressed) > 0 {
		m.Metadata["garden_suppressed"] = suppressed
	}
	seq, err := s.stream.PublishSequence(ctx, m)
	if err != nil {
		return SendResult{}, failure("unavailable", "Publish outcome uncertain; do not automatically retry")
	}
	if err = s.recordLoopGuard(ctx, m.ID, seq, p.First, depth, b.name, attended, now, window); err != nil {
		return SendResult{}, failure("unavailable", "Message published but loop guard state failed; do not automatically retry")
	}
	msg, err := s.render(m, seq)
	if err != nil {
		return SendResult{}, err
	}
	return SendResult{Message: msg, Deliveries: deliveries, Notice: notice}, nil
}
func suppressionReasons(deliveries []Delivery) string {
	reasons := []string{}
	for _, d := range deliveries {
		if d.Delivery == "suppressed" && d.Reason != "self" && !slices.Contains(reasons, d.Reason) {
			reasons = append(reasons, d.Reason)
		}
	}
	return strings.Join(reasons, ", ")
}

// messageDepth returns the stored thread depth of a message the stream still retains (sequence
// at or above first); found is false for unknown or no longer retained IDs, which count as roots.
func (s *Server) messageDepth(ctx context.Context, id string, first uint64) (depth int, found bool, err error) {
	err = s.sql.QueryRowContext(ctx, "SELECT depth FROM garden_message_depth WHERE message_id=? AND seq>=?", id, first).Scan(&depth)
	if errors.Is(err, sql.ErrNoRows) {
		return 0, false, nil
	}
	if err != nil {
		return 0, false, failure("unavailable", "Garden loop guard state unavailable")
	}
	return depth, true, nil
}

// pairCount counts attention requests between an unordered pair since the cutoff.
func (s *Server) pairCount(ctx context.Context, a, b string, cutoff time.Time) (int, error) {
	low, high := pairKey(a, b)
	var n int
	err := s.sql.QueryRowContext(ctx, "SELECT COUNT(*) FROM garden_attention WHERE pair_low=? AND pair_high=? AND attended_at>?", low, high, cutoff.UnixNano()).Scan(&n)
	if err != nil {
		return 0, failure("unavailable", "Garden loop guard state unavailable")
	}
	return n, nil
}

func pairKey(a, b string) (string, string) {
	if a > b {
		return b, a
	}
	return a, b
}

// recordLoopGuard stores the message depth, the attention requests it made, and prunes expired pair rows
// and the depths of messages the stream no longer retains (sequence below first).
func (s *Server) recordLoopGuard(ctx context.Context, id string, seq, first uint64, depth int, author string, recipients []string, now time.Time, window time.Duration) error {
	tx, err := s.sql.BeginTx(ctx, nil)
	if err != nil {
		return err
	}
	defer tx.Rollback()
	if _, err = tx.ExecContext(ctx, "INSERT INTO garden_message_depth(message_id,depth,seq) VALUES(?,?,?)", id, depth, seq); err != nil {
		return err
	}
	if _, err = tx.ExecContext(ctx, "DELETE FROM garden_message_depth WHERE seq<?", first); err != nil {
		return err
	}
	if _, err = tx.ExecContext(ctx, "DELETE FROM garden_attention WHERE attended_at<=?", now.Add(-window).UnixNano()); err != nil {
		return err
	}
	for _, r := range recipients {
		low, high := pairKey(author, r)
		if _, err = tx.ExecContext(ctx, "INSERT INTO garden_attention(pair_low,pair_high,attended_at) VALUES(?,?,?)", low, high, now.UnixNano()); err != nil {
			return err
		}
	}
	return tx.Commit()
}

// presenceByName reports, for every configured participant, whether its lease is live and whether it
// is deliverable. Inbox rows of principals removed from the configuration are retained but not listed.
func (s *Server) presenceByName(ctx context.Context, now time.Time) (map[string]Presence, error) {
	configured := map[string]bool{}
	for _, name := range s.cfg.Principals {
		configured[name] = true
	}
	rows, err := s.sql.QueryContext(ctx, "SELECT name,consumer,lease_until,deliverable FROM garden_inbox ORDER BY name")
	if err != nil {
		return nil, failure("unavailable", "Garden presence unavailable")
	}
	defer rows.Close()
	out := map[string]Presence{}
	for rows.Next() {
		var name, consumer string
		var leaseUntil int64
		var deliverable bool
		if err = rows.Scan(&name, &consumer, &leaseUntil, &deliverable); err != nil {
			return nil, err
		}
		if !configured[name] {
			continue
		}
		online := consumer != "" && leaseUntil > now.UnixNano()
		out[name] = Presence{Name: name, Online: online, Deliverable: online && deliverable}
	}
	return out, rows.Err()
}

// who lists participants with live-lease presence. It does not change any inbox.
func (s *Server) who(ctx context.Context, principal string) (WhoResult, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if _, _, err := s.position(ctx, principal); err != nil {
		return WhoResult{}, err
	}
	presence, err := s.presenceByName(ctx, s.now())
	if err != nil {
		return WhoResult{}, err
	}
	out := WhoResult{Participants: make([]Presence, 0, len(presence))}
	for _, p := range presence {
		out.Participants = append(out.Participants, p)
	}
	sort.Slice(out.Participants, func(i, j int) bool { return out.Participants[i].Name < out.Participants[j].Name })
	return out, nil
}

// heartbeat takes or renews the session lease and records deliverability without leasing a message.
func (s *Server) heartbeat(ctx context.Context, principal string, a HeartbeatArgs) (HeartbeatResult, error) {
	if !canonicalUUID(a.ConsumerID) {
		return HeartbeatResult{}, failure("invalid_argument", "Heartbeat requires a consumer UUID")
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	_, b, err := s.position(ctx, principal)
	if err != nil {
		return HeartbeatResult{}, err
	}
	now := s.now()
	if b.consumer != "" && b.consumer != a.ConsumerID && b.leaseUntil > now.UnixNano() {
		return HeartbeatResult{}, failure("inbox_busy", "Another adapter owns this inbox lease")
	}
	b.consumer = a.ConsumerID
	b.leaseUntil = now.Add(sessionLease).UnixNano()
	b.deliverable = a.Deliverable
	if err = s.saveInbox(ctx, b); err != nil {
		return HeartbeatResult{}, err
	}
	return HeartbeatResult{Generation: b.generation, LeaseExpiresAt: time.Unix(0, b.leaseUntil).UTC().Format(time.RFC3339Nano), Deliverable: b.deliverable}, nil
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
	now := s.now()
	if b.consumer != "" && b.consumer != consumer && b.leaseUntil > now.UnixNano() {
		return PollResult{}, failure("inbox_busy", "Another adapter owns this inbox lease")
	}
	b.consumer = consumer
	b.leaseUntil = now.Add(sessionLease).UnixNano()
	// Polling is itself proof of delivery, so an old adapter that only polls stays deliverable.
	b.deliverable = true
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
			suppressed, e := messageNames(m, "garden_suppressed")
			if e != nil {
				return out, e
			}
			// Suppressed attention stays in history but is never leased to the recipients it was denied.
			if m.AuthorID != b.participant && slices.Contains(recipients, b.name) && !slices.Contains(suppressed, b.name) {
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
