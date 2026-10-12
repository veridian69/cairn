package chat

import (
	"context"
	"testing"
	"time"

	tea "github.com/charmbracelet/bubbletea"
)

type recordingSender struct {
	msgs []tea.Msg
}

func (r *recordingSender) Send(msg tea.Msg) {
	r.msgs = append(r.msgs, msg)
}

func TestRunSubscriptionsBridgesEventsUntilCancelled(t *testing.T) {
	svc := &fakeViewService{}
	sender := &recordingSender{}
	ctx, cancel := context.WithCancel(context.Background())
	if err := runSubscriptions(ctx, svc, sender); err != nil {
		t.Fatalf("runSubscriptions: %v", err)
	}

	svc.streamHandler(StreamEvent{})
	svc.activityHandler(ActivityEvent{Agent: "claude", State: "thinking"})
	svc.connectionHandler(ConnectionEvent{State: "disconnected"})

	if len(sender.msgs) != 3 {
		t.Fatalf("got %d messages, want 3", len(sender.msgs))
	}
	if _, ok := sender.msgs[0].(streamMsg); !ok {
		t.Fatalf("message 0 = %T, want streamMsg", sender.msgs[0])
	}
	if _, ok := sender.msgs[1].(activityMsg); !ok {
		t.Fatalf("message 1 = %T, want activityMsg", sender.msgs[1])
	}
	if _, ok := sender.msgs[2].(connectionMsg); !ok {
		t.Fatalf("message 2 = %T, want connectionMsg", sender.msgs[2])
	}

	cancel()
	svc.streamHandler(StreamEvent{})
	if len(sender.msgs) != 3 {
		t.Fatal("cancelled subscription forwarded a late event")
	}
}

func TestStatusCmdReturnsGenerationAndStatuses(t *testing.T) {
	svc := &fakeViewService{
		statuses: []AgentPresence{{Name: "claude", State: "active"}},
	}
	msg := statusCmd(context.Background(), svc, 7)()
	status, ok := msg.(statusMsg)
	if !ok {
		t.Fatalf("message = %T, want statusMsg", msg)
	}
	if status.generation != 7 || status.err != nil ||
		len(status.statuses) != 1 || status.statuses[0].State != "active" {
		t.Fatalf("unexpected status message: %+v", status)
	}
}

func TestControlCmdRecordsActionAndSubmission(t *testing.T) {
	svc := &fakeViewService{}
	msg := controlCmd(
		context.Background(), svc, 11,
		"pause", "claude", "/pause claude", 4,
	)()
	result, ok := msg.(controlResultMsg)
	if !ok || result.err != nil || result.actionID != 11 ||
		result.action != "pause" || result.agent != "claude" ||
		result.submission != "/pause claude" || result.revision != 4 {
		t.Fatalf("unexpected result: %#v", msg)
	}
	if svc.pauseAgent != "claude" {
		t.Fatalf("pause agent = %q, want claude", svc.pauseAgent)
	}
}

func TestPollTickDeliversGeneration(t *testing.T) {
	msg := pollTick(time.Millisecond, 9)()
	poll, ok := msg.(pollMsg)
	if !ok || poll.generation != 9 {
		t.Fatalf("message = %#v, want generation 9 pollMsg", msg)
	}
}

func TestRedactionCmdReturnsMessageScopedResult(t *testing.T) {
	svc := &fakeViewService{
		redactions: map[string]RedactionStatus{
			"m1": {Redacted: true, Reason: "operator-request"},
		},
	}
	msg := redactionCmd(svc, "m1")()
	result, ok := msg.(redactionResultMsg)
	if !ok {
		t.Fatalf("message = %T, want redactionResultMsg", msg)
	}
	if result.messageID != "m1" || !result.status.Redacted ||
		result.status.Reason != "operator-request" || result.err != nil {
		t.Fatalf("unexpected redaction result: %+v", result)
	}
}
