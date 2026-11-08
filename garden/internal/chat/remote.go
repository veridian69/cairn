package chat

import (
	"context"
	"errors"
	"fmt"
	"sync"
	"time"

	"github.com/veridian69/cairn/garden/internal/garden"
	"github.com/veridian69/cairn/garden/internal/model"
	"github.com/veridian69/cairn/garden/internal/remotecli"
)

// RemoteGateway is the part of a remote Garden session the chat screen uses. *remotecli.Session
// satisfies it, and tests substitute a fake.
type RemoteGateway interface {
	remotecli.Reader
	Send(context.Context, garden.SendArgs) (garden.SendResult, error)
	Who(context.Context) (garden.WhoResult, error)
	Close() error
}

// remoteBacklog is how many messages a subscription shows when it starts without a prior Tail.
const remoteBacklog = 50

type remoteService struct {
	gateway  RemoteGateway
	identity string
	interval time.Duration

	// ctx stops every follow loop when Close is called.
	ctx       context.Context
	cancel    context.CancelFunc
	wg        sync.WaitGroup
	closeOnce sync.Once

	mu            sync.Mutex
	closed        bool
	cursor        *remotecli.Window
	nextHandlerID int
	connHandlers  map[int]func(ConnectionEvent)
}

// NewRemoteService returns a Service over a remote Garden session. Messages arrive by polling
// read_messages every remotecli.PollInterval. Sends are made as identity, the session's own
// Garden participant; Close stops polling and closes the gateway.
func NewRemoteService(gateway RemoteGateway, identity string) Service {
	return newRemoteService(gateway, identity, remotecli.PollInterval)
}

func newRemoteService(gateway RemoteGateway, identity string, interval time.Duration) *remoteService {
	ctx, cancel := context.WithCancel(context.Background())
	return &remoteService{
		gateway:      gateway,
		identity:     identity,
		interval:     interval,
		ctx:          ctx,
		cancel:       cancel,
		connHandlers: make(map[int]func(ConnectionEvent)),
	}
}

var _ Service = (*remoteService)(nil)

// Tail returns the newest limit messages and remembers where they end, so Subscribe continues
// from there without repeating them.
func (s *remoteService) Tail(ctx context.Context, limit int) ([]StreamEvent, error) {
	w, err := remotecli.Tail(ctx, s.gateway, limit)
	if err != nil {
		return nil, err
	}
	events, err := streamEvents(w.Messages)
	if err != nil {
		return nil, err
	}
	s.mu.Lock()
	s.cursor = &w
	s.mu.Unlock()
	return events, nil
}

// Subscribe polls for messages after the last Tail until ctx ends or Close is called. Without a prior
// Tail it starts from the newest remoteBacklog messages.
func (s *remoteService) Subscribe(ctx context.Context, handler func(StreamEvent)) error {
	s.mu.Lock()
	closed := s.closed
	start := s.cursor
	s.mu.Unlock()
	if closed {
		return errors.New("remote chat is closed")
	}
	if start == nil {
		w, err := remotecli.Tail(ctx, s.gateway, remoteBacklog)
		if err != nil {
			return err
		}
		start = &w
	}

	loopCtx, cancel := context.WithCancel(ctx)
	stop := context.AfterFunc(s.ctx, cancel)
	s.mu.Lock()
	if s.closed {
		s.mu.Unlock()
		stop()
		cancel()
		return errors.New("remote chat is closed")
	}
	s.wg.Add(1)
	s.mu.Unlock()

	follower := remotecli.Follower{
		Reader:   s.gateway,
		Interval: s.interval,
		Message: func(m garden.Message) error {
			msg, err := remotecli.Convert(m)
			if err != nil {
				return err
			}
			if loopCtx.Err() == nil {
				handler(StreamEvent{Message: msg, Seq: m.Sequence})
			}
			return nil
		},
		Link: func(up bool, err error) {
			if up {
				s.emitConnection(ConnectionEvent{State: "reconnected"})
				return
			}
			s.emitConnection(ConnectionEvent{State: "disconnected", Err: err})
		},
	}
	go func() {
		defer s.wg.Done()
		defer stop()
		defer cancel()
		if err := follower.Run(loopCtx, *start); err != nil && loopCtx.Err() == nil {
			s.emitConnection(ConnectionEvent{State: "closed", Err: err})
		}
	}()
	return nil
}

// Status lists every participant with its presence from who. The chat screen shows only the names
// it was given, so this is the full roster.
func (s *remoteService) Status(ctx context.Context) ([]AgentPresence, error) {
	who, err := s.gateway.Who(ctx)
	if err != nil {
		return nil, err
	}
	presence := make([]AgentPresence, 0, len(who.Participants))
	for _, p := range who.Participants {
		presence = append(presence, AgentPresence{Name: p.Name, State: presenceState(p)})
	}
	return presence, nil
}

// presenceState maps a participant's online and deliverable flags to the states the chat screen
// understands. Remote mode has no activity events, so there is no "thinking" state.
func presenceState(p garden.Presence) string {
	switch {
	case !p.Online:
		return "offline"
	case p.Deliverable:
		return "active"
	default:
		return "idle"
	}
}

// SubscribeActivity is a no-op: the gateway publishes no activity events.
func (s *remoteService) SubscribeActivity(context.Context, func(ActivityEvent)) error {
	return nil
}

func (s *remoteService) SubscribeConnection(ctx context.Context, handler func(ConnectionEvent)) error {
	s.mu.Lock()
	id := s.nextHandlerID
	s.nextHandlerID++
	s.connHandlers[id] = handler
	s.mu.Unlock()

	go func() {
		<-ctx.Done()
		s.mu.Lock()
		delete(s.connHandlers, id)
		s.mu.Unlock()
	}()
	return nil
}

// Redaction reports nothing: the gateway already renders redacted content in what it returns.
func (s *remoteService) Redaction(string) (RedactionStatus, error) {
	return RedactionStatus{}, nil
}

// Send posts content to the room as the session's identity. Remote mode cannot speak as another
// participant, so an identity other than the session's is refused.
func (s *remoteService) Send(ctx context.Context, identity string, content string, replyTo *string) error {
	if identity != "" && identity != s.identity {
		return fmt.Errorf("remote chat sends only as %s", s.identity)
	}
	args := garden.SendArgs{Content: content}
	if replyTo != nil {
		args.ReplyTo = *replyTo
	}
	_, err := s.gateway.Send(ctx, args)
	return err
}

func (s *remoteService) Promote(string, model.Message, bool) (bool, error) {
	return false, remoteUnavailable("promote")
}

func (s *remoteService) Pause(context.Context, string) error {
	return remoteUnavailable("pause")
}

func (s *remoteService) Resume(context.Context, string) error {
	return remoteUnavailable("resume")
}

// Close stops polling, waits for the follow loop to exit, and closes the gateway. It is safe to
// call more than once.
func (s *remoteService) Close() {
	s.closeOnce.Do(func() {
		s.mu.Lock()
		s.closed = true
		s.mu.Unlock()
		s.cancel()
		s.wg.Wait()
		_ = s.gateway.Close()
	})
}

func (s *remoteService) emitConnection(event ConnectionEvent) {
	s.mu.Lock()
	handlers := make([]func(ConnectionEvent), 0, len(s.connHandlers))
	for _, handler := range s.connHandlers {
		handlers = append(handlers, handler)
	}
	s.mu.Unlock()
	for _, handler := range handlers {
		handler(event)
	}
}

func remoteUnavailable(action string) error {
	return fmt.Errorf("%s is not available in remote mode", action)
}

func streamEvents(messages []garden.Message) ([]StreamEvent, error) {
	events := make([]StreamEvent, 0, len(messages))
	for _, m := range messages {
		msg, err := remotecli.Convert(m)
		if err != nil {
			return nil, err
		}
		events = append(events, StreamEvent{Message: msg, Seq: m.Sequence})
	}
	return events, nil
}
