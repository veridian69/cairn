package transport

import (
	"context"
	"encoding/json"
	"fmt"
	"time"

	"github.com/nats-io/nats.go/jetstream"
	"github.com/veridian69/cairn/a2a/internal/model"
)

// Position describes a stream generation and retained sequence range.
type Position struct {
	Created     time.Time
	First, Last uint64
}

// Position reads fresh stream information, including creation across resets.
func (s *Stream) Position(ctx context.Context) (Position, error) {
	info, err := s.stream.Info(ctx)
	if err != nil {
		return Position{}, err
	}
	return Position{Created: info.Created, First: info.State.FirstSeq, Last: info.State.LastSeq}, nil
}

// Exact reads one sequence without silently skipping missing or malformed data.
func (s *Stream) Exact(ctx context.Context, seq uint64) (model.Message, error) {
	raw, err := s.stream.GetMsg(ctx, seq)
	if err != nil {
		return model.Message{}, err
	}
	var m model.Message
	if err = json.Unmarshal(raw.Data, &m); err != nil {
		return m, fmt.Errorf("decode sequence %d: %w", seq, err)
	}
	return m, nil
}

// PublishSequence publishes once and returns the acknowledged stream sequence.
func (s *Stream) PublishSequence(ctx context.Context, msg model.Message) (uint64, error) {
	data, err := json.Marshal(msg)
	if err != nil {
		return 0, err
	}
	ack, err := s.js.Publish(ctx, Subject, data, jetstream.WithMsgID(msg.ID))
	if err != nil {
		return 0, err
	}
	return ack.Sequence, nil
}
