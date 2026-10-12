package attention

import (
	"context"
	"encoding/json"
	"errors"
	"io"
	"testing"
	"time"
)

func TestRunnerRetriesPreSubmissionOutage(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	inbox := &testInbox{pending: &Pending{Event: Event{ID: "m", Payload: json.RawMessage(`{}`)}, Receipt: "receipt-one", Generation: "g"}, cancel: cancel}
	calls := 0
	runner := Runner{Inbox: inbox, ConsumerID: "c", retryAfter: time.Millisecond, Host: deliveryFunc(func(context.Context, Event) error {
		calls++
		if calls == 1 {
			return ErrUnavailable
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

type interruptedAck struct{ *testInbox }

func (i interruptedAck) Ack(context.Context, string, string) error { i.cancel(); return ErrUnavailable }

func TestRunnerCancellationPreservesAmbiguousDelivery(t *testing.T) {
	for _, phase := range []string{"host", "ack"} {
		t.Run(phase, func(t *testing.T) {
			ctx, cancel := context.WithCancel(context.Background())
			defer cancel()
			inbox := &testInbox{pending: &Pending{Event: Event{ID: "m", Payload: json.RawMessage(`{}`)}, Receipt: "receipt-one", Generation: "g"}, cancel: cancel}
			var source Inbox = inbox
			if phase == "ack" {
				source = interruptedAck{inbox}
			}
			runner := Runner{Inbox: source, ConsumerID: "c", Host: deliveryFunc(func(context.Context, Event) error {
				if phase == "host" {
					cancel()
					return ErrUncertain
				}
				return nil
			})}
			if err := runner.Run(ctx); !errors.Is(err, ErrUncertain) {
				t.Fatalf("ambiguous cancellation: %v", err)
			}
			if inbox.acks != 0 {
				t.Fatal("acknowledged interrupted delivery")
			}
		})
	}
}

func TestStdioWorkerFailureClosesBlockedInput(t *testing.T) {
	in, writer := io.Pipe()
	defer writer.Close()
	server := NewStdio(io.Discard, fakeTools{}, true)
	done := make(chan error, 1)
	go func() {
		done <- server.Run(context.Background(), in, func(context.Context) error { return ErrRejected })
	}()
	_, _ = io.WriteString(writer, "{\"jsonrpc\":\"2.0\",\"id\":1,\"method\":\"initialize\",\"params\":{\"protocolVersion\":\"2025-11-25\"}}\n{\"jsonrpc\":\"2.0\",\"method\":\"notifications/initialized\"}\n")
	select {
	case err := <-done:
		if !errors.Is(err, ErrRejected) {
			t.Fatal(err)
		}
	case <-time.After(time.Second):
		t.Fatal("worker failure did not stop MCP")
	}
	closed := make(chan error, 1)
	go func() { _, err := io.WriteString(writer, "still open\n"); closed <- err }()
	select {
	case err := <-closed:
		if err == nil {
			t.Fatal("reader still open")
		}
	case <-time.After(time.Second):
		t.Fatal("scanner leaked")
	}
}

func TestPermanentPreSubmissionHTTPFailuresStop(t *testing.T) {
	for _, code := range []int{301, 307, 400, 401, 403, 404, 405, 409, 422} {
		if !errors.Is(rejectedStatus(code, false), ErrRejected) {
			t.Errorf("HTTP %d must stop", code)
		}
	}
	for _, code := range []int{408, 425, 429, 500, 503} {
		if !errors.Is(rejectedStatus(code, false), ErrUnavailable) {
			t.Errorf("HTTP %d must retry", code)
		}
	}
}

func TestStdioShutdownPreservesWorkerUncertainty(t *testing.T) {
	for _, cause := range []string{"cancel", "eof"} {
		t.Run(cause, func(t *testing.T) {
			ctx, cancel := context.WithCancel(context.Background())
			defer cancel()
			in, writer := io.Pipe()
			defer writer.Close()
			started := make(chan struct{})
			done := make(chan error, 1)
			server := NewStdio(io.Discard, fakeTools{}, true)
			go func() {
				done <- server.Run(ctx, in, func(ctx context.Context) error { close(started); <-ctx.Done(); return ErrUncertain })
			}()
			_, _ = io.WriteString(writer, "{\"jsonrpc\":\"2.0\",\"id\":1,\"method\":\"initialize\",\"params\":{\"protocolVersion\":\"2025-11-25\"}}\n{\"jsonrpc\":\"2.0\",\"method\":\"notifications/initialized\"}\n")
			select {
			case <-started:
			case <-time.After(time.Second):
				t.Fatal("worker did not start")
			}
			if cause == "cancel" {
				cancel()
			} else {
				_ = writer.Close()
			}
			select {
			case err := <-done:
				if !errors.Is(err, ErrUncertain) {
					t.Fatalf("lost delivery uncertainty: %v", err)
				}
			case <-time.After(2 * time.Second):
				t.Fatal("shutdown stuck")
			}
		})
	}
}
