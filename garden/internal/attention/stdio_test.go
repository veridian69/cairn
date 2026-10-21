package attention

import (
	"bufio"
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"io"
	"strings"
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
	server := NewStdio(outWriter, fakeTools{}, true, ProfileInstructions)
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
	server := NewStdio(io.Discard, fakeTools{}, true, ProfileInstructions)
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

// exchange feeds JSON-RPC lines to a stdio server and returns every frame it wrote.
func exchange(t *testing.T, tools Tools, instructions string, lines ...string) []map[string]any {
	t.Helper()
	var out bytes.Buffer
	server := NewStdio(&out, tools, false, instructions)
	input := strings.Join(lines, "\n") + "\n"
	if err := server.Run(context.Background(), io.NopCloser(strings.NewReader(input)), nil); err != nil {
		t.Fatal(err)
	}
	var frames []map[string]any
	decoder := json.NewDecoder(&out)
	for decoder.More() {
		var frame map[string]any
		if err := decoder.Decode(&frame); err != nil {
			t.Fatal(err)
		}
		frames = append(frames, frame)
	}
	return frames
}

const (
	handshake = `{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-11-25"}}`
	ready     = `{"jsonrpc":"2.0","method":"notifications/initialized"}`
)

type safeErrorTools struct{ fakeTools }

func (safeErrorTools) Call(context.Context, string, json.RawMessage) (any, error) {
	return nil, &SafeError{Message: "unknown recipient; known names: Spike, Val"}
}

func TestStdioShowsSafeToolErrorsAndKeepsGenericForOthers(t *testing.T) {
	frames := exchange(t, safeErrorTools{}, ProfileInstructions, handshake, ready,
		`{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"send_message","arguments":{"content":"x"}}}`)
	text := frames[1]["result"].(map[string]any)["content"].([]any)[0].(map[string]any)["text"]
	if text != "unknown recipient; known names: Spike, Val" {
		t.Fatalf("safe error not shown: %v", text)
	}
	frames = exchange(t, failingTools{}, ProfileInstructions, handshake, ready,
		`{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"send_message","arguments":{"content":"x"}}}`)
	text = frames[1]["result"].(map[string]any)["content"].([]any)[0].(map[string]any)["text"]
	if text != "Garden operation failed; delivery remains pending where applicable." {
		t.Fatalf("generic error changed: %v", text)
	}
}

type failingTools struct{ fakeTools }

func (failingTools) Call(context.Context, string, json.RawMessage) (any, error) {
	return nil, errors.New("internal detail")
}

func TestStdioUsesSuppliedInstructions(t *testing.T) {
	frames := exchange(t, fakeTools{}, "custom instructions", handshake)
	if got := frames[0]["result"].(map[string]any)["instructions"]; got != "custom instructions" {
		t.Fatalf("instructions not supplied: %v", got)
	}
}

func TestDisconnectedStdioAdvertisesOnlyStatusWithoutChannel(t *testing.T) {
	session := &HarnessSession{Tools: Disconnected{Identity: "spike", Reason: "no default identity for claude"}}
	frames := exchange(t, session.Tools, "x", handshake, ready, `{"jsonrpc":"2.0","id":2,"method":"tools/list"}`)
	if _, ok := frames[0]["result"].(map[string]any)["capabilities"].(map[string]any)["experimental"]; ok {
		t.Fatal("disconnected session advertised a channel")
	}
	tools := frames[1]["result"].(map[string]any)["tools"].([]any)
	if len(tools) != 1 || tools[0].(map[string]any)["name"] != "status" {
		t.Fatalf("disconnected tools: %v", tools)
	}
}
