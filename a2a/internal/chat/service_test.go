package chat

import (
	"context"
	"encoding/json"
	"sync"
	"testing"
	"time"

	"github.com/nats-io/nats.go"
	"github.com/veridian69/cairn/a2a/internal/daemon"
	"github.com/veridian69/cairn/a2a/internal/memory"
	"github.com/veridian69/cairn/a2a/internal/model"
	"github.com/veridian69/cairn/a2a/internal/state"
	transportpkg "github.com/veridian69/cairn/a2a/internal/transport"
)

type fakeTransport struct {
	mu        sync.Mutex
	published []model.Message
	tail      []transportpkg.ReplayEntry
	handler   func(model.Message, uint64) error
}

func (f *fakeTransport) TailWithSeq(ctx context.Context, limit int) ([]transportpkg.ReplayEntry, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	if limit <= 0 {
		return []transportpkg.ReplayEntry{}, nil
	}
	if len(f.tail) <= limit {
		return append([]transportpkg.ReplayEntry(nil), f.tail...), nil
	}
	return append([]transportpkg.ReplayEntry(nil), f.tail[len(f.tail)-limit:]...), nil
}

func (f *fakeTransport) Subscribe(ctx context.Context, handler func(model.Message, uint64) error) error {
	f.mu.Lock()
	f.handler = handler
	f.mu.Unlock()
	return nil
}

func (f *fakeTransport) Publish(ctx context.Context, msg model.Message) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.published = append(f.published, msg)
	return nil
}

func (f *fakeTransport) Close() {}

func newTestService(
	t *testing.T,
	stream Transport,
	db *state.DB,
	nc *nats.Conn,
) Service {
	t.Helper()
	return NewServiceWithDeps(stream, db, nc, t.TempDir(), 4096)
}

func TestServiceSendHumanAndAgent(t *testing.T) {
	db, err := state.Open(t.TempDir() + "/state.db")
	if err != nil {
		t.Fatalf("Open state: %v", err)
	}
	defer db.Close()

	fake := &fakeTransport{}
	svc := newTestService(t, fake, db, nil)
	defer svc.Close()

	agentParticipant, err := db.RegisterParticipant(model.Participant{
		Name:     "claude",
		Kind:     model.KindAgent,
		Provider: "anthropic",
		Model:    "claude-sonnet",
	})
	if err != nil {
		t.Fatalf("RegisterParticipant agent: %v", err)
	}

	ctx := context.Background()
	if err := svc.Send(ctx, "", "hello human", nil); err != nil {
		t.Fatalf("Send human: %v", err)
	}
	if len(fake.published) != 1 {
		t.Fatalf("got %d published messages, want 1", len(fake.published))
	}
	humanMsg := fake.published[0]
	if humanMsg.Content != "hello human" {
		t.Fatalf("human content = %q", humanMsg.Content)
	}
	if humanMsg.Metadata["seeded_by"] != nil {
		t.Fatal("human send should not include seeded_by metadata")
	}
	participants, err := db.ListParticipants()
	if err != nil {
		t.Fatalf("ListParticipants: %v", err)
	}
	if len(participants) != 2 {
		t.Fatalf("got %d participants, want 2", len(participants))
	}
	var human model.Participant
	for _, p := range participants {
		if p.Kind == model.KindHuman {
			human = p
			break
		}
	}
	if human.ID == "" {
		t.Fatal("expected registered human participant")
	}
	if humanMsg.AuthorID != human.ID {
		t.Fatalf("human author id = %q, want %q", humanMsg.AuthorID, human.ID)
	}

	if err := svc.Send(ctx, "claude", "seeded hello", nil); err != nil {
		t.Fatalf("Send agent: %v", err)
	}
	if len(fake.published) != 2 {
		t.Fatalf("got %d published messages, want 2", len(fake.published))
	}
	agentMsg := fake.published[1]
	if agentMsg.AuthorID != agentParticipant.ID {
		t.Fatalf("agent author id = %q, want %q", agentMsg.AuthorID, agentParticipant.ID)
	}
	if agentMsg.AuthorName != "claude" {
		t.Fatalf("agent author name = %q", agentMsg.AuthorName)
	}
	if agentMsg.Metadata["authorship_mode"] != "seeded" {
		t.Fatalf("authorship_mode = %#v, want seeded", agentMsg.Metadata["authorship_mode"])
	}
	if agentMsg.Metadata["seeded_by"] != human.ID {
		t.Fatalf("seeded_by = %#v, want %q", agentMsg.Metadata["seeded_by"], human.ID)
	}

	if err := svc.Send(ctx, "operator-custom", "human alias", nil); err != nil {
		t.Fatalf("Send explicit human alias: %v", err)
	}
	if len(fake.published) != 3 {
		t.Fatalf("got %d published messages, want 3", len(fake.published))
	}
	aliasMsg := fake.published[2]
	if aliasMsg.AuthorName != "operator-custom" {
		t.Fatalf("alias author name = %q, want operator-custom", aliasMsg.AuthorName)
	}
	if aliasMsg.Metadata["seeded_by"] != nil {
		t.Fatalf("explicit human alias should not create seeded metadata: %#v", aliasMsg.Metadata)
	}
}

func TestServicePromoteUsesConfiguredMemoryStore(t *testing.T) {
	dataDir := t.TempDir()
	db, err := state.Open(dataDir + "/state.db")
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()
	svc := NewServiceWithDeps(&fakeTransport{}, db, nil, dataDir, 4096)
	defer svc.Close()

	source := model.Participant{ID: "agent", Name: "claude", Kind: model.KindAgent}
	message := model.NewMessage(source, "remember this", nil)
	existed, err := svc.Promote("operator", message, true)
	if err != nil || existed {
		t.Fatalf("Promote existed=%v err=%v", existed, err)
	}
	store, err := memory.Open(dataDir, memory.Options{})
	if err != nil {
		t.Fatal(err)
	}
	defer store.Close()
	items, err := store.List(memory.ListFilter{}, map[string]bool{})
	if err != nil {
		t.Fatal(err)
	}
	if len(items) != 1 || !items[0].Pinned || items[0].SourceMessageID != message.ID {
		t.Fatalf("promoted items = %+v", items)
	}
	nominator, err := db.GetParticipantByName("operator")
	if err != nil {
		t.Fatal(err)
	}
	if items[0].NominatedBy != nominator.ID || nominator.Kind != model.KindHuman {
		t.Fatalf("nominator=%+v item=%+v", nominator, items[0])
	}
}

func TestServiceStatusControlAndActivity(t *testing.T) {
	srv, err := transportpkg.NewServer(t.TempDir())
	if err != nil {
		t.Fatalf("NewServer: %v", err)
	}
	defer srv.Stop()

	db, err := state.Open(t.TempDir() + "/state.db")
	if err != nil {
		t.Fatalf("Open state: %v", err)
	}
	defer db.Close()

	nc, err := nats.Connect(srv.ClientURL())
	if err != nil {
		t.Fatalf("Connect: %v", err)
	}
	defer nc.Close()

	fake := &fakeTransport{
		tail: []transportpkg.ReplayEntry{
			{Message: model.NewMessage(model.Participant{ID: "a1", Name: "claude", Kind: model.KindAgent}, "older", nil), Seq: 1},
			{Message: model.NewMessage(model.Participant{ID: "a2", Name: "gemini", Kind: model.KindAgent}, "newer", nil), Seq: 2},
		},
	}
	svc := newTestService(t, fake, db, nc)
	defer svc.Close()

	lastActivity := time.Now().UTC().Round(time.Second)
	if _, err := nc.Subscribe("a2a.status", func(msg *nats.Msg) {
		payload, marshalErr := json.Marshal([]daemon.AgentStatus{{
			Name:           "claude",
			Provider:       "anthropic",
			Model:          "claude-sonnet",
			Active:         true,
			State:          "thinking",
			Responsiveness: 0.7,
			QueueDepth:     2,
			LastSeenSeq:    11,
			LastActivityAt: lastActivity,
		}})
		if marshalErr == nil {
			_ = msg.Respond(payload)
		}
	}); err != nil {
		t.Fatalf("status subscribe: %v", err)
	}

	controlSub, err := nc.SubscribeSync("a2a.control")
	if err != nil {
		t.Fatalf("control subscribe: %v", err)
	}
	if err := nc.Flush(); err != nil {
		t.Fatalf("Flush: %v", err)
	}

	events, err := svc.Tail(context.Background(), 1)
	if err != nil {
		t.Fatalf("Tail: %v", err)
	}
	if len(events) != 1 || events[0].Seq != 2 {
		t.Fatalf("unexpected tail result: %+v", events)
	}

	statuses, err := svc.Status(context.Background())
	if err != nil {
		t.Fatalf("Status: %v", err)
	}
	if len(statuses) != 1 {
		t.Fatalf("got %d statuses, want 1", len(statuses))
	}
	if statuses[0].State != "thinking" {
		t.Fatalf("state = %q, want thinking", statuses[0].State)
	}
	if !statuses[0].LastActivityAt.Equal(lastActivity) {
		t.Fatalf("LastActivityAt = %v, want %v", statuses[0].LastActivityAt, lastActivity)
	}
	if err := svc.Pause(context.Background(), "claude"); err != nil {
		t.Fatalf("Pause: %v", err)
	}
	pauseMsg, err := controlSub.NextMsg(2 * time.Second)
	if err != nil {
		t.Fatalf("NextMsg pause: %v", err)
	}
	if string(pauseMsg.Data) != "pause:claude" {
		t.Fatalf("pause payload = %q", string(pauseMsg.Data))
	}

	if err := svc.Resume(context.Background(), "claude"); err != nil {
		t.Fatalf("Resume: %v", err)
	}
	resumeMsg, err := controlSub.NextMsg(2 * time.Second)
	if err != nil {
		t.Fatalf("NextMsg resume: %v", err)
	}
	if string(resumeMsg.Data) != "resume:claude" {
		t.Fatalf("resume payload = %q", string(resumeMsg.Data))
	}

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	activityCh := make(chan ActivityEvent, 1)
	if err := svc.SubscribeActivity(ctx, func(event ActivityEvent) {
		select {
		case activityCh <- event:
		default:
		}
	}); err != nil {
		t.Fatalf("SubscribeActivity: %v", err)
	}
	payload, err := json.Marshal(daemon.ActivityEvent{
		Agent: "claude",
		State: "thinking",
		At:    lastActivity,
	})
	if err != nil {
		t.Fatalf("Marshal activity: %v", err)
	}
	if err := nc.Publish("a2a.activity", payload); err != nil {
		t.Fatalf("Publish activity: %v", err)
	}
	if err := nc.Flush(); err != nil {
		t.Fatalf("Flush activity: %v", err)
	}

	select {
	case event := <-activityCh:
		if event.Agent != "claude" || event.State != "thinking" || !event.At.Equal(lastActivity) {
			t.Fatalf("unexpected activity event: %+v", event)
		}
	case <-time.After(2 * time.Second):
		t.Fatal("timed out waiting for activity event")
	}
}

func TestServiceStatusBackfillsStateFromActiveFlag(t *testing.T) {
	srv, err := transportpkg.NewServer(t.TempDir())
	if err != nil {
		t.Fatalf("NewServer: %v", err)
	}
	defer srv.Stop()

	db, err := state.Open(t.TempDir() + "/state.db")
	if err != nil {
		t.Fatalf("Open state: %v", err)
	}
	defer db.Close()

	nc, err := nats.Connect(srv.ClientURL())
	if err != nil {
		t.Fatalf("Connect: %v", err)
	}
	defer nc.Close()

	svc := newTestService(t, &fakeTransport{}, db, nc)
	defer svc.Close()

	if _, err := nc.Subscribe("a2a.status", func(msg *nats.Msg) {
		payload, marshalErr := json.Marshal([]daemon.AgentStatus{
			{Name: "active-agent", Active: true},
			{Name: "paused-agent", Active: false},
		})
		if marshalErr == nil {
			_ = msg.Respond(payload)
		}
	}); err != nil {
		t.Fatalf("status subscribe: %v", err)
	}
	if err := nc.Flush(); err != nil {
		t.Fatalf("Flush: %v", err)
	}

	statuses, err := svc.Status(context.Background())
	if err != nil {
		t.Fatalf("Status: %v", err)
	}
	if len(statuses) != 2 {
		t.Fatalf("got %d statuses, want 2", len(statuses))
	}
	if statuses[0].State != "active" {
		t.Fatalf("first state = %q, want active", statuses[0].State)
	}
	if statuses[1].State != "paused" {
		t.Fatalf("second state = %q, want paused", statuses[1].State)
	}
}

func TestServiceStatusDecodeError(t *testing.T) {
	srv, err := transportpkg.NewServer(t.TempDir())
	if err != nil {
		t.Fatalf("NewServer: %v", err)
	}
	defer srv.Stop()

	db, err := state.Open(t.TempDir() + "/state.db")
	if err != nil {
		t.Fatalf("Open state: %v", err)
	}
	defer db.Close()

	nc, err := nats.Connect(srv.ClientURL())
	if err != nil {
		t.Fatalf("Connect: %v", err)
	}
	defer nc.Close()

	if _, err := nc.Subscribe("a2a.status", func(msg *nats.Msg) {
		_ = msg.Respond([]byte("{not-json"))
	}); err != nil {
		t.Fatalf("status subscribe: %v", err)
	}
	if err := nc.Flush(); err != nil {
		t.Fatalf("Flush: %v", err)
	}

	svc := newTestService(t, &fakeTransport{}, db, nc)
	defer svc.Close()

	if _, err := svc.Status(context.Background()); err == nil {
		t.Fatal("expected decode error, got nil")
	}
}

func TestServiceSubscribeConnection(t *testing.T) {
	db, err := state.Open(t.TempDir() + "/state.db")
	if err != nil {
		t.Fatalf("Open state: %v", err)
	}
	defer db.Close()

	svc := newTestService(t, &fakeTransport{}, db, nil).(*service)
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()

	events := make(chan ConnectionEvent, 1)
	if err := svc.SubscribeConnection(ctx, func(event ConnectionEvent) {
		events <- event
	}); err != nil {
		t.Fatalf("SubscribeConnection: %v", err)
	}

	svc.emitConnection(ConnectionEvent{State: "disconnected"})

	select {
	case event := <-events:
		if event.State != "disconnected" {
			t.Fatalf("event.State = %q, want disconnected", event.State)
		}
	case <-time.After(2 * time.Second):
		t.Fatal("timed out waiting for connection event")
	}
}

func TestServiceRedaction(t *testing.T) {
	db, err := state.Open(t.TempDir() + "/state.db")
	if err != nil {
		t.Fatalf("Open state: %v", err)
	}
	defer db.Close()

	if err := db.Redact("msg-1", "operator-request", "human-1"); err != nil {
		t.Fatalf("Redact: %v", err)
	}

	svc := newTestService(t, &fakeTransport{}, db, nil)
	defer svc.Close()

	status, err := svc.Redaction("msg-1")
	if err != nil {
		t.Fatalf("Redaction: %v", err)
	}
	if !status.Redacted || status.Reason != "operator-request" {
		t.Fatalf("unexpected status: %+v", status)
	}

	status, err = svc.Redaction("msg-2")
	if err != nil {
		t.Fatalf("Redaction missing: %v", err)
	}
	if status.Redacted {
		t.Fatalf("unexpected redacted missing status: %+v", status)
	}
}
