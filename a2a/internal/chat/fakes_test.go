package chat

import (
	"context"

	"github.com/veridian69/cairn/a2a/internal/model"
)

type fakeViewService struct {
	sendErr      error
	pauseErr     error
	resumeErr    error
	statusErr    error
	sentIdentity string
	sentContent  string
	sentReplyTo  *string
	pauseAgent   string
	resumeAgent  string

	streamHandler     func(StreamEvent)
	activityHandler   func(ActivityEvent)
	connectionHandler func(ConnectionEvent)

	statuses     []AgentPresence
	statusFunc   func(context.Context) ([]AgentPresence, error)
	redactions   map[string]RedactionStatus
	redactionErr map[string]error
	tailEvents   []StreamEvent
	promoted     *model.Message
	promotedBy   string
	promotedPin  bool
	promoteErr   error
	promoteExist bool
}

func (f *fakeViewService) Tail(context.Context, int) ([]StreamEvent, error) {
	return f.tailEvents, nil
}

func (f *fakeViewService) Subscribe(_ context.Context, handler func(StreamEvent)) error {
	f.streamHandler = handler
	return nil
}

func (f *fakeViewService) Status(ctx context.Context) ([]AgentPresence, error) {
	if f.statusFunc != nil {
		return f.statusFunc(ctx)
	}
	return f.statuses, f.statusErr
}

func (f *fakeViewService) SubscribeActivity(_ context.Context, handler func(ActivityEvent)) error {
	f.activityHandler = handler
	return nil
}

func (f *fakeViewService) SubscribeConnection(_ context.Context, handler func(ConnectionEvent)) error {
	f.connectionHandler = handler
	return nil
}

func (f *fakeViewService) Redaction(messageID string) (RedactionStatus, error) {
	if err := f.redactionErr[messageID]; err != nil {
		return RedactionStatus{}, err
	}
	return f.redactions[messageID], nil
}

func (f *fakeViewService) Send(_ context.Context, identity, content string, replyTo *string) error {
	if f.sendErr != nil {
		return f.sendErr
	}
	f.sentIdentity = identity
	f.sentContent = content
	f.sentReplyTo = replyTo
	return nil
}

func (f *fakeViewService) Promote(humanIdentity string, message model.Message, pin bool) (bool, error) {
	f.promoted = &message
	f.promotedBy = humanIdentity
	f.promotedPin = pin
	return f.promoteExist, f.promoteErr
}

func (f *fakeViewService) Pause(_ context.Context, agent string) error {
	if f.pauseErr != nil {
		return f.pauseErr
	}
	f.pauseAgent = agent
	return nil
}

func (f *fakeViewService) Resume(_ context.Context, agent string) error {
	if f.resumeErr != nil {
		return f.resumeErr
	}
	f.resumeAgent = agent
	return nil
}

func (f *fakeViewService) Close() {}
