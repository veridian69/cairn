package attention

import (
	"bufio"
	"context"
	"encoding/json"
	"io"
	"testing"
	"time"
)

type fakeTools struct{}

func (fakeTools) List(context.Context) ([]Tool, error) {
	return []Tool{{Name: "send_message", Description: "reply", InputSchema: json.RawMessage(`{"type":"object"}`)}}, nil
}
func (fakeTools) Call(context.Context, string, json.RawMessage) (any, error) {
	return map[string]string{"message_id": "reply-id"}, nil
}

func TestClaudeRequiresExplicitAcknowledgementAndStartsAfterHandshake(t *testing.T) {
	input, inWriter := io.Pipe()
	output, outWriter := io.Pipe()
	defer input.Close()
	defer inWriter.Close()
	defer output.Close()
	defer outWriter.Close()
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	server := NewStdio(outWriter, fakeTools{}, true)
	delivered := make(chan error, 1)
	finished := make(chan error, 1)
	go func() {
		finished <- server.Run(ctx, input, func(ctx context.Context) error {
			err := server.Deliver(ctx, Event{ID: "message-1", Sender: "Spike", Recipient: "Val", Payload: json.RawMessage(`{"content":"hello"}`)})
			delivered <- err
			<-ctx.Done()
			return nil
		})
	}()
	decoder := json.NewDecoder(bufio.NewReader(output))
	encoder := json.NewEncoder(inWriter)
	_ = encoder.Encode(map[string]any{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": map[string]string{"protocolVersion": "2025-11-25"}})
	var frame map[string]any
	if err := decoder.Decode(&frame); err != nil {
		t.Fatal(err)
	}
	capabilities := frame["result"].(map[string]any)["capabilities"].(map[string]any)
	if _, ok := capabilities["experimental"].(map[string]any)["claude/channel"]; !ok {
		t.Fatal("missing channel capability")
	}
	select {
	case <-delivered:
		t.Fatal("delivery started before initialisation")
	default:
	}
	_ = encoder.Encode(map[string]any{"jsonrpc": "2.0", "method": "notifications/initialized"})
	if err := decoder.Decode(&frame); err != nil {
		t.Fatal(err)
	}
	if frame["method"] != "notifications/claude/channel" {
		t.Fatalf("wrong frame: %#v", frame)
	}
	select {
	case <-delivered:
		t.Fatal("pipe write was treated as acceptance")
	default:
	}
	_ = encoder.Encode(map[string]any{"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": map[string]any{"name": "acknowledge_delivery", "arguments": map[string]string{"message_id": "message-1"}}})
	if err := decoder.Decode(&frame); err != nil {
		t.Fatal(err)
	}
	select {
	case err := <-delivered:
		if err != nil {
			t.Fatal(err)
		}
	case <-time.After(time.Second):
		t.Fatal("acknowledgement did not release delivery")
	}
	_ = inWriter.Close()
	select {
	case err := <-finished:
		if err != nil {
			t.Fatal(err)
		}
	case <-time.After(time.Second):
		t.Fatal("EOF did not stop channel")
	}
}

func TestClaudeUnknownAcknowledgementDoesNotReleasePendingMessage(t *testing.T) {
	server := NewStdio(io.Discard, fakeTools{}, true)
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	done := make(chan error, 1)
	go func() { done <- server.Deliver(ctx, Event{ID: "wanted", Payload: json.RawMessage(`{}`)}) }()
	if err := server.acknowledge("other"); err == nil {
		t.Fatal("unknown receipt accepted")
	}
	cancel()
	select {
	case err := <-done:
		if err == nil {
			t.Fatal("unacknowledged message accepted")
		}
	case <-time.After(time.Second):
		t.Fatal("delivery ignored cancellation")
	}
}
