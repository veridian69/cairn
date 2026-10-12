package attention

import (
	"bufio"
	"context"
	"encoding/json"
	"errors"
	"io"
	"os"
	"os/exec"
	"strings"
	"testing"
	"time"
)

func TestCodexDeliversExternalToolOutputAndWaitsForAcceptance(t *testing.T) {
	for _, mode := range []string{"accept", "reject", "disconnect", "busy", "busy-race"} {
		t.Run(mode, func(t *testing.T) {
			ctx, cancel := context.WithCancel(context.Background())
			defer cancel()
			command := exec.CommandContext(ctx, os.Args[0], "-test.run=TestCodexWireHelper")
			command.Env = append(os.Environ(), "GARDEN_CODEX_TEST_HELPER="+mode)
			host, err := startCodex(ctx, command, "thread-target")
			if err != nil {
				t.Fatal(err)
			}
			defer host.Close()
			err = host.Deliver(ctx, Event{ID: "msg-one", Sender: "Spike", Recipient: "Val", Payload: json.RawMessage(`{"content":"review"}`)})
			switch mode {
			case "accept":
				if err != nil {
					t.Fatal(err)
				}
			case "reject":
				if !errors.Is(err, ErrRejected) {
					t.Fatalf("got %v", err)
				}
			case "disconnect":
				if !errors.Is(err, ErrUncertain) {
					t.Fatalf("got %v", err)
				}
			case "busy", "busy-race":
				if !errors.Is(err, ErrBusy) {
					t.Fatalf("got %v", err)
				}
			}
		})
	}
}

// A fake host executable exercises actual JSONL pipes and response correlation.
func TestCodexWireHelper(t *testing.T) {
	mode := os.Getenv("GARDEN_CODEX_TEST_HELPER")
	if mode == "" {
		return
	}
	scanner := bufio.NewScanner(os.Stdin)
	for scanner.Scan() {
		var frame map[string]any
		if json.Unmarshal(scanner.Bytes(), &frame) != nil {
			os.Exit(11)
		}
		method, _ := frame["method"].(string)
		if method == "initialized" {
			continue
		}
		params, _ := frame["params"].(map[string]any)
		result := map[string]any{}
		switch method {
		case "initialize":
			result["userAgent"] = "synthetic"
		case "thread/resume":
			if mode == "probe" {
				os.Exit(25)
			}
			if params["threadId"] != "thread-target" {
				os.Exit(12)
			}
			result["thread"] = map[string]any{"id": "thread-target"}
		case "thread/read":
			if mode == "read-disconnect" {
				os.Exit(0)
			}
			state := "idle"
			if mode == "busy" {
				state = "active"
			}
			result["thread"] = map[string]any{"id": "thread-target", "status": map[string]string{"type": state}}
		case "turn/start":
			if mode == "probe" {
				os.Exit(26)
			}
			if mode == "busy" {
				os.Exit(18)
			}
			if params["threadId"] != "thread-target" || len(params["input"].([]any)) != 0 {
				os.Exit(13)
			}
			out, ok := params["toolOutput"].(map[string]any)
			if !ok {
				os.Exit(14)
			}
			expectedID := os.Getenv("GARDEN_CODEX_MESSAGE_ID")
			if expectedID == "" {
				expectedID = "msg-one"
			}
			if out["name"] != "attention_message" || out["namespace"] != "garden" || !strings.Contains(out["output"].(string), expectedID) {
				os.Exit(15)
			}
			for _, field := range []string{"approvalPolicy", "sandboxPolicy", "model", "cwd"} {
				if _, exists := params[field]; exists {
					os.Exit(16)
				}
			}
			if mode == "disconnect" {
				os.Exit(0)
			}
			if mode == "reject" {
				_ = json.NewEncoder(os.Stdout).Encode(map[string]any{"id": frame["id"], "error": map[string]any{"code": -32602, "message": "private host diagnostic"}})
				continue
			}
			if mode == "busy-race" {
				_ = json.NewEncoder(os.Stdout).Encode(map[string]any{"id": frame["id"], "error": map[string]any{"code": -32000, "message": "busy", "data": map[string]any{"codexErrorInfo": map[string]any{"activeTurnNotSteerable": map[string]any{"turnKind": "review"}}}}})
				continue
			}
			_, _ = io.WriteString(os.Stdout, "{\"method\":\"turn/started\",\"params\":{}}\n")
			result["turn"] = map[string]any{"id": "turn-one", "status": "inProgress"}
		default:
			os.Exit(17)
		}
		_ = json.NewEncoder(os.Stdout).Encode(map[string]any{"id": frame["id"], "result": result})
		if method == "thread/resume" && strings.HasPrefix(mode, "close-") {
			thread := "thread-other"
			if mode == "close-target" {
				thread = "thread-target"
			}
			_ = json.NewEncoder(os.Stdout).Encode(map[string]any{"method": "thread/closed", "params": map[string]any{"threadId": thread}})
		}
		if method == "thread/resume" && mode == "archive-target" {
			_ = json.NewEncoder(os.Stdout).Encode(map[string]any{"method": "thread/archived", "params": map[string]any{"threadId": "thread-target"}})
		}
		if mode == "stall-input" && method == "thread/read" {
			time.Sleep(time.Minute)
		}
	}
	os.Exit(0)
}

func TestCodexCancellationUnblocksStalledSubmissionPipe(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	command := exec.CommandContext(ctx, os.Args[0], "-test.run=TestCodexWireHelper")
	command.Env = append(os.Environ(), "GARDEN_CODEX_TEST_HELPER=stall-input")
	host, err := startCodex(ctx, command, "thread-target")
	if err != nil {
		t.Fatal(err)
	}
	defer host.Close()
	payload, _ := json.Marshal(map[string]string{"content": strings.Repeat("x", 64*1024)})
	deliveryCtx, stop := context.WithTimeout(ctx, 100*time.Millisecond)
	defer stop()
	done := make(chan error, 1)
	go func() { done <- host.Deliver(deliveryCtx, Event{ID: "msg-one", Payload: payload}) }()
	select {
	case err := <-done:
		if !errors.Is(err, ErrUncertain) {
			t.Fatalf("stalled submission: %v", err)
		}
	case <-time.After(2 * time.Second):
		t.Fatal("cancellation did not unblock pipe")
	}
}

func TestCodexReconnectsOnlyBeforeSubmission(t *testing.T) {
	for _, mode := range []string{"read-disconnect", "disconnect"} {
		t.Run(mode, func(t *testing.T) {
			ctx, cancel := context.WithCancel(context.Background())
			defer cancel()
			opens := 0
			open := func(callCtx context.Context) (*Codex, error) {
				opens++
				selected := mode
				if opens > 1 {
					selected = "accept"
				}
				command := exec.CommandContext(ctx, os.Args[0], "-test.run=TestCodexWireHelper")
				command.Env = append(os.Environ(), "GARDEN_CODEX_TEST_HELPER="+selected)
				return startCodex(callCtx, command, "thread-target")
			}
			host := newCodexAdapter(nil, open)
			defer host.Close()
			event := Event{ID: "msg-one", Payload: json.RawMessage(`{}`)}
			err := host.Deliver(ctx, event)
			if mode == "disconnect" {
				if !errors.Is(err, ErrUncertain) || opens != 1 || host.connection == nil {
					t.Fatalf("ambiguous submit must stop: %v opens=%d", err, opens)
				}
				return
			}
			if !errors.Is(err, ErrUnavailable) {
				t.Fatalf("preflight failure: %v", err)
			}
			if err = host.Deliver(ctx, event); err != nil || opens != 2 {
				t.Fatalf("reconnect: %v opens=%d", err, opens)
			}
		})
	}
}

func TestCodexAdapterStopsOnlyForSelectedThreadLifecycle(t *testing.T) {
	for _, tc := range []struct {
		name       string
		mode       string
		wantClosed bool
	}{
		{name: "closed", mode: "close-target", wantClosed: true},
		{name: "archived", mode: "archive-target", wantClosed: true},
		{name: "other thread", mode: "close-other", wantClosed: false},
	} {
		t.Run(tc.name, func(t *testing.T) {
			ctx, cancel := context.WithCancel(context.Background())
			defer cancel()
			command := exec.CommandContext(ctx, os.Args[0], "-test.run=TestCodexWireHelper")
			command.Env = append(os.Environ(), "GARDEN_CODEX_TEST_HELPER="+tc.mode)
			connection, err := startCodex(ctx, command, "thread-target")
			if err != nil {
				t.Fatal(err)
			}
			host := newCodexAdapter(connection, nil)
			defer host.Close()

			select {
			case <-host.Done():
				if !tc.wantClosed {
					t.Fatal("another thread's lifecycle notification stopped the adapter")
				}
			case <-time.After(100 * time.Millisecond):
				if tc.wantClosed {
					t.Fatal("selected thread lifecycle notification did not stop the adapter")
				}
			}

			if !tc.wantClosed {
				if err := host.Deliver(ctx, Event{ID: "msg-one", Payload: json.RawMessage(`{}`)}); err != nil {
					t.Fatalf("adapter stopped after unrelated notification: %v", err)
				}
			}
		})
	}
}

func TestCodexAdapterDoneRemainsStableAcrossProxyReconnect(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	opens := 0
	open := func(callCtx context.Context) (*Codex, error) {
		opens++
		mode := "read-disconnect"
		if opens > 1 {
			mode = "close-target"
		}
		command := exec.CommandContext(ctx, os.Args[0], "-test.run=TestCodexWireHelper")
		command.Env = append(os.Environ(), "GARDEN_CODEX_TEST_HELPER="+mode)
		return startCodex(callCtx, command, "thread-target")
	}
	host := newCodexAdapter(nil, open)
	defer host.Close()
	done := host.Done()
	event := Event{ID: "msg-one", Payload: json.RawMessage(`{}`)}
	if err := host.Deliver(ctx, event); !errors.Is(err, ErrUnavailable) {
		t.Fatalf("preflight disconnect: %v", err)
	}
	select {
	case <-done:
		t.Fatal("proxy disconnect ended selected task lifecycle")
	default:
	}
	_ = host.Deliver(ctx, event)
	if host.Done() != done {
		t.Fatal("Done channel changed across reconnect")
	}
	select {
	case <-done:
	case <-time.After(time.Second):
		t.Fatal("selected task closure after reconnect was not observed")
	}
}

func TestCodexAdapterDoesNotLoseTerminalLifecycleWhenProxyAlsoCloses(t *testing.T) {
	for _, order := range []string{"lifecycle-first", "proxy-first"} {
		t.Run(order, func(t *testing.T) {
			for attempt := 0; attempt < 40; attempt++ {
				connection := &Codex{done: make(chan struct{}), lifecycle: make(chan struct{}), command: &exec.Cmd{}}
				// The watcher may close this synthetic connection; make that a no-op.
				connection.closeOnce.Do(func() {})
				if order == "lifecycle-first" {
					connection.signalLifecycle()
					close(connection.done)
				} else {
					close(connection.done)
					connection.signalLifecycle()
				}
				opens := 0
				host := &CodexAdapter{
					connection: connection,
					open: func(context.Context) (*Codex, error) {
						opens++
						return nil, ErrUnavailable
					},
					done: make(chan struct{}),
				}
				host.watch(connection)
				select {
				case <-host.Done():
				case <-time.After(time.Second):
					t.Fatalf("attempt %d lost terminal lifecycle", attempt)
				}
				if err := host.Deliver(context.Background(), Event{}); !errors.Is(err, ErrRejected) {
					t.Fatalf("attempt %d delivery after terminal lifecycle: %v", attempt, err)
				}
				if opens != 0 {
					t.Fatalf("attempt %d reconnected a closed task", attempt)
				}
			}
		})
	}
}

func TestCodexAdapterRejectsDeliveryAsSoonAsLifecycleDoneIsClosed(t *testing.T) {
	done := make(chan struct{})
	close(done)
	opens := 0
	host := &CodexAdapter{
		done: done,
		open: func(context.Context) (*Codex, error) {
			opens++
			return nil, ErrUnavailable
		},
	}
	if err := host.Deliver(context.Background(), Event{}); !errors.Is(err, ErrRejected) {
		t.Fatalf("delivery after lifecycle Done: %v", err)
	}
	if opens != 0 {
		t.Fatalf("reconnected after lifecycle Done: %d", opens)
	}
}
