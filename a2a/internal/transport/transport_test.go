package transport

import (
	"context"
	"errors"
	"fmt"
	"sync"
	"testing"
	"time"

	"github.com/veridian69/cairn/a2a/internal/model"
)

func TestServerStartStop(t *testing.T) {
	srv, err := NewServer(t.TempDir())
	if err != nil {
		t.Fatalf("NewServer: %v", err)
	}
	defer srv.Stop()

	if srv.ClientURL() == "" {
		t.Fatal("ClientURL empty")
	}
}

func TestManagedStreamGenerationSurvivesRepeatedServerRestarts(t *testing.T) {
	for _, options := range []StreamOptions{{}, {MaxAge: 24 * time.Hour, MaxBytes: 1024 * 1024}} {
		t.Run(fmt.Sprint(options), func(t *testing.T) {
			dir := t.TempDir()
			var generation time.Time
			for round := 0; round < 4; round++ {
				srv, err := NewServer(dir)
				if err != nil {
					t.Fatal(err)
				}
				stream, err := NewManagedStream(srv.ClientURL(), options)
				if err != nil {
					srv.Stop()
					t.Fatal(err)
				}
				position, err := stream.Position(context.Background())
				if err != nil {
					t.Fatal(err)
				}
				if round == 0 {
					generation = position.Created
				}
				stream.Close()
				srv.Stop()
				if !position.Created.Equal(generation) {
					t.Fatalf("restart %d changed stream generation from %s to %s", round, generation, position.Created)
				}
			}
		})
	}
}

func TestManagedStreamRejectsChangedRetentionWithoutMutatingExistingStream(t *testing.T) {
	dir := t.TempDir()
	options := StreamOptions{MaxAge: 24 * time.Hour, MaxBytes: 1024 * 1024}
	srv, err := NewServer(dir)
	if err != nil {
		t.Fatal(err)
	}
	stream, err := NewManagedStream(srv.ClientURL(), options)
	if err != nil {
		t.Fatal(err)
	}
	position, err := stream.Position(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	stream.Close()
	srv.Stop()
	srv, err = NewServer(dir)
	if err != nil {
		t.Fatal(err)
	}
	changed, err := NewManagedStream(srv.ClientURL(), StreamOptions{MaxAge: time.Hour})
	if err == nil {
		changed.Close()
		srv.Stop()
		t.Fatal("retention change accepted on existing stream")
	}
	srv.Stop()
	srv, err = NewServer(dir)
	if err != nil {
		t.Fatal(err)
	}
	defer srv.Stop()
	stream, err = NewManagedStream(srv.ClientURL(), options)
	if err != nil {
		t.Fatal(err)
	}
	defer stream.Close()
	after, err := stream.Position(context.Background())
	if err != nil || !after.Created.Equal(position.Created) {
		t.Fatalf("rejected change altered generation: %v", err)
	}
}

func TestPublishAndSubscribe(t *testing.T) {
	srv, err := NewServer(t.TempDir())
	if err != nil {
		t.Fatalf("NewServer: %v", err)
	}
	defer srv.Stop()

	stream, err := NewStream(srv.ClientURL())
	if err != nil {
		t.Fatalf("NewStream: %v", err)
	}
	defer stream.Close()

	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()

	var received model.Message
	var wg sync.WaitGroup
	wg.Add(1)

	err = stream.Subscribe(ctx, func(msg model.Message, seq uint64) error {
		received = msg
		wg.Done()
		return nil
	})
	if err != nil {
		t.Fatalf("Subscribe: %v", err)
	}

	author := model.Participant{ID: "p-1", Name: "claude", Kind: "agent"}
	sent := model.NewMessage(author, "Hello from the agora", nil)
	if err := stream.Publish(ctx, sent); err != nil {
		t.Fatalf("Publish: %v", err)
	}

	wg.Wait()

	if received.ID != sent.ID {
		t.Errorf("ID mismatch")
	}
	if received.AuthorName != "claude" {
		t.Errorf("AuthorName = %q", received.AuthorName)
	}
}

func TestReplay(t *testing.T) {
	srv, err := NewServer(t.TempDir())
	if err != nil {
		t.Fatalf("NewServer: %v", err)
	}
	defer srv.Stop()

	stream, err := NewStream(srv.ClientURL())
	if err != nil {
		t.Fatalf("NewStream: %v", err)
	}
	defer stream.Close()

	ctx := context.Background()
	author := model.Participant{ID: "p-1", Name: "test"}

	for i := 0; i < 5; i++ {
		stream.Publish(ctx, model.NewMessage(author, "msg", nil))
	}

	msgs, err := stream.Replay(ctx, 0, 100)
	if err != nil {
		t.Fatalf("Replay: %v", err)
	}
	if len(msgs) != 5 {
		t.Fatalf("got %d, want 5", len(msgs))
	}

	// Replay from seq 3
	msgs2, err := stream.Replay(ctx, 3, 100)
	if err != nil {
		t.Fatalf("Replay from 3: %v", err)
	}
	if len(msgs2) != 3 {
		t.Fatalf("got %d, want 3", len(msgs2))
	}
}

func TestTailWithSeq(t *testing.T) {
	srv, err := NewServer(t.TempDir())
	if err != nil {
		t.Fatalf("NewServer: %v", err)
	}
	defer srv.Stop()

	stream, err := NewStream(srv.ClientURL())
	if err != nil {
		t.Fatalf("NewStream: %v", err)
	}
	defer stream.Close()

	ctx := context.Background()
	author := model.Participant{ID: "p-1", Name: "tail"}

	if entries, err := stream.TailWithSeq(ctx, 0); err != nil {
		t.Fatalf("TailWithSeq zero limit: %v", err)
	} else if len(entries) != 0 {
		t.Fatalf("got %d entries, want 0", len(entries))
	}

	if entries, err := stream.TailWithSeq(ctx, 5); err != nil {
		t.Fatalf("TailWithSeq empty stream: %v", err)
	} else if len(entries) != 0 {
		t.Fatalf("got %d entries, want 0", len(entries))
	}

	var published []model.Message
	for i := 0; i < 6; i++ {
		msg := model.NewMessage(author, fmt.Sprintf("msg-%d", i), nil)
		published = append(published, msg)
		if err := stream.Publish(ctx, msg); err != nil {
			t.Fatalf("Publish %d: %v", i, err)
		}
	}

	all, err := stream.TailWithSeq(ctx, 10)
	if err != nil {
		t.Fatalf("TailWithSeq all: %v", err)
	}
	if len(all) != 6 {
		t.Fatalf("got %d entries, want 6", len(all))
	}
	for i, entry := range all {
		if entry.Message.ID != published[i].ID {
			t.Fatalf("entry %d id mismatch: got %s want %s", i, entry.Message.ID, published[i].ID)
		}
	}

	tail, err := stream.TailWithSeq(ctx, 3)
	if err != nil {
		t.Fatalf("TailWithSeq 3: %v", err)
	}
	if len(tail) != 3 {
		t.Fatalf("got %d entries, want 3", len(tail))
	}
	want := published[len(published)-3:]
	for i, entry := range tail {
		if entry.Message.ID != want[i].ID {
			t.Fatalf("tail entry %d id mismatch: got %s want %s", i, entry.Message.ID, want[i].ID)
		}
		if i > 0 && entry.Seq <= tail[i-1].Seq {
			t.Fatalf("tail sequence not chronological: %d then %d", tail[i-1].Seq, entry.Seq)
		}
	}
}

func TestPublishWithDedupAvoidsDuplicateMessages(t *testing.T) {
	srv, err := NewServer(t.TempDir())
	if err != nil {
		t.Fatalf("NewServer: %v", err)
	}
	defer srv.Stop()

	stream, err := NewStream(srv.ClientURL())
	if err != nil {
		t.Fatalf("NewStream: %v", err)
	}
	defer stream.Close()

	ctx := context.Background()
	author := model.Participant{ID: "agent-1", Name: "claude", Kind: "agent"}
	msg := model.NewMessage(author, "duplicate me once", nil)

	if err := stream.PublishWithDedup(ctx, msg, author.ID); err != nil {
		t.Fatalf("first PublishWithDedup: %v", err)
	}
	if err := stream.PublishWithDedup(ctx, msg, author.ID); err != nil {
		t.Fatalf("second PublishWithDedup: %v", err)
	}

	msgs, err := stream.Replay(ctx, 0, 10)
	if err != nil {
		t.Fatalf("Replay: %v", err)
	}
	if len(msgs) != 1 {
		t.Fatalf("got %d messages after dedup publish, want 1", len(msgs))
	}
}

func TestGetByIDFindsAndMissesMessages(t *testing.T) {
	srv, err := NewServer(t.TempDir())
	if err != nil {
		t.Fatalf("NewServer: %v", err)
	}
	defer srv.Stop()

	stream, err := NewStream(srv.ClientURL())
	if err != nil {
		t.Fatalf("NewStream: %v", err)
	}
	defer stream.Close()

	ctx := context.Background()
	author := model.Participant{ID: "p-1", Name: "finder", Kind: "human"}
	first := model.NewMessage(author, "first", nil)
	second := model.NewMessage(author, "second", nil)
	if err := stream.Publish(ctx, first); err != nil {
		t.Fatalf("Publish first: %v", err)
	}
	if err := stream.Publish(ctx, second); err != nil {
		t.Fatalf("Publish second: %v", err)
	}

	found, err := stream.GetByID(ctx, second.ID)
	if err != nil {
		t.Fatalf("GetByID existing: %v", err)
	}
	if found == nil || found.ID != second.ID {
		t.Fatalf("GetByID returned %+v, want %s", found, second.ID)
	}

	_, err = stream.GetByID(ctx, "missing-message-id")
	if err == nil {
		t.Fatal("GetByID should fail for a missing message")
	}
	if !errors.Is(err, context.DeadlineExceeded) && err.Error() != `message "missing-message-id" not found` {
		t.Fatalf("unexpected GetByID error: %v", err)
	}
}

func TestSubscribeFromSeqStartsAtRequestedSequence(t *testing.T) {
	srv, err := NewServer(t.TempDir())
	if err != nil {
		t.Fatalf("NewServer: %v", err)
	}
	defer srv.Stop()

	stream, err := NewStream(srv.ClientURL())
	if err != nil {
		t.Fatalf("NewStream: %v", err)
	}
	defer stream.Close()

	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()

	author := model.Participant{ID: "p-1", Name: "subscriber", Kind: "human"}
	var published []model.Message
	for i := 0; i < 3; i++ {
		msg := model.NewMessage(author, fmt.Sprintf("seed-%d", i), nil)
		published = append(published, msg)
		if err := stream.Publish(ctx, msg); err != nil {
			t.Fatalf("Publish seed %d: %v", i, err)
		}
	}

	entries, err := stream.ReplayWithSeq(ctx, 0, 10)
	if err != nil {
		t.Fatalf("ReplayWithSeq: %v", err)
	}
	if len(entries) != 3 {
		t.Fatalf("got %d replay entries, want 3", len(entries))
	}
	startSeq := entries[1].Seq

	received := make(chan ReplayEntry, 4)
	if err := stream.SubscribeFromSeq(ctx, startSeq, func(msg model.Message, seq uint64) error {
		received <- ReplayEntry{Message: msg, Seq: seq}
		return nil
	}); err != nil {
		t.Fatalf("SubscribeFromSeq: %v", err)
	}

	live := model.NewMessage(author, "live-message", nil)
	if err := stream.Publish(ctx, live); err != nil {
		t.Fatalf("Publish live: %v", err)
	}

	var got []ReplayEntry
	deadline := time.After(2 * time.Second)
	for len(got) < 3 {
		select {
		case entry := <-received:
			got = append(got, entry)
		case <-deadline:
			t.Fatalf("timed out waiting for subscribed messages, got %d", len(got))
		}
	}

	if got[0].Message.ID != published[1].ID {
		t.Fatalf("first subscribed message = %s, want %s", got[0].Message.ID, published[1].ID)
	}
	if got[1].Message.ID != published[2].ID {
		t.Fatalf("second subscribed message = %s, want %s", got[1].Message.ID, published[2].ID)
	}
	if got[2].Message.ID != live.ID {
		t.Fatalf("third subscribed message = %s, want %s", got[2].Message.ID, live.ID)
	}
}

func TestSubscriptionStopsAfterContextCancellation(t *testing.T) {
	tests := []struct {
		name      string
		subscribe func(*Stream, context.Context, func(model.Message, uint64) error) error
	}{
		{
			name: "new messages",
			subscribe: func(stream *Stream, ctx context.Context, handler func(model.Message, uint64) error) error {
				return stream.Subscribe(ctx, handler)
			},
		},
		{
			name: "from sequence",
			subscribe: func(stream *Stream, ctx context.Context, handler func(model.Message, uint64) error) error {
				return stream.SubscribeFromSeq(ctx, 1, handler)
			},
		},
	}

	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			srv, err := NewServer(t.TempDir())
			if err != nil {
				t.Fatalf("NewServer: %v", err)
			}
			defer srv.Stop()

			stream, err := NewStream(srv.ClientURL())
			if err != nil {
				t.Fatalf("NewStream: %v", err)
			}
			defer stream.Close()

			subCtx, cancel := context.WithCancel(context.Background())
			received := make(chan string, 2)
			if err := tc.subscribe(stream, subCtx, func(msg model.Message, _ uint64) error {
				received <- msg.ID
				return nil
			}); err != nil {
				t.Fatalf("subscribe: %v", err)
			}

			author := model.Participant{ID: "p-1", Name: "subscriber", Kind: "human"}
			first := model.NewMessage(author, "before cancellation", nil)
			if err := stream.Publish(context.Background(), first); err != nil {
				t.Fatalf("Publish first: %v", err)
			}
			select {
			case got := <-received:
				if got != first.ID {
					t.Fatalf("received %q, want %q", got, first.ID)
				}
			case <-time.After(2 * time.Second):
				t.Fatal("timed out waiting for pre-cancellation delivery")
			}

			cancel()
			// Subscribe does not expose the JetStream consume context. Allow its
			// cancellation watcher to stop before testing post-cancel delivery.
			time.Sleep(50 * time.Millisecond)

			second := model.NewMessage(author, "after cancellation", nil)
			if err := stream.Publish(context.Background(), second); err != nil {
				t.Fatalf("Publish second: %v", err)
			}
			select {
			case got := <-received:
				t.Fatalf("received %q after subscription context cancellation", got)
			case <-time.After(200 * time.Millisecond):
			}
		})
	}
}

func TestSubscribeFromSeqEphemeralDoesNotRetainOnStream(t *testing.T) {
	srv, err := NewServer(t.TempDir())
	if err != nil {
		t.Fatalf("NewServer: %v", err)
	}
	defer srv.Stop()

	stream, err := NewStream(srv.ClientURL())
	if err != nil {
		t.Fatalf("NewStream: %v", err)
	}
	defer stream.Close()

	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()

	// Per-call subscribers (one per wait_for_messages) must not grow Stream
	// state: the caller's context owns the consumer's lifecycle.
	for i := 0; i < 10; i++ {
		subCtx, stop := context.WithCancel(ctx)
		if err := stream.SubscribeFromSeqEphemeral(subCtx, 1, func(model.Message, uint64) error { return nil }); err != nil {
			t.Fatalf("subscribe %d: %v", i, err)
		}
		stop()
	}

	stream.mu.Lock()
	retained := len(stream.cancel)
	stream.mu.Unlock()
	if retained != 0 {
		t.Fatalf("ephemeral subscriptions retained %d stop funcs on the Stream", retained)
	}
}
