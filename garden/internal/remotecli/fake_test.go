package remotecli

import (
	"context"
	"fmt"
	"sync"
	"time"

	"github.com/veridian69/cairn/garden/internal/garden"
	"github.com/veridian69/cairn/garden/internal/gardenauth"
)

// fakeRoom models the gateway's read_messages semantics for one room: retained messages with
// sequences from first to last, a generation, tail reads, and the stream_reset and retention_gap
// refusals. legacy makes it ignore tail, as a gateway that predates tail reads does.
type fakeRoom struct {
	mu         sync.Mutex
	generation string
	messages   []garden.Message // messages[i] has sequence i+1
	first      uint64           // oldest retained sequence
	legacy     bool
	rejectTail bool
	failReads  int    // reads to fail with unavailable before succeeding
	failCode   string // when set, every read fails with this code
	reads      int
	sent       []garden.SendArgs
	closed     int
	status     garden.StatusResult
	who        garden.WhoResult
}

func newFakeRoom(generation string) *fakeRoom {
	return &fakeRoom{generation: generation, first: 1}
}

func (r *fakeRoom) last() uint64 { return uint64(len(r.messages)) }

// add appends a message as participant author and returns it with its sequence.
func (r *fakeRoom) add(author, content string) garden.Message {
	r.mu.Lock()
	defer r.mu.Unlock()
	return r.appendLocked(author, content)
}

func (r *fakeRoom) appendLocked(author, content string) garden.Message {
	seq := r.last() + 1
	m := garden.Message{
		ID:         fmt.Sprintf("00000000-0000-4000-8000-%012d", seq),
		Sequence:   seq,
		AuthorName: author,
		AuthorID:   "author-" + author,
		Content:    content,
		Recipients: []string{},
		CreatedAt:  time.Date(2026, 11, 8, 9, 0, 0, 0, time.UTC).Add(time.Duration(seq) * time.Second).Format(time.RFC3339Nano),
		Binding:    garden.Binding{},
	}
	r.messages = append(r.messages, m)
	return m
}

// reset starts a new generation that keeps none of the old messages.
func (r *fakeRoom) reset(generation string) {
	r.mu.Lock()
	defer r.mu.Unlock()
	r.generation = generation
	r.messages = nil
	r.first = 1
}

// expire drops messages below first, as retention does.
func (r *fakeRoom) expire(first uint64) {
	r.mu.Lock()
	defer r.mu.Unlock()
	r.first = first
}

func (r *fakeRoom) Read(_ context.Context, a garden.ReadArgs) (garden.ReadResult, error) {
	r.mu.Lock()
	defer r.mu.Unlock()
	r.reads++
	if r.failCode != "" {
		return garden.ReadResult{}, &garden.Error{Code: r.failCode, Message: "refused"}
	}
	if r.failReads > 0 {
		r.failReads--
		return garden.ReadResult{}, &garden.Error{Code: "unavailable", Message: "gateway unavailable"}
	}
	if a.Tail > 0 && r.rejectTail {
		return garden.ReadResult{}, &garden.Error{Code: "invalid_argument", Message: "unknown argument"}
	}
	if a.Tail > 0 && !r.legacy {
		if a.AfterSeq > 0 {
			return garden.ReadResult{}, &garden.Error{Code: "invalid_argument", Message: "tail excludes after_seq"}
		}
		start := uint64(1)
		if r.last() > uint64(a.Tail) {
			start = r.last() - uint64(a.Tail) + 1
		}
		if start < r.first {
			start = r.first
		}
		return r.page(start, r.last(), r.last()), nil
	}
	limit := a.Limit
	if limit == 0 {
		limit = 50
	}
	if a.Generation != "" && a.Generation != r.generation {
		return garden.ReadResult{}, &garden.Error{Code: "stream_reset", Message: "another generation"}
	}
	after := a.AfterSeq
	if after > r.last() {
		return garden.ReadResult{}, &garden.Error{Code: "stream_reset", Message: "beyond tail"}
	}
	if after == 0 && r.first > 1 {
		after = r.first - 1
	}
	if after+1 < r.first {
		return garden.ReadResult{}, &garden.Error{Code: "retention_gap", Message: "precedes retained"}
	}
	end := after + uint64(limit)
	if end > r.last() {
		end = r.last()
	}
	res := r.page(after+1, end, after)
	res.More = res.NextSeq < r.last()
	return res, nil
}

// page returns messages from start to end inclusive, with cursor next as NextSeq when empty.
func (r *fakeRoom) page(start, end, next uint64) garden.ReadResult {
	out := garden.ReadResult{Messages: []garden.Message{}, Generation: r.generation, NextSeq: next}
	for seq := start; seq <= end && seq >= 1; seq++ {
		out.Messages = append(out.Messages, r.messages[seq-1])
		out.NextSeq = seq
	}
	return out
}

func (r *fakeRoom) Status(context.Context) (garden.StatusResult, error) {
	r.mu.Lock()
	defer r.mu.Unlock()
	return r.status, nil
}

func (r *fakeRoom) Send(_ context.Context, a garden.SendArgs) (garden.SendResult, error) {
	r.mu.Lock()
	defer r.mu.Unlock()
	r.sent = append(r.sent, a)
	m := r.appendLocked(r.status.Participant, a.Content)
	m.ReplyTo = a.ReplyTo
	r.messages[len(r.messages)-1] = m
	return garden.SendResult{Message: m}, nil
}

func (r *fakeRoom) Who(context.Context) (garden.WhoResult, error) {
	r.mu.Lock()
	defer r.mu.Unlock()
	return r.who, nil
}

func (r *fakeRoom) Supports(string) bool { return true }

func (r *fakeRoom) Close() error {
	r.mu.Lock()
	defer r.mu.Unlock()
	r.closed++
	return nil
}

// testBinding is the binding the fixture home's garden.json declares.
func testBinding() garden.Binding {
	return garden.Binding{
		InstanceID:     "6f1f3d2e-8a4b-4c5d-9e6f-7a8b9c0d1e2f",
		Scope:          gardenauth.Scope{Realm: "cairn", Segments: []gardenauth.Segment{{Kind: "project", Identifier: "garden"}}},
		Classification: "internal",
	}
}
