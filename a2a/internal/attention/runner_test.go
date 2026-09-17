package attention

import (
	"context"
	"encoding/json"
	"errors"
	"sync"
	"testing"
	"time"
)

type testInbox struct {
	mu          sync.Mutex
	pending     *Pending
	acks, polls int
	cancel      context.CancelFunc
}

func (i *testInbox) Poll(ctx context.Context, consumer string) (*Pending, error) {
	i.mu.Lock()
	defer i.mu.Unlock()
	i.polls++
	if i.pending == nil {
		return nil, nil
	}
	copy := *i.pending
	return &copy, nil
}
func (i *testInbox) Ack(ctx context.Context, consumer, receipt string) error {
	i.mu.Lock()
	defer i.mu.Unlock()
	if receipt != "receipt-one" {
		return errors.New("wrong receipt")
	}
	i.acks++
	i.pending = nil
	i.cancel()
	return nil
}

type deliveryFunc func(context.Context, Event) error

func (f deliveryFunc) Deliver(c context.Context, e Event) error { return f(c, e) }

type lifecycleTestHost struct {
	deliveryFunc
	done <-chan struct{}
}

func (h lifecycleTestHost) Done() <-chan struct{} { return h.done }

type blockingAckInbox struct {
	*testInbox
	started chan struct{}
}

func (i *blockingAckInbox) Ack(ctx context.Context, _, _ string) error {
	close(i.started)
	<-ctx.Done()
	return ctx.Err()
}

func TestRunnerOnlyAcknowledgesAcceptedHostDelivery(t *testing.T) {
	for _, outcome := range []error{nil, ErrUncertain, ErrRejected} {
		ctx, cancel := context.WithCancel(context.Background())
		inbox := &testInbox{pending: &Pending{Event: Event{ID: "m", Payload: json.RawMessage(`{}`)}, Receipt: "receipt-one", Generation: "g"}, cancel: cancel}
		runner := Runner{Inbox: inbox, Host: deliveryFunc(func(context.Context, Event) error { return outcome }), ConsumerID: "consumer"}
		err := runner.Run(ctx)
		cancel()
		if outcome == nil && err != nil {
			t.Fatal(err)
		}
		if outcome != nil && !errors.Is(err, outcome) {
			t.Fatalf("got %v want %v", err, outcome)
		}
		want := 0
		if outcome == nil {
			want = 1
		}
		if inbox.acks != want {
			t.Fatalf("ack count %d want %d", inbox.acks, want)
		}
	}
}

func TestRunnerRenewsLeaseWhileWaitingForHostAcknowledgement(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	inbox := &testInbox{pending: &Pending{Event: Event{ID: "m", Payload: json.RawMessage(`{}`)}, Receipt: "receipt-one", Generation: "g"}, cancel: cancel}
	runner := Runner{Inbox: inbox, ConsumerID: "consumer", renewEvery: 5 * time.Millisecond, Host: deliveryFunc(func(ctx context.Context, e Event) error {
		for {
			inbox.mu.Lock()
			polls := inbox.polls
			inbox.mu.Unlock()
			if polls >= 3 {
				return nil
			}
			select {
			case <-ctx.Done():
				return ctx.Err()
			case <-time.After(time.Millisecond):
			}
		}
	})}
	if err := runner.Run(ctx); err != nil {
		t.Fatal(err)
	}
	if inbox.acks != 1 || inbox.polls < 3 {
		t.Fatalf("acks=%d polls=%d", inbox.acks, inbox.polls)
	}
}

func TestRunnerRetriesBusyHostWithoutAcknowledgingEarly(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	inbox := &testInbox{pending: &Pending{Event: Event{ID: "m", Payload: json.RawMessage(`{}`)}, Receipt: "receipt-one", Generation: "g"}, cancel: cancel}
	calls := 0
	runner := Runner{Inbox: inbox, ConsumerID: "consumer", retryAfter: time.Millisecond, Host: deliveryFunc(func(context.Context, Event) error {
		calls++
		if calls == 1 {
			return ErrBusy
		}
		if inbox.acks != 0 {
			t.Error("early ack")
		}
		return nil
	})}
	if err := runner.Run(ctx); err != nil {
		t.Fatal(err)
	}
	if calls != 2 || inbox.acks != 1 {
		t.Fatalf("calls=%d acks=%d", calls, inbox.acks)
	}
}

func TestRunnerStopsCleanlyWhenIdleHostSessionCloses(t *testing.T) {
	closed := make(chan struct{})
	close(closed)
	runner := Runner{
		Inbox:      &testInbox{},
		ConsumerID: "consumer",
		Host:       lifecycleTestHost{deliveryFunc: deliveryFunc(func(context.Context, Event) error { return nil }), done: closed},
	}
	if err := runner.Run(context.Background()); err != nil {
		t.Fatalf("idle lifecycle close: %v", err)
	}
}

func TestRunnerTreatsHostSessionCloseDuringDeliveryAsUncertain(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	closed := make(chan struct{})
	started := make(chan struct{})
	inbox := &testInbox{pending: &Pending{Event: Event{ID: "m", Payload: json.RawMessage(`{}`)}, Receipt: "receipt-one", Generation: "g"}, cancel: cancel}
	runner := Runner{
		Inbox:      inbox,
		ConsumerID: "consumer",
		Host: lifecycleTestHost{deliveryFunc: deliveryFunc(func(ctx context.Context, _ Event) error {
			close(started)
			<-ctx.Done()
			return ctx.Err()
		}), done: closed},
	}
	result := make(chan error, 1)
	go func() { result <- runner.Run(ctx) }()
	<-started
	close(closed)
	if err := <-result; !errors.Is(err, ErrUncertain) {
		t.Fatalf("delivery lifecycle close: %v", err)
	}
	if inbox.acks != 0 {
		t.Fatalf("acknowledged uncertain delivery: %d", inbox.acks)
	}
}

func TestRunnerTreatsHostSessionCloseDuringAckAsUncertain(t *testing.T) {
	closed := make(chan struct{})
	ackStarted := make(chan struct{})
	inbox := &blockingAckInbox{
		testInbox: &testInbox{pending: &Pending{Event: Event{ID: "m", Payload: json.RawMessage(`{}`)}, Receipt: "receipt-one", Generation: "g"}},
		started:   ackStarted,
	}
	runner := Runner{
		Inbox:      inbox,
		ConsumerID: "consumer",
		Host:       lifecycleTestHost{deliveryFunc: deliveryFunc(func(context.Context, Event) error { return nil }), done: closed},
	}
	result := make(chan error, 1)
	go func() { result <- runner.Run(context.Background()) }()
	<-ackStarted
	close(closed)
	if err := <-result; !errors.Is(err, ErrUncertain) {
		t.Fatalf("ack lifecycle close: %v", err)
	}
	if inbox.acks != 0 {
		t.Fatalf("recorded an acknowledgement after lifecycle close: %d", inbox.acks)
	}
}
