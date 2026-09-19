package transport

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"sync"
	"time"

	"github.com/nats-io/nats.go"
	"github.com/nats-io/nats.go/jetstream"
	"github.com/veridian69/cairn/a2a/internal/model"
)

const (
	StreamName = "A2A"
	Subject    = "a2a.stream"
)

// Stream provides publish, subscribe, and replay over a JetStream-backed NATS stream.
type Stream struct {
	nc     *nats.Conn
	js     jetstream.JetStream
	stream jetstream.Stream
	mu     sync.Mutex
	cancel []func()
}

type ReplayEntry struct {
	Message model.Message
	Seq     uint64
}

type StreamOptions struct {
	MaxAge   time.Duration
	MaxBytes int64
}

// NewStream connects to NATS at the given URL and ensures the A2A JetStream stream exists.
func NewStream(url string) (*Stream, error) {
	return openStream(url, nil, 0)
}

func NewStreamWithTimeout(url string, timeout time.Duration) (*Stream, error) {
	return openStream(url, nil, timeout)
}

// NewManagedStream creates a stream with the provided settings, or opens an
// existing stream only when those settings match. Retention changes require an
// explicit migration; a startup must never rewrite persisted stream identity.
func NewManagedStream(url string, opts StreamOptions) (*Stream, error) {
	return openStream(url, &opts, 0)
}

func openStream(url string, opts *StreamOptions, dialTimeout time.Duration) (*Stream, error) {
	var connectOptions []nats.Option
	if dialTimeout > 0 {
		connectOptions = append(connectOptions, nats.Timeout(dialTimeout))
	}
	nc, err := nats.Connect(url, connectOptions...)
	if err != nil {
		return nil, fmt.Errorf("connecting: %w", err)
	}

	js, err := jetstream.New(nc)
	if err != nil {
		nc.Close()
		return nil, fmt.Errorf("jetstream: %w", err)
	}

	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()

	cfg := jetstream.StreamConfig{
		Name:       StreamName,
		Subjects:   []string{Subject},
		Storage:    jetstream.FileStorage,
		Retention:  jetstream.LimitsPolicy,
		Duplicates: 5 * time.Minute, // dedup window for crash recovery
	}
	if opts != nil {
		cfg.MaxAge = opts.MaxAge
		cfg.MaxBytes = opts.MaxBytes
	}

	stream, err := js.Stream(ctx, StreamName)
	if errors.Is(err, jetstream.ErrStreamNotFound) {
		stream, err = js.CreateStream(ctx, cfg)
	} else if err == nil && opts != nil {
		existing := stream.CachedInfo().Config
		maxBytes := opts.MaxBytes
		if maxBytes == 0 {
			maxBytes = -1 // NATS reports zero/unlimited as -1.
		}
		// NATS 2.12.6 rewrites a restored filestore's Created timestamp on
		// UpdateStream, even for an unchanged config. Avoid all startup
		// updates, including changed retention: a silent generation change
		// would invalidate durable history/inbox cursors on the next restart.
		if existing.MaxAge != opts.MaxAge || existing.MaxBytes != maxBytes ||
			existing.Storage != jetstream.FileStorage || existing.Retention != jetstream.LimitsPolicy ||
			len(existing.Subjects) != 1 || existing.Subjects[0] != Subject || existing.Duplicates != cfg.Duplicates {
			err = errors.New("existing stream settings differ; explicit stream migration is required")
		}
	}
	if err != nil {
		nc.Close()
		return nil, fmt.Errorf("opening stream: %w", err)
	}

	return &Stream{nc: nc, js: js, stream: stream}, nil
}

// Publish sends a message to the stream, using the message ID for deduplication.
func (s *Stream) Publish(ctx context.Context, msg model.Message) error {
	data, err := json.Marshal(msg)
	if err != nil {
		return fmt.Errorf("marshal: %w", err)
	}
	_, err = s.js.Publish(ctx, Subject, data,
		jetstream.WithMsgID(msg.ID),
	)
	return err
}

// PublishWithDedup publishes with an agent-scoped dedup ID.
func (s *Stream) PublishWithDedup(ctx context.Context, msg model.Message, agentID string) error {
	data, err := json.Marshal(msg)
	if err != nil {
		return fmt.Errorf("marshal: %w", err)
	}
	dedupID := agentID + ":" + msg.ID
	_, err = s.js.Publish(ctx, Subject, data,
		jetstream.WithMsgID(dedupID),
	)
	return err
}

// Subscribe delivers new messages from the stream. Handler receives message + JetStream sequence.
func (s *Stream) Subscribe(ctx context.Context, handler func(model.Message, uint64) error) error {
	cons, err := s.js.CreateOrUpdateConsumer(ctx, StreamName, jetstream.ConsumerConfig{
		DeliverPolicy: jetstream.DeliverNewPolicy,
		AckPolicy:     jetstream.AckExplicitPolicy,
		FilterSubject: Subject,
		MaxDeliver:    5,
	})
	if err != nil {
		return fmt.Errorf("consumer: %w", err)
	}

	cctx, err := cons.Consume(func(msg jetstream.Msg) {
		var m model.Message
		if err := json.Unmarshal(msg.Data(), &m); err != nil {
			msg.Term()
			return
		}
		meta, _ := msg.Metadata()
		seq := uint64(0)
		if meta != nil {
			seq = meta.Sequence.Stream
		}
		if err := handler(m, seq); err != nil {
			msg.Nak()
			return
		}
		msg.Ack()
	})
	if err != nil {
		return fmt.Errorf("consume: %w", err)
	}

	s.mu.Lock()
	s.cancel = append(s.cancel, cctx.Stop)
	s.mu.Unlock()
	go stopConsumerOnContext(ctx, cctx)
	return nil
}

// SubscribeFromSeq delivers messages starting from a specific JetStream
// sequence. The consumer's stop func is retained on the Stream so Close tears
// it down — for long-lived subscribers like the daemon's agent workers.
func (s *Stream) SubscribeFromSeq(ctx context.Context, startSeq uint64, handler func(model.Message, uint64) error) error {
	return s.subscribeFromSeq(ctx, startSeq, handler, true)
}

// SubscribeFromSeqEphemeral is SubscribeFromSeq for short-lived, per-call
// subscribers: nothing is retained on the Stream, so repeated calls do not
// grow Stream state. The caller's ctx owns the consumer's lifetime and must
// be cancelled when the subscription is done with.
func (s *Stream) SubscribeFromSeqEphemeral(ctx context.Context, startSeq uint64, handler func(model.Message, uint64) error) error {
	return s.subscribeFromSeq(ctx, startSeq, handler, false)
}

func (s *Stream) subscribeFromSeq(ctx context.Context, startSeq uint64, handler func(model.Message, uint64) error, retain bool) error {
	policy := jetstream.DeliverAllPolicy
	if startSeq > 0 {
		policy = jetstream.DeliverByStartSequencePolicy
	}

	cons, err := s.js.CreateOrUpdateConsumer(ctx, StreamName, jetstream.ConsumerConfig{
		DeliverPolicy: policy,
		OptStartSeq:   startSeq,
		AckPolicy:     jetstream.AckExplicitPolicy,
		FilterSubject: Subject,
		MaxDeliver:    5,
	})
	if err != nil {
		return fmt.Errorf("consumer: %w", err)
	}

	cctx, err := cons.Consume(func(msg jetstream.Msg) {
		var m model.Message
		if err := json.Unmarshal(msg.Data(), &m); err != nil {
			msg.Term()
			return
		}
		meta, _ := msg.Metadata()
		seq := uint64(0)
		if meta != nil {
			seq = meta.Sequence.Stream
		}
		if err := handler(m, seq); err != nil {
			msg.Nak()
			return
		}
		msg.Ack()
	})
	if err != nil {
		return fmt.Errorf("consume: %w", err)
	}

	if retain {
		s.mu.Lock()
		s.cancel = append(s.cancel, cctx.Stop)
		s.mu.Unlock()
	}
	go stopConsumerOnContext(ctx, cctx)
	return nil
}

func stopConsumerOnContext(ctx context.Context, consumer jetstream.ConsumeContext) {
	select {
	case <-ctx.Done():
		consumer.Stop()
	case <-consumer.Closed():
	}
}

// Replay fetches messages from the stream. startSeq 0 = from beginning.
func (s *Stream) Replay(ctx context.Context, startSeq uint64, limit int) ([]model.Message, error) {
	entries, err := s.ReplayWithSeq(ctx, startSeq, limit)
	if err != nil {
		return nil, err
	}
	msgs := make([]model.Message, 0, len(entries))
	for _, entry := range entries {
		msgs = append(msgs, entry.Message)
	}
	return msgs, nil
}

// ReplayWithSeq fetches messages plus their JetStream sequence numbers.
func (s *Stream) ReplayWithSeq(ctx context.Context, startSeq uint64, limit int) ([]ReplayEntry, error) {
	if limit <= 0 {
		return []ReplayEntry{}, nil
	}

	policy := jetstream.DeliverAllPolicy
	if startSeq > 0 {
		policy = jetstream.DeliverByStartSequencePolicy
	}

	cons, err := s.js.CreateOrUpdateConsumer(ctx, StreamName, jetstream.ConsumerConfig{
		DeliverPolicy:     policy,
		OptStartSeq:       startSeq,
		AckPolicy:         jetstream.AckNonePolicy,
		FilterSubject:     Subject,
		InactiveThreshold: 2 * time.Second,
	})
	if err != nil {
		return nil, fmt.Errorf("replay consumer: %w", err)
	}

	batch, err := cons.Fetch(limit, jetstream.FetchMaxWait(2*time.Second))
	if err != nil {
		return nil, fmt.Errorf("fetch: %w", err)
	}

	var msgs []ReplayEntry
	for msg := range batch.Messages() {
		var m model.Message
		if err := json.Unmarshal(msg.Data(), &m); err != nil {
			continue
		}
		meta, _ := msg.Metadata()
		seq := uint64(0)
		if meta != nil {
			seq = meta.Sequence.Stream
		}
		msgs = append(msgs, ReplayEntry{Message: m, Seq: seq})
	}
	if err := batch.Error(); err != nil && !errors.Is(err, nats.ErrTimeout) {
		return msgs, fmt.Errorf("fetch incomplete: %w", err)
	}
	return msgs, nil
}

// Tail returns the newest limit messages in chronological order.
func (s *Stream) Tail(ctx context.Context, limit int) ([]model.Message, error) {
	entries, err := s.TailWithSeq(ctx, limit)
	if err != nil {
		return nil, err
	}
	msgs := make([]model.Message, 0, len(entries))
	for _, entry := range entries {
		msgs = append(msgs, entry.Message)
	}
	return msgs, nil
}

// TailWithSeq returns the newest limit messages in chronological order.
func (s *Stream) TailWithSeq(ctx context.Context, limit int) ([]ReplayEntry, error) {
	if limit <= 0 {
		return []ReplayEntry{}, nil
	}

	info, err := s.stream.Info(ctx)
	if err != nil {
		return nil, fmt.Errorf("stream info: %w", err)
	}
	if info == nil || info.State.Msgs == 0 {
		return []ReplayEntry{}, nil
	}

	entries := make([]ReplayEntry, 0, limit)
	firstSeq := info.State.FirstSeq
	for seq := info.State.LastSeq; seq >= firstSeq && len(entries) < limit; seq-- {
		raw, err := s.stream.GetMsg(ctx, seq)
		if err != nil {
			if errors.Is(err, jetstream.ErrMsgNotFound) {
				if seq == 0 {
					break
				}
				continue
			}
			return nil, fmt.Errorf("get msg seq %d: %w", seq, err)
		}

		var msg model.Message
		if err := json.Unmarshal(raw.Data, &msg); err != nil {
			if seq == 0 {
				break
			}
			continue
		}
		entries = append(entries, ReplayEntry{Message: msg, Seq: seq})
		if seq == 0 {
			break
		}
	}

	for i, j := 0, len(entries)-1; i < j; i, j = i+1, j-1 {
		entries[i], entries[j] = entries[j], entries[i]
	}
	return entries, nil
}

// GetByID replays the entire stream and returns the message with the given ID.
// TEMPORARY: O(n) linear scan. Sufficient for v1 parent-walking with small streams.
// Replace with indexed lookup if stream exceeds ~10k messages.
func (s *Stream) GetByID(ctx context.Context, id string) (*model.Message, error) {
	startSeq := uint64(0)
	const batchSize = 1000
	for {
		msgs, err := s.ReplayWithSeq(ctx, startSeq, batchSize)
		if err != nil {
			return nil, err
		}
		if len(msgs) == 0 {
			break
		}
		for _, entry := range msgs {
			if entry.Message.ID == id {
				msg := entry.Message
				return &msg, nil
			}
		}
		startSeq = msgs[len(msgs)-1].Seq + 1
		if len(msgs) < batchSize {
			break
		}
	}
	return nil, fmt.Errorf("message %q not found", id)
}

// Close stops any active subscription and closes the NATS connection.
func (s *Stream) Close() {
	s.mu.Lock()
	for _, cancel := range s.cancel {
		cancel()
	}
	s.cancel = nil
	s.mu.Unlock()
	s.nc.Close()
}

// NATSConn returns the underlying NATS connection for control messages.
func (s *Stream) NATSConn() *nats.Conn {
	return s.nc
}
