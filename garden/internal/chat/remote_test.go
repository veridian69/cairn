package chat

import (
	"context"
	"errors"
	"fmt"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/veridian69/cairn/garden/internal/garden"
	"github.com/veridian69/cairn/garden/internal/model"
)

// fakeRemoteGateway is a scripted gateway: messages with sequences from 1, tail reads, after-sequence
// reads with a generation, and injected failures.
type fakeRemoteGateway struct {
	mu        sync.Mutex
	messages  []garden.Message
	reads     int
	failReads int
	failCode  string
	sent      []garden.SendArgs
	who       garden.WhoResult
	whoErr    error
	closed    int
}

func (g *fakeRemoteGateway) add(author, content string) {
	g.mu.Lock()
	defer g.mu.Unlock()
	seq := uint64(len(g.messages) + 1)
	g.messages = append(g.messages, garden.Message{
		ID:         fmt.Sprintf("00000000-0000-4000-8000-%012d", seq),
		Sequence:   seq,
		AuthorName: author,
		Content:    content,
		CreatedAt:  "2026-11-08T09:00:00Z",
	})
}

func (g *fakeRemoteGateway) Read(_ context.Context, a garden.ReadArgs) (garden.ReadResult, error) {
	g.mu.Lock()
	defer g.mu.Unlock()
	g.reads++
	if g.failCode != "" {
		return garden.ReadResult{}, &garden.Error{Code: g.failCode, Message: "refused"}
	}
	if g.failReads > 0 {
		g.failReads--
		return garden.ReadResult{}, &garden.Error{Code: "unavailable", Message: "gateway unavailable"}
	}
	last := uint64(len(g.messages))
	if a.Tail > 0 {
		start := uint64(1)
		if last > uint64(a.Tail) {
			start = last - uint64(a.Tail) + 1
		}
		return g.page(start, last, last), nil
	}
	if a.AfterSeq > last {
		return garden.ReadResult{}, &garden.Error{Code: "stream_reset", Message: "beyond tail"}
	}
	end := a.AfterSeq + 100
	if end > last {
		end = last
	}
	return g.page(a.AfterSeq+1, end, a.AfterSeq), nil
}

func (g *fakeRemoteGateway) page(start, end, next uint64) garden.ReadResult {
	out := garden.ReadResult{Messages: []garden.Message{}, Generation: "gen-1", NextSeq: next}
	for seq := start; seq <= end && seq >= 1; seq++ {
		out.Messages = append(out.Messages, g.messages[seq-1])
		out.NextSeq = seq
	}
	return out
}

func (g *fakeRemoteGateway) readCount() int {
	g.mu.Lock()
	defer g.mu.Unlock()
	return g.reads
}

func (g *fakeRemoteGateway) Send(_ context.Context, a garden.SendArgs) (garden.SendResult, error) {
	g.mu.Lock()
	defer g.mu.Unlock()
	g.sent = append(g.sent, a)
	return garden.SendResult{}, nil
}

func (g *fakeRemoteGateway) Who(context.Context) (garden.WhoResult, error) {
	g.mu.Lock()
	defer g.mu.Unlock()
	return g.who, g.whoErr
}

func (g *fakeRemoteGateway) Close() error {
	g.mu.Lock()
	defer g.mu.Unlock()
	g.closed++
	return nil
}

func (g *fakeRemoteGateway) sentArgs() []garden.SendArgs {
	g.mu.Lock()
	defer g.mu.Unlock()
	return append([]garden.SendArgs(nil), g.sent...)
}

// remoteEvents collects stream events and connection events from a remote service.
type remoteEvents struct {
	stream chan StreamEvent
	conn   chan ConnectionEvent
}

func subscribeRemote(t *testing.T, svc Service) remoteEvents {
	t.Helper()
	ev := remoteEvents{stream: make(chan StreamEvent, 100), conn: make(chan ConnectionEvent, 100)}
	ctx := context.Background()
	// Connection handlers go first: the first poll may fail at once, and only registered handlers hear it.
	if err := svc.SubscribeConnection(ctx, func(e ConnectionEvent) { ev.conn <- e }); err != nil {
		t.Fatalf("SubscribeConnection: %v", err)
	}
	if err := svc.Subscribe(ctx, func(e StreamEvent) { ev.stream <- e }); err != nil {
		t.Fatalf("Subscribe: %v", err)
	}
	return ev
}

func nextStream(t *testing.T, ev remoteEvents) StreamEvent {
	t.Helper()
	select {
	case e := <-ev.stream:
		return e
	case <-time.After(5 * time.Second):
		t.Fatal("timed out waiting for a stream event")
	}
	return StreamEvent{}
}

func nextConnection(t *testing.T, ev remoteEvents) ConnectionEvent {
	t.Helper()
	select {
	case e := <-ev.conn:
		return e
	case <-time.After(5 * time.Second):
		t.Fatal("timed out waiting for a connection event")
	}
	return ConnectionEvent{}
}

func TestRemoteTailReturnsNewestMessagesWithSequence(t *testing.T) {
	gw := &fakeRemoteGateway{}
	for i := 1; i <= 5; i++ {
		gw.add("operator", fmt.Sprintf("m%d", i))
	}
	gw.messages[4].ReplyTo = "00000000-0000-4000-8000-000000000002"
	svc := newRemoteService(gw, "operator", time.Millisecond)
	defer svc.Close()

	events, err := svc.Tail(context.Background(), 3)
	if err != nil {
		t.Fatalf("Tail: %v", err)
	}
	if len(events) != 3 || events[0].Seq != 3 || events[2].Seq != 5 {
		t.Fatalf("events = %+v, want sequences 3 to 5", events)
	}
	if events[2].Message.Content != "m5" || events[2].Message.AuthorName != "operator" {
		t.Fatalf("last event = %+v", events[2].Message)
	}
	if events[2].Message.ReplyTo == nil || *events[2].Message.ReplyTo != "00000000-0000-4000-8000-000000000002" {
		t.Fatalf("reply to = %v", events[2].Message.ReplyTo)
	}
}

func TestRemoteSubscribeContinuesAfterTailWithoutRepeats(t *testing.T) {
	gw := &fakeRemoteGateway{}
	gw.add("operator", "one")
	gw.add("operator", "two")
	svc := newRemoteService(gw, "operator", time.Millisecond)
	defer svc.Close()
	if _, err := svc.Tail(context.Background(), 50); err != nil {
		t.Fatal(err)
	}
	ev := subscribeRemote(t, svc)

	gw.add("claude", "three")
	got := nextStream(t, ev)
	if got.Seq != 3 || got.Message.Content != "three" {
		t.Fatalf("first streamed event = seq %d %q, want seq 3 three", got.Seq, got.Message.Content)
	}
	select {
	case extra := <-ev.stream:
		t.Fatalf("unexpected repeated event %+v", extra)
	case <-time.After(20 * time.Millisecond):
	}
}

func TestRemoteSubscribeWithoutTailStartsAtNewest(t *testing.T) {
	gw := &fakeRemoteGateway{}
	gw.add("operator", "old")
	svc := newRemoteService(gw, "operator", time.Millisecond)
	defer svc.Close()
	ev := subscribeRemote(t, svc)

	gw.add("operator", "new")
	if got := nextStream(t, ev); got.Message.Content != "new" {
		t.Fatalf("first event = %q, want new", got.Message.Content)
	}
}

func TestRemoteReportsDisconnectAndReconnect(t *testing.T) {
	gw := &fakeRemoteGateway{}
	gw.add("operator", "one")
	svc := newRemoteService(gw, "operator", time.Millisecond)
	defer svc.Close()
	if _, err := svc.Tail(context.Background(), 50); err != nil {
		t.Fatal(err)
	}
	gw.mu.Lock()
	gw.failReads = 2
	gw.mu.Unlock()
	ev := subscribeRemote(t, svc)

	down := nextConnection(t, ev)
	if down.State != "disconnected" || down.Err == nil {
		t.Fatalf("first connection event = %+v, want disconnected with an error", down)
	}
	up := nextConnection(t, ev)
	if up.State != "reconnected" {
		t.Fatalf("second connection event = %+v, want reconnected", up)
	}
}

func TestRemoteFatalRefusalClosesConnection(t *testing.T) {
	gw := &fakeRemoteGateway{}
	svc := newRemoteService(gw, "operator", time.Millisecond)
	defer svc.Close()
	if _, err := svc.Tail(context.Background(), 50); err != nil {
		t.Fatal(err)
	}
	gw.mu.Lock()
	gw.failCode = "forbidden"
	gw.mu.Unlock()
	ev := subscribeRemote(t, svc)

	got := nextConnection(t, ev)
	if got.State != "closed" || got.Err == nil {
		t.Fatalf("connection event = %+v, want closed with the refusal", got)
	}
}

func TestRemoteSendUsesReplyTo(t *testing.T) {
	gw := &fakeRemoteGateway{}
	svc := newRemoteService(gw, "operator", time.Millisecond)
	defer svc.Close()

	reply := "00000000-0000-4000-8000-000000000007"
	if err := svc.Send(context.Background(), "operator", "answer", &reply); err != nil {
		t.Fatalf("Send with reply: %v", err)
	}
	if err := svc.Send(context.Background(), "", "plain", nil); err != nil {
		t.Fatalf("Send without identity: %v", err)
	}
	sent := gw.sentArgs()
	if len(sent) != 2 {
		t.Fatalf("sent %d messages, want 2", len(sent))
	}
	if sent[0].Content != "answer" || sent[0].ReplyTo != reply {
		t.Fatalf("first send = %+v, want reply to %s", sent[0], reply)
	}
	if sent[1].Content != "plain" || sent[1].ReplyTo != "" {
		t.Fatalf("second send = %+v", sent[1])
	}
}

func TestRemoteSendRefusesAnotherIdentity(t *testing.T) {
	gw := &fakeRemoteGateway{}
	svc := newRemoteService(gw, "operator", time.Millisecond)
	defer svc.Close()

	err := svc.Send(context.Background(), "claude", "speaking for you", nil)
	if err == nil || !strings.Contains(err.Error(), "sends only as operator") {
		t.Fatalf("Send error = %v, want a refusal to send as claude", err)
	}
	if len(gw.sentArgs()) != 0 {
		t.Fatal("a refused identity must not reach the gateway")
	}
}

func TestRemoteUnsupportedOperationsSayNotAvailable(t *testing.T) {
	svc := newRemoteService(&fakeRemoteGateway{}, "operator", time.Millisecond)
	defer svc.Close()
	ctx := context.Background()

	if pinned, err := svc.Promote("operator", model.Message{ID: "x"}, true); pinned || err == nil || !strings.Contains(err.Error(), "not available in remote mode") {
		t.Fatalf("Promote = (%v, %v), want not available", pinned, err)
	}
	if err := svc.Pause(ctx, "claude"); err == nil || !strings.Contains(err.Error(), "not available in remote mode") {
		t.Fatalf("Pause error = %v", err)
	}
	if err := svc.Resume(ctx, "claude"); err == nil || !strings.Contains(err.Error(), "not available in remote mode") {
		t.Fatalf("Resume error = %v", err)
	}
}

func TestRemoteActivityAndRedactionAreNoOps(t *testing.T) {
	svc := newRemoteService(&fakeRemoteGateway{}, "operator", time.Millisecond)
	defer svc.Close()

	if err := svc.SubscribeActivity(context.Background(), func(ActivityEvent) { t.Error("no activity events") }); err != nil {
		t.Fatalf("SubscribeActivity: %v", err)
	}
	status, err := svc.Redaction("any")
	if err != nil || status.Redacted {
		t.Fatalf("Redaction = (%+v, %v), want not redacted", status, err)
	}
}

func TestRemoteStatusMapsWhoToPresence(t *testing.T) {
	gw := &fakeRemoteGateway{who: garden.WhoResult{Participants: []garden.Presence{
		{Name: "claude", Online: true, Deliverable: true},
		{Name: "codex", Online: true, Deliverable: false},
		{Name: "gpt", Online: false},
	}}}
	svc := newRemoteService(gw, "operator", time.Millisecond)
	defer svc.Close()

	statuses, err := svc.Status(context.Background())
	if err != nil {
		t.Fatalf("Status: %v", err)
	}
	want := map[string]string{"claude": "active", "codex": "idle", "gpt": "offline"}
	if len(statuses) != len(want) {
		t.Fatalf("statuses = %+v", statuses)
	}
	for _, s := range statuses {
		if want[s.Name] != s.State {
			t.Fatalf("%s state = %q, want %q", s.Name, s.State, want[s.Name])
		}
	}
}

func TestRemoteStatusReportsWhoFailure(t *testing.T) {
	gw := &fakeRemoteGateway{whoErr: errors.New("gateway predates who")}
	svc := newRemoteService(gw, "operator", time.Millisecond)
	defer svc.Close()

	if _, err := svc.Status(context.Background()); err == nil {
		t.Fatal("Status must report a gateway that cannot list participants")
	}
}

func TestRemoteCloseStopsPollingAndClosesGatewayOnce(t *testing.T) {
	gw := &fakeRemoteGateway{}
	svc := newRemoteService(gw, "operator", time.Millisecond)
	ev := subscribeRemote(t, svc)
	gw.add("operator", "hello")
	nextStream(t, ev)

	svc.Close()
	svc.Close()
	after := gw.readCount()
	time.Sleep(30 * time.Millisecond)
	if gw.readCount() != after {
		t.Fatalf("polling continued after Close: reads %d then %d", after, gw.readCount())
	}
	gw.mu.Lock()
	closed := gw.closed
	gw.mu.Unlock()
	if closed != 1 {
		t.Fatalf("gateway closed %d times, want 1", closed)
	}
	if err := svc.Subscribe(context.Background(), func(StreamEvent) {}); err == nil {
		t.Fatal("Subscribe after Close must fail")
	}
}

func TestRemoteSubscribeStopsWhenItsContextEnds(t *testing.T) {
	gw := &fakeRemoteGateway{}
	svc := newRemoteService(gw, "operator", time.Millisecond)
	defer svc.Close()
	ctx, cancel := context.WithCancel(context.Background())
	if err := svc.Subscribe(ctx, func(StreamEvent) {}); err != nil {
		t.Fatal(err)
	}
	for gw.readCount() == 0 {
		time.Sleep(time.Millisecond)
	}
	cancel()
	time.Sleep(20 * time.Millisecond)
	after := gw.readCount()
	time.Sleep(30 * time.Millisecond)
	if gw.readCount() != after {
		t.Fatal("polling continued after the subscription context ended")
	}
}
