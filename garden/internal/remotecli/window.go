package remotecli

import (
	"context"
	"errors"
	"fmt"
	"time"

	"github.com/veridian69/cairn/garden/internal/garden"
	"github.com/veridian69/cairn/garden/internal/model"
)

// MaxTail is the most messages one tail read may ask for, the gateway's own limit.
const MaxTail = 100

// ErrNoTail reports a gateway that cannot return the newest messages in one read. An older gateway
// ignores the tail argument and returns the oldest page, so its answer is refused rather than shown
// as the newest messages.
var ErrNoTail = errors.New("the Garden gateway cannot return the newest messages; it predates tail reads, so upgrade it before using remote history, watch or chat")

// Reader is the one gateway call the remote reads need. *Session and *garden.Client satisfy it.
type Reader interface {
	Read(context.Context, garden.ReadArgs) (garden.ReadResult, error)
}

// Window is a run of messages and the cursor that follows its last message.
type Window struct {
	Messages   []garden.Message
	Generation string
	NextSeq    uint64
}

// Tail returns the newest n messages, oldest first.
//
// A gateway that predates tail reads either refuses the argument as invalid_argument or ignores it
// and answers with the oldest page, which still reports More. Both come back as ErrNoTail. A
// complete answer with no More is the newest window; if the gateway returned more than n messages
// in such an answer, only the newest n are kept.
func Tail(ctx context.Context, r Reader, n int) (Window, error) {
	if n < 1 || n > MaxTail {
		return Window{}, fmt.Errorf("remote reads show 1 to %d messages", MaxTail)
	}
	res, err := r.Read(ctx, garden.ReadArgs{Tail: n})
	if err != nil {
		if errorCode(err) == "invalid_argument" {
			return Window{}, ErrNoTail
		}
		return Window{}, err
	}
	if res.More {
		return Window{}, ErrNoTail
	}
	messages := res.Messages
	if len(messages) > n {
		messages = messages[len(messages)-n:]
	}
	return Window{Messages: messages, Generation: res.Generation, NextSeq: res.NextSeq}, nil
}

// Convert turns a gateway message into the chat model. Gateway timestamps are RFC 3339.
func Convert(m garden.Message) (model.Message, error) {
	created, err := time.Parse(time.RFC3339Nano, m.CreatedAt)
	if err != nil {
		return model.Message{}, fmt.Errorf("Garden message %s has an invalid timestamp", model.ShortID(m.ID))
	}
	out := model.Message{
		ID:         m.ID,
		AuthorID:   m.AuthorID,
		AuthorName: m.AuthorName,
		Content:    m.Content,
		CreatedAt:  created,
	}
	if m.ReplyTo != "" {
		replyTo := m.ReplyTo
		out.ReplyTo = &replyTo
	}
	return out, nil
}

// errorCode returns the stable Garden error code of err, or "" when err is not one.
func errorCode(err error) string {
	var e *garden.Error
	if errors.As(err, &e) {
		return e.Code
	}
	return ""
}
