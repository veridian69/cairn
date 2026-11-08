package remotecli

import (
	"context"
	"errors"
	"time"

	"github.com/veridian69/cairn/garden/internal/garden"
)

// PollInterval is how long a follower waits between reads when the room is quiet.
const PollInterval = 2 * time.Second

const (
	// pageLimit is the most messages one follow read asks for, the gateway's own limit.
	pageLimit = 100
	// resyncTail is how many messages a resynchronisation shows after a reset or gap.
	resyncTail = 50
)

// Follower polls read_messages from a cursor and reports new messages in sequence order. It
// returns nil when its context ends, and an error when the gateway refuses the session for good.
// Each callback runs on the Run goroutine.
type Follower struct {
	Reader Reader
	// Interval is the wait between quiet polls; zero means PollInterval.
	Interval time.Duration
	// Message receives each new message. An error stops the follower and is returned by Run.
	Message func(garden.Message) error
	// Notice reports a reset or retention gap before the follower resynchronises from the tail.
	Notice func(text string)
	// Link reports a change in reachability: down with the failure, or up again. It is not called
	// for every poll.
	Link func(up bool, err error)
}

// consumerError marks an error from Message, so Run returns it rather than retrying.
type consumerError struct{ err error }

func (e consumerError) Error() string { return e.err.Error() }
func (e consumerError) Unwrap() error { return e.err }

// Run follows the room from start, which is the window the caller has already shown.
func (f Follower) Run(ctx context.Context, start Window) error {
	interval := f.Interval
	if interval <= 0 {
		interval = PollInterval
	}
	cur := Window{Generation: start.Generation, NextSeq: start.NextSeq}
	up := true
	for {
		err := f.poll(ctx, &cur)
		if ctx.Err() != nil {
			return nil
		}
		var consumer consumerError
		if errors.As(err, &consumer) {
			return consumer.err
		}
		switch {
		case err != nil && fatalFollowError(err):
			return err
		case err != nil:
			if up {
				up = false
				f.link(false, err)
			}
		case !up:
			up = true
			f.link(true, nil)
		}
		timer := time.NewTimer(interval)
		select {
		case <-ctx.Done():
			timer.Stop()
			return nil
		case <-timer.C:
		}
	}
}

// poll reads every page that is ready, advancing cur as it goes.
func (f Follower) poll(ctx context.Context, cur *Window) error {
	for {
		res, err := f.Reader.Read(ctx, garden.ReadArgs{AfterSeq: cur.NextSeq, Generation: cur.Generation, Limit: pageLimit})
		if err != nil {
			code := errorCode(err)
			if code != "stream_reset" && code != "retention_gap" {
				return err
			}
			return f.resync(ctx, cur, code)
		}
		for _, m := range res.Messages {
			if err := f.deliver(m); err != nil {
				return consumerError{err}
			}
		}
		cur.Generation, cur.NextSeq = res.Generation, res.NextSeq
		if !res.More {
			return nil
		}
	}
}

// resync replaces the cursor with the newest window after the gateway reports that the stream was
// reset or that the cursor precedes what it still retains.
func (f Follower) resync(ctx context.Context, cur *Window, code string) error {
	if f.Notice != nil {
		if code == "retention_gap" {
			f.Notice("older messages are no longer retained; showing the newest messages again")
		} else {
			f.Notice("the Garden stream was reset; showing the newest messages again")
		}
	}
	w, err := Tail(ctx, f.Reader, resyncTail)
	if err != nil {
		return err
	}
	for _, m := range w.Messages {
		if err := f.deliver(m); err != nil {
			return consumerError{err}
		}
	}
	cur.Generation, cur.NextSeq = w.Generation, w.NextSeq
	return nil
}

func (f Follower) deliver(m garden.Message) error {
	if f.Message == nil {
		return nil
	}
	return f.Message(m)
}

func (f Follower) link(up bool, err error) {
	if f.Link != nil {
		f.Link(up, err)
	}
}

// fatalFollowError reports failures that polling cannot recover from: the gateway refused this
// identity, rejected the request shape, or does not support tail reads.
func fatalFollowError(err error) bool {
	if errors.Is(err, ErrNoTail) {
		return true
	}
	code := errorCode(err)
	return code == "forbidden" || code == "invalid_argument"
}
