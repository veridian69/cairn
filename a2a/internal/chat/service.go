package chat

import (
	"context"
	"encoding/json"
	"fmt"
	"os/user"
	"path/filepath"
	"strings"
	"sync"
	"time"

	"github.com/nats-io/nats.go"
	"github.com/veridian69/cairn/a2a/internal/daemon"
	"github.com/veridian69/cairn/a2a/internal/memory"
	"github.com/veridian69/cairn/a2a/internal/model"
	"github.com/veridian69/cairn/a2a/internal/state"
	"github.com/veridian69/cairn/a2a/internal/transport"
)

type StreamEvent struct {
	Message model.Message
	Seq     uint64
}

type AgentPresence struct {
	Name           string
	Provider       string
	Model          string
	State          string
	QueueDepth     int
	Responsiveness float64
	LastActivityAt time.Time
}

type ActivityEvent struct {
	Agent string
	State string
	At    time.Time
}

type ConnectionEvent struct {
	State string
	Err   error
}

type RedactionStatus struct {
	Reason   string
	Redacted bool
}

type Service interface {
	Tail(ctx context.Context, limit int) ([]StreamEvent, error)
	Subscribe(ctx context.Context, handler func(StreamEvent)) error
	Status(ctx context.Context) ([]AgentPresence, error)
	SubscribeActivity(ctx context.Context, handler func(ActivityEvent)) error
	SubscribeConnection(ctx context.Context, handler func(ConnectionEvent)) error
	Redaction(messageID string) (RedactionStatus, error)
	Send(ctx context.Context, identity string, content string, replyTo *string) error
	Promote(humanIdentity string, msg model.Message, pin bool) (bool, error)
	Pause(ctx context.Context, agent string) error
	Resume(ctx context.Context, agent string) error
	Close()
}

type Transport interface {
	TailWithSeq(ctx context.Context, limit int) ([]transport.ReplayEntry, error)
	Subscribe(ctx context.Context, handler func(model.Message, uint64) error) error
	Publish(ctx context.Context, msg model.Message) error
	Close()
}

type service struct {
	stream        Transport
	state         *state.DB
	nc            *nats.Conn
	mu            sync.Mutex
	nextHandlerID int
	connHandlers  map[int]func(ConnectionEvent)
	dataDir       string
	maxItemBytes  int
}

func NewService(daemonURL, stateDBPath string, maxItemBytes int) (Service, error) {
	stream, err := transport.NewStream(strings.TrimSpace(daemonURL))
	if err != nil {
		return nil, fmt.Errorf("stream: %w", err)
	}

	db, err := state.Open(stateDBPath)
	if err != nil {
		stream.Close()
		return nil, fmt.Errorf("state: %w", err)
	}

	nc, err := nats.Connect(strings.TrimSpace(daemonURL))
	if err != nil {
		db.Close()
		stream.Close()
		return nil, fmt.Errorf("nats: %w", err)
	}

	svc := &service{
		stream: stream, state: db, nc: nc,
		connHandlers: make(map[int]func(ConnectionEvent)),
		dataDir:      filepath.Dir(stateDBPath), maxItemBytes: maxItemBytes,
	}
	svc.installConnHandlers()
	return svc, nil
}

func NewServiceWithDeps(
	stream Transport,
	db *state.DB,
	nc *nats.Conn,
	dataDir string,
	maxItemBytes int,
) Service {
	svc := &service{
		stream: stream, state: db, nc: nc,
		connHandlers: make(map[int]func(ConnectionEvent)),
		dataDir:      dataDir, maxItemBytes: maxItemBytes,
	}
	svc.installConnHandlers()
	return svc
}

func (s *service) Tail(ctx context.Context, limit int) ([]StreamEvent, error) {
	entries, err := s.stream.TailWithSeq(ctx, limit)
	if err != nil {
		return nil, err
	}
	events := make([]StreamEvent, 0, len(entries))
	for _, entry := range entries {
		events = append(events, StreamEvent{
			Message: entry.Message,
			Seq:     entry.Seq,
		})
	}
	return events, nil
}

func (s *service) Subscribe(ctx context.Context, handler func(StreamEvent)) error {
	return s.stream.Subscribe(ctx, func(msg model.Message, seq uint64) error {
		handler(StreamEvent{Message: msg, Seq: seq})
		return nil
	})
}

func (s *service) Status(ctx context.Context) ([]AgentPresence, error) {
	msg, err := s.nc.RequestWithContext(ctx, "a2a.status", nil)
	if err != nil {
		return nil, err
	}
	var statuses []daemon.AgentStatus
	if err := json.Unmarshal(msg.Data, &statuses); err != nil {
		return nil, fmt.Errorf("decode status: %w", err)
	}
	presence := make([]AgentPresence, 0, len(statuses))
	for _, st := range statuses {
		stateName := st.State
		if stateName == "" {
			switch {
			case st.Active:
				stateName = "active"
			default:
				stateName = "paused"
			}
		}
		presence = append(presence, AgentPresence{
			Name:           st.Name,
			Provider:       st.Provider,
			Model:          st.Model,
			State:          stateName,
			QueueDepth:     st.QueueDepth,
			Responsiveness: st.Responsiveness,
			LastActivityAt: st.LastActivityAt,
		})
	}
	return presence, nil
}

func (s *service) SubscribeActivity(ctx context.Context, handler func(ActivityEvent)) error {
	sub, err := s.nc.Subscribe("a2a.activity", func(msg *nats.Msg) {
		var event daemon.ActivityEvent
		if err := json.Unmarshal(msg.Data, &event); err != nil {
			return
		}
		handler(ActivityEvent{
			Agent: event.Agent,
			State: event.State,
			At:    event.At,
		})
	})
	if err != nil {
		return err
	}
	go func() {
		<-ctx.Done()
		sub.Unsubscribe()
	}()
	return nil
}

func (s *service) SubscribeConnection(ctx context.Context, handler func(ConnectionEvent)) error {
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

func (s *service) Redaction(messageID string) (RedactionStatus, error) {
	reason, redacted, err := s.state.RedactionReason(messageID)
	if err != nil {
		return RedactionStatus{}, err
	}
	return RedactionStatus{Reason: reason, Redacted: redacted}, nil
}

func (s *service) Send(ctx context.Context, identity string, content string, replyTo *string) error {
	author, err := s.resolveIdentity(identity)
	if err != nil {
		return err
	}
	msg := model.NewMessage(author, content, replyTo)

	if identity != "" && author.Kind == model.KindAgent {
		humanParticipant, humanErr := s.resolveHumanIdentity("")
		if humanErr != nil {
			return humanErr
		}
		msg.Metadata["seeded_by"] = humanParticipant.ID
		msg.Metadata["authorship_mode"] = "seeded"
	}

	return s.stream.Publish(ctx, msg)
}

func (s *service) Promote(humanIdentity string, message model.Message, pin bool) (bool, error) {
	nominator, err := s.resolveHumanIdentity(humanIdentity)
	if err != nil {
		return false, err
	}
	store, err := memory.Open(s.dataDir, memory.Options{MaxItemBytes: s.maxItemBytes})
	if err != nil {
		return false, err
	}
	defer store.Close()
	result, err := store.Insert(memory.Item{
		Content: message.Content, AuthorID: message.AuthorID,
		AuthorName: message.AuthorName, SourceMessageID: message.ID,
		SourceCreatedAt: message.CreatedAt, ReplyTo: message.ReplyTo,
		NominatedBy: nominator.ID, Pinned: pin,
	})
	return result.Existed, err
}

func (s *service) Pause(ctx context.Context, agent string) error {
	return s.publishControl(ctx, "pause:"+agent)
}

func (s *service) Resume(ctx context.Context, agent string) error {
	return s.publishControl(ctx, "resume:"+agent)
}

func (s *service) publishControl(ctx context.Context, payload string) error {
	msg := nats.NewMsg("a2a.control")
	msg.Data = []byte(payload)
	if err := s.nc.PublishMsg(msg); err != nil {
		return err
	}
	if _, ok := ctx.Deadline(); ok {
		if err := s.nc.FlushWithContext(ctx); err != nil {
			return err
		}
		return nil
	}
	return s.nc.Flush()
}

func (s *service) resolveIdentity(identity string) (model.Participant, error) {
	if identity == "" {
		return s.resolveHumanIdentity("")
	}
	participant, err := s.state.GetParticipantByName(identity)
	if err == nil {
		return participant, nil
	}
	return s.resolveHumanIdentity(identity)
}

func (s *service) resolveHumanIdentity(identity string) (model.Participant, error) {
	name := identity
	if name == "" {
		current, err := user.Current()
		if err == nil && current != nil && current.Username != "" {
			name = current.Username
		}
	}
	if name == "" {
		name = "human"
	}
	return s.state.RegisterParticipant(model.Participant{Name: name, Kind: model.KindHuman})
}

func (s *service) Close() {
	if s.nc != nil {
		s.nc.Close()
	}
	if s.state != nil {
		s.state.Close()
	}
	if s.stream != nil {
		s.stream.Close()
	}
}

func (s *service) installConnHandlers() {
	if s.nc == nil {
		return
	}
	s.nc.SetDisconnectErrHandler(func(_ *nats.Conn, err error) {
		s.emitConnection(ConnectionEvent{State: "disconnected", Err: err})
	})
	s.nc.SetReconnectHandler(func(_ *nats.Conn) {
		s.emitConnection(ConnectionEvent{State: "reconnected"})
	})
	s.nc.SetClosedHandler(func(_ *nats.Conn) {
		s.emitConnection(ConnectionEvent{State: "closed"})
	})
}

func (s *service) emitConnection(event ConnectionEvent) {
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
