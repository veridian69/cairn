package remotecli

import (
	"context"
	"errors"
	"fmt"
	"testing"
	"time"

	"github.com/veridian69/cairn/garden/internal/garden"
)

// recorder collects follower callbacks on channels so tests can wait for exact events.
type recorder struct {
	messages chan garden.Message
	notices  chan string
	links    chan bool
}

func newRecorder() *recorder {
	return &recorder{messages: make(chan garden.Message, 1000), notices: make(chan string, 100), links: make(chan bool, 100)}
}

// follower returns a follower over room that polls every millisecond.
func (r *recorder) follower(room Reader) Follower {
	return Follower{
		Reader:   room,
		Interval: time.Millisecond,
		Message: func(m garden.Message) error {
			r.messages <- m
			return nil
		},
		Notice: func(text string) { r.notices <- text },
		Link:   func(up bool, _ error) { r.links <- up },
	}
}

func waitNotice(t *testing.T, r *recorder) string {
	t.Helper()
	select {
	case text := <-r.notices:
		if text == "" {
			t.Fatal("a resynchronisation must say why")
		}
		return text
	case <-time.After(5 * time.Second):
		t.Fatal("timed out waiting for a notice")
	}
	return ""
}

func waitMessage(t *testing.T, r *recorder) garden.Message {
	t.Helper()
	select {
	case m := <-r.messages:
		return m
	case <-time.After(5 * time.Second):
		t.Fatal("timed out waiting for a message")
	}
	return garden.Message{}
}

// runFollower runs f in the background and returns its result channel and a cancel function.
func runFollower(f Follower, start Window) (<-chan error, context.CancelFunc) {
	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan error, 1)
	go func() { done <- f.Run(ctx, start) }()
	return done, cancel
}

func waitDone(t *testing.T, done <-chan error) error {
	t.Helper()
	select {
	case err := <-done:
		return err
	case <-time.After(5 * time.Second):
		t.Fatal("follower did not stop")
	}
	return nil
}

func TestFollowerDeliversNewMessagesInOrder(t *testing.T) {
	room := newFakeRoom("gen-1")
	room.add("operator", "one")
	room.add("operator", "two")
	start, err := Tail(context.Background(), room, 50)
	if err != nil {
		t.Fatal(err)
	}
	rec := newRecorder()
	done, cancel := runFollower(rec.follower(room), start)

	room.add("claude", "three")
	room.add("claude", "four")
	for want := uint64(3); want <= 4; want++ {
		if got := waitMessage(t, rec); got.Sequence != want {
			t.Fatalf("sequence = %d, want %d", got.Sequence, want)
		}
	}
	cancel()
	if err := waitDone(t, done); err != nil {
		t.Fatalf("Run returned %v after cancel, want nil", err)
	}
	if len(rec.messages) != 0 {
		t.Fatal("no message may be delivered twice")
	}
}

func TestFollowerDrainsPagesWithoutWaiting(t *testing.T) {
	room := newFakeRoom("gen-1")
	start, err := Tail(context.Background(), room, 50)
	if err != nil {
		t.Fatal(err)
	}
	for i := 0; i < 250; i++ {
		room.add("operator", fmt.Sprintf("m%d", i))
	}
	rec := newRecorder()
	f := rec.follower(room)
	f.Interval = time.Hour // any second wait would hang the test
	done, cancel := runFollower(f, start)
	defer cancel()

	for want := uint64(1); want <= 250; want++ {
		if got := waitMessage(t, rec); got.Sequence != want {
			t.Fatalf("sequence = %d, want %d", got.Sequence, want)
		}
	}
	cancel()
	waitDone(t, done)
}

func TestFollowerResyncsFromTailAfterStreamReset(t *testing.T) {
	room := newFakeRoom("gen-1")
	room.add("operator", "old")
	start, err := Tail(context.Background(), room, 50)
	if err != nil {
		t.Fatal(err)
	}
	rec := newRecorder()
	done, cancel := runFollower(rec.follower(room), start)
	defer cancel()

	room.reset("gen-2")
	room.add("operator", "new-1")
	room.add("operator", "new-2")

	waitNotice(t, rec)
	if got := waitMessage(t, rec); got.Content != "new-1" {
		t.Fatalf("first message after reset = %q, want new-1", got.Content)
	}
	if got := waitMessage(t, rec); got.Content != "new-2" {
		t.Fatalf("second message after reset = %q, want new-2", got.Content)
	}
	cancel()
	waitDone(t, done)
}

func TestFollowerResyncsFromTailAfterRetentionGap(t *testing.T) {
	room := newFakeRoom("gen-1")
	for i := 0; i < 9; i++ {
		room.add("operator", fmt.Sprintf("m%d", i+1))
	}
	// The cursor is at 3, but retention now keeps only 8 and 9, so the gap is hit on the first poll.
	room.expire(8)
	rec := newRecorder()
	done, cancel := runFollower(rec.follower(room), Window{Generation: "gen-1", NextSeq: 3})
	defer cancel()

	waitNotice(t, rec)
	for want := uint64(8); want <= 9; want++ {
		if got := waitMessage(t, rec); got.Sequence != want {
			t.Fatalf("sequence after gap = %d, want %d", got.Sequence, want)
		}
	}
	cancel()
	waitDone(t, done)
}

func TestFollowerReportsLinkLossAndRecoveryOnce(t *testing.T) {
	room := newFakeRoom("gen-1")
	room.add("operator", "one")
	start, err := Tail(context.Background(), room, 50)
	if err != nil {
		t.Fatal(err)
	}
	room.failReads = 3
	rec := newRecorder()
	done, cancel := runFollower(rec.follower(room), start)
	defer cancel()

	if up := <-rec.links; up {
		t.Fatal("first link change must be down")
	}
	if up := <-rec.links; !up {
		t.Fatal("second link change must be up")
	}
	room.add("operator", "two")
	if got := waitMessage(t, rec); got.Content != "two" {
		t.Fatalf("message after recovery = %q", got.Content)
	}
	cancel()
	waitDone(t, done)
	if len(rec.links) != 0 {
		t.Fatalf("extra link changes: %d", len(rec.links))
	}
}

func TestFollowerStopsOnForbidden(t *testing.T) {
	room := newFakeRoom("gen-1")
	start, err := Tail(context.Background(), room, 50)
	if err != nil {
		t.Fatal(err)
	}
	room.failCode = "forbidden"
	rec := newRecorder()
	done, cancel := runFollower(rec.follower(room), start)
	defer cancel()

	err = waitDone(t, done)
	if errorCode(err) != "forbidden" {
		t.Fatalf("Run error = %v, want forbidden", err)
	}
}

func TestFollowerReturnsMessageCallbackError(t *testing.T) {
	room := newFakeRoom("gen-1")
	start, err := Tail(context.Background(), room, 50)
	if err != nil {
		t.Fatal(err)
	}
	boom := errors.New("cannot show message")
	f := Follower{Reader: room, Interval: time.Millisecond, Message: func(garden.Message) error { return boom }}
	done, cancel := runFollower(f, start)
	defer cancel()
	room.add("operator", "hello")

	if err := waitDone(t, done); !errors.Is(err, boom) {
		t.Fatalf("Run error = %v, want the callback error", err)
	}
}

func TestTailReturnsNewestWindow(t *testing.T) {
	room := newFakeRoom("gen-1")
	for i := 0; i < 120; i++ {
		room.add("operator", fmt.Sprintf("m%d", i+1))
	}
	w, err := Tail(context.Background(), room, 3)
	if err != nil {
		t.Fatal(err)
	}
	if len(w.Messages) != 3 || w.Messages[0].Sequence != 118 || w.Messages[2].Sequence != 120 {
		t.Fatalf("window = %v, want sequences 118 to 120", sequences(w.Messages))
	}
	if w.NextSeq != 120 || w.Generation != "gen-1" {
		t.Fatalf("cursor = (%q, %d), want (gen-1, 120)", w.Generation, w.NextSeq)
	}
}

func TestTailOfEmptyStreamHasNoMessages(t *testing.T) {
	room := newFakeRoom("gen-1")
	w, err := Tail(context.Background(), room, 50)
	if err != nil {
		t.Fatal(err)
	}
	if len(w.Messages) != 0 || w.NextSeq != 0 {
		t.Fatalf("window = %+v, want empty", w)
	}
}

func TestTailRefusesLegacyGatewayThatReturnsOldestPage(t *testing.T) {
	room := newFakeRoom("gen-1")
	room.legacy = true
	for i := 0; i < 60; i++ {
		room.add("operator", "m")
	}
	_, err := Tail(context.Background(), room, 5)
	if !errors.Is(err, ErrNoTail) {
		t.Fatalf("Tail error = %v, want ErrNoTail", err)
	}
}

func TestTailTrimsLegacyWindowWhenItIsComplete(t *testing.T) {
	room := newFakeRoom("gen-1")
	room.legacy = true
	for i := 0; i < 10; i++ {
		room.add("operator", "m")
	}
	w, err := Tail(context.Background(), room, 5)
	if err != nil {
		t.Fatal(err)
	}
	if got := sequences(w.Messages); fmt.Sprint(got) != "[6 7 8 9 10]" {
		t.Fatalf("window = %v, want 6 to 10", got)
	}
}

func TestTailRefusesGatewayThatRejectsTheArgument(t *testing.T) {
	room := newFakeRoom("gen-1")
	room.rejectTail = true
	room.add("operator", "m")
	_, err := Tail(context.Background(), room, 5)
	if !errors.Is(err, ErrNoTail) {
		t.Fatalf("Tail error = %v, want ErrNoTail", err)
	}
}

func TestTailRejectsOutOfRangeCounts(t *testing.T) {
	room := newFakeRoom("gen-1")
	for _, n := range []int{0, MaxTail + 1, -3} {
		if _, err := Tail(context.Background(), room, n); err == nil {
			t.Fatalf("Tail(%d) succeeded, want an error", n)
		}
	}
	if room.reads != 0 {
		t.Fatal("out-of-range counts must not reach the gateway")
	}
}

func TestConvertKeepsReplyAndTimestamp(t *testing.T) {
	m := garden.Message{ID: "abc", Sequence: 4, AuthorName: "operator", Content: "hi", ReplyTo: "xyz", CreatedAt: "2026-11-08T09:00:04.5+01:00"}
	got, err := Convert(m)
	if err != nil {
		t.Fatal(err)
	}
	if got.ID != "abc" || got.AuthorName != "operator" || got.Content != "hi" {
		t.Fatalf("converted = %+v", got)
	}
	if got.ReplyTo == nil || *got.ReplyTo != "xyz" {
		t.Fatalf("reply = %v, want xyz", got.ReplyTo)
	}
	if got.CreatedAt.UTC().Format(time.RFC3339Nano) != "2026-11-08T08:00:04.5Z" {
		t.Fatalf("created = %s", got.CreatedAt)
	}

	plain, err := Convert(garden.Message{ID: "def", CreatedAt: "2026-11-08T09:00:00Z"})
	if err != nil || plain.ReplyTo != nil {
		t.Fatalf("plain message: %+v, %v", plain, err)
	}
}

func TestConvertRefusesInvalidTimestamp(t *testing.T) {
	if _, err := Convert(garden.Message{ID: "abcdefghijk", CreatedAt: "yesterday"}); err == nil {
		t.Fatal("Convert accepted an invalid timestamp")
	}
}

func sequences(messages []garden.Message) []uint64 {
	out := make([]uint64, 0, len(messages))
	for _, m := range messages {
		out = append(out, m.Sequence)
	}
	return out
}
