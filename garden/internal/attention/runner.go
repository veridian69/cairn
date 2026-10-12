package attention

import (
	"context"
	"errors"
	"time"
)

// Pending carries a principal-bound Garden receipt, never a host authority.
type Pending struct {
	Event      Event
	Receipt    string
	Generation string
}

type Inbox interface {
	Poll(context.Context, string) (*Pending, error)
	Ack(context.Context, string, string) error
}

type lifecycleHost interface {
	Done() <-chan struct{}
}

// Runner processes one delivery at a time and renews its consumer lease while
// the host is accepting it. It never retries an ambiguous host submission.
type Runner struct {
	Inbox      Inbox
	Host       Host
	ConsumerID string
	retryAfter time.Duration
	renewEvery time.Duration
}

func (r Runner) Run(ctx context.Context) error {
	if r.Inbox == nil || r.Host == nil || r.ConsumerID == "" {
		return errors.New("incomplete attention runner")
	}
	if r.retryAfter <= 0 {
		r.retryAfter = 2 * time.Second
	}
	if r.renewEvery <= 0 {
		r.renewEvery = 30 * time.Second
	}
	if host, ok := r.Host.(lifecycleHost); ok {
		done := host.Done()
		if done != nil {
			var cancel context.CancelFunc
			ctx, cancel = context.WithCancel(ctx)
			defer cancel()
			select {
			case <-done:
				cancel()
			default:
				go func() {
					select {
					case <-done:
						cancel()
					case <-ctx.Done():
					}
				}()
			}
		}
	}
	for {
		if ctx.Err() != nil {
			return nil
		}
		pending, err := r.Inbox.Poll(ctx, r.ConsumerID)
		if err != nil {
			if ctx.Err() != nil {
				return nil
			}
			if errors.Is(err, ErrUnavailable) || errors.Is(err, ErrBusy) {
				if !pause(ctx, r.retryAfter) {
					return nil
				}
				continue
			}
			return err
		}
		if pending == nil {
			if !pause(ctx, 100*time.Millisecond) {
				return nil
			}
			continue
		}
		err = r.deliver(ctx, *pending)
		if ctx.Err() != nil {
			return ErrUncertain
		}
		if errors.Is(err, ErrBusy) || errors.Is(err, ErrUnavailable) {
			if !pause(ctx, r.retryAfter) {
				return nil
			}
			continue
		}
		if err != nil {
			return err
		}
		// After acceptance only retry the idempotent ACK, never the host call.
		for {
			err = r.Inbox.Ack(ctx, r.ConsumerID, pending.Receipt)
			if err == nil {
				break
			}
			if ctx.Err() != nil {
				return ErrUncertain
			}
			if !errors.Is(err, ErrUnavailable) {
				return err
			}
			if !pause(ctx, r.retryAfter) {
				return ErrUncertain
			}
		}
	}
}

func (r Runner) deliver(ctx context.Context, pending Pending) error {
	ctx, cancel := context.WithCancel(ctx)
	defer cancel()
	done := make(chan error, 1)
	go func() { done <- r.Host.Deliver(ctx, pending.Event) }()
	tick := time.NewTicker(r.renewEvery)
	defer tick.Stop()
	for {
		select {
		case err := <-done:
			return err
		case <-ctx.Done():
			return ErrUncertain
		case <-tick.C:
			current, err := r.Inbox.Poll(ctx, r.ConsumerID)
			if err != nil || current == nil || current.Receipt != pending.Receipt || current.Generation != pending.Generation || current.Event.ID != pending.Event.ID {
				return ErrUncertain
			}
		}
	}
}

func pause(ctx context.Context, d time.Duration) bool {
	timer := time.NewTimer(d)
	defer timer.Stop()
	select {
	case <-ctx.Done():
		return false
	case <-timer.C:
		return true
	}
}
