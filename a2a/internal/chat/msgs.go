package chat

import (
	"context"
	"fmt"
	"time"

	tea "github.com/charmbracelet/bubbletea"
	"github.com/veridian69/cairn/a2a/internal/model"
)

type streamMsg StreamEvent
type activityMsg ActivityEvent
type connectionMsg ConnectionEvent

type statusMsg struct {
	generation uint64
	statuses   []AgentPresence
	err        error
}

type pollMsg struct {
	generation uint64
}

type sendResultMsg struct {
	actionID uint64
	raw      string
	revision uint64
	err      error
}

type controlResultMsg struct {
	actionID   uint64
	action     string
	agent      string
	submission string
	revision   uint64
	err        error
}

type promoteResultMsg struct {
	actionID uint64
	existed  bool
	pin      bool
	err      error
}

type redactionResultMsg struct {
	messageID string
	status    RedactionStatus
	err       error
}

type msgSender interface {
	Send(tea.Msg)
}

const serviceCallTimeout = 5 * time.Second

func runSubscriptions(ctx context.Context, svc Service, sender msgSender) error {
	if err := svc.Subscribe(ctx, func(event StreamEvent) {
		if ctx.Err() == nil {
			sender.Send(streamMsg(event))
		}
	}); err != nil {
		return err
	}
	if err := svc.SubscribeActivity(ctx, func(event ActivityEvent) {
		if ctx.Err() == nil {
			sender.Send(activityMsg(event))
		}
	}); err != nil {
		return err
	}
	return svc.SubscribeConnection(ctx, func(event ConnectionEvent) {
		if ctx.Err() == nil {
			sender.Send(connectionMsg(event))
		}
	})
}

func statusCmd(parent context.Context, svc Service, generation uint64) tea.Cmd {
	return func() tea.Msg {
		ctx, cancel := context.WithTimeout(parent, serviceCallTimeout)
		defer cancel()
		statuses, err := svc.Status(ctx)
		return statusMsg{generation: generation, statuses: statuses, err: err}
	}
}

func pollTick(interval time.Duration, generation uint64) tea.Cmd {
	return tea.Tick(interval, func(time.Time) tea.Msg {
		return pollMsg{generation: generation}
	})
}

func sendCmd(
	parent context.Context,
	svc Service,
	actionID uint64,
	raw string,
	revision uint64,
	identity string,
	content string,
	replyTo *string,
) tea.Cmd {
	return func() tea.Msg {
		ctx, cancel := context.WithTimeout(parent, serviceCallTimeout)
		defer cancel()
		return sendResultMsg{
			actionID: actionID,
			raw:      raw,
			revision: revision,
			err:      svc.Send(ctx, identity, content, replyTo),
		}
	}
}

func controlCmd(
	parent context.Context,
	svc Service,
	actionID uint64,
	action string,
	agent string,
	submission string,
	revision uint64,
) tea.Cmd {
	return func() tea.Msg {
		ctx, cancel := context.WithTimeout(parent, serviceCallTimeout)
		defer cancel()
		var err error
		switch action {
		case "pause":
			err = svc.Pause(ctx, agent)
		case "resume":
			err = svc.Resume(ctx, agent)
		default:
			err = fmt.Errorf("unsupported control action %q", action)
		}
		return controlResultMsg{
			actionID: actionID, action: action, agent: agent,
			submission: submission, revision: revision, err: err,
		}
	}
}

func promoteCmd(
	svc Service,
	actionID uint64,
	human string,
	message model.Message,
	pin bool,
) tea.Cmd {
	return func() tea.Msg {
		existed, err := svc.Promote(human, message, pin)
		return promoteResultMsg{
			actionID: actionID, existed: existed, pin: pin, err: err,
		}
	}
}

func redactionCmd(svc Service, messageID string) tea.Cmd {
	return func() tea.Msg {
		status, err := svc.Redaction(messageID)
		return redactionResultMsg{messageID: messageID, status: status, err: err}
	}
}
