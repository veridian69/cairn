package attention

import (
	"context"
	"encoding/json"
	"errors"
	"net"
	"net/http"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/coder/websocket"
)

// Test names stay short: t.TempDir() paths must fit the 108-byte sun_path limit.
func TestCodexDeliveryOutcomes(t *testing.T) {
	for _, mode := range []string{"accept", "reject", "disconnect", "busy", "busy-race", "busy-data", "internal-error"} {
		t.Run(mode, func(t *testing.T) {
			ctx, cancel := context.WithCancel(context.Background())
			defer cancel()
			fake := newFakeCodex(t, "msg-one", mode)
			host, err := startCodex(ctx, fake.socket, "thread-target")
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
			case "reject", "internal-error":
				if !errors.Is(err, ErrRejected) {
					t.Fatalf("got %v", err)
				}
			case "disconnect":
				if !errors.Is(err, ErrUncertain) {
					t.Fatalf("got %v", err)
				}
			case "busy", "busy-race", "busy-data":
				if !errors.Is(err, ErrBusy) {
					t.Fatalf("got %v", err)
				}
			}
		})
	}
}

func TestCodexHandshakeFailures(t *testing.T) {
	for _, tc := range []struct {
		name   string
		mode   string
		socket func(t *testing.T) string
		want   error
	}{
		{name: "refused upgrade", mode: "refuse", want: ErrUnavailable},
		{name: "initialize error", mode: "init-error", want: ErrRejected},
		{name: "resume rejection", mode: "resume-reject", want: ErrRejected},
		{name: "no socket", socket: func(t *testing.T) string { return filepath.Join(t.TempDir(), "missing.sock") }, want: ErrUnavailable},
	} {
		t.Run(tc.name, func(t *testing.T) {
			ctx, cancel := context.WithCancel(context.Background())
			defer cancel()
			socket := ""
			if tc.socket != nil {
				socket = tc.socket(t)
			} else {
				socket = newFakeCodex(t, "msg-one", tc.mode).socket
			}
			host, err := startCodex(ctx, socket, "thread-target")
			if !errors.Is(err, tc.want) {
				t.Fatalf("got host=%v err=%v, want %v", host, err, tc.want)
			}
		})
	}
}

func TestCodexDeadlineClosesStalledConnection(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	fake := newFakeCodex(t, "msg-one", "stall-input")
	host, err := startCodex(ctx, fake.socket, "thread-target")
	if err != nil {
		t.Fatal(err)
	}
	defer host.Close()
	// Larger than the socket buffers, so the submission write blocks. The deadline
	// leaves room for event encoding, which runs before the first request.
	payload, _ := json.Marshal(map[string]string{"content": strings.Repeat("x", 2<<20)})
	deliveryCtx, stop := context.WithTimeout(ctx, time.Second)
	defer stop()
	done := make(chan error, 1)
	go func() {
		done <- host.Deliver(deliveryCtx, Event{ID: "msg-one", Payload: payload})
	}()
	select {
	case err := <-done:
		if !errors.Is(err, ErrUncertain) {
			t.Fatalf("stalled delivery: %v", err)
		}
	case <-time.After(5 * time.Second):
		t.Fatal("deadline did not unblock the stalled connection")
	}
}

func TestCodexReconnectsOnlyBeforeSubmission(t *testing.T) {
	for _, tc := range []struct {
		name  string
		modes []string
	}{
		{name: "preflight", modes: []string{"read-disconnect", "accept"}},
		{name: "ambiguous", modes: []string{"disconnect"}},
	} {
		t.Run(tc.name, func(t *testing.T) {
			ctx, cancel := context.WithCancel(context.Background())
			defer cancel()
			fake := newFakeCodex(t, "msg-one", tc.modes...)
			opens := 0
			open := func(callCtx context.Context) (*Codex, error) {
				opens++
				return startCodex(callCtx, fake.socket, "thread-target")
			}
			host := newCodexAdapter(nil, open)
			defer host.Close()
			event := Event{ID: "msg-one", Payload: json.RawMessage(`{}`)}
			err := host.Deliver(ctx, event)
			if tc.name == "ambiguous" {
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
			fake := newFakeCodex(t, "msg-one", tc.mode)
			connection, err := startCodex(ctx, fake.socket, "thread-target")
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

func TestCodexAdapterDoneRemainsStableAcrossReconnect(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	fake := newFakeCodex(t, "msg-one", "read-disconnect", "close-target")
	open := func(callCtx context.Context) (*Codex, error) {
		return startCodex(callCtx, fake.socket, "thread-target")
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
		t.Fatal("connection loss ended selected task lifecycle")
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

// The server's request is refused with -32601 by the adapter (checked by the
// fake), and the turn then proceeds normally.
func TestCodexRefusesServerRequestsWithMethodNotFound(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	fake := newFakeCodex(t, "msg-one", "server-request")
	host, err := startCodex(ctx, fake.socket, "thread-target")
	if err != nil {
		t.Fatal(err)
	}
	defer host.Close()
	if err := host.Deliver(ctx, Event{ID: "msg-one", Payload: json.RawMessage(`{}`)}); err != nil {
		t.Fatal(err)
	}
}

func TestCodexAdapterDoesNotLoseTerminalLifecycleWhenConnectionAlsoCloses(t *testing.T) {
	for _, order := range []string{"lifecycle-first", "connection-first"} {
		t.Run(order, func(t *testing.T) {
			for attempt := 0; attempt < 40; attempt++ {
				connection := &Codex{done: make(chan struct{}), lifecycle: make(chan struct{})}
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

// fakeCodex serves the app-server control protocol over a Unix socket in
// t.TempDir(). Accepted connections follow modes in order, repeating the last
// one, so a test can script reconnects. It records every request method and
// reports protocol violations through t.
type fakeCodex struct {
	t       *testing.T
	socket  string
	message string
	modes   []string
	stall   chan struct{}
	mu      sync.Mutex
	conns   int
	seen    []string
}

func newFakeCodex(t *testing.T, message string, modes ...string) *fakeCodex {
	t.Helper()
	socket := filepath.Join(t.TempDir(), "codex.sock")
	if len(socket) >= 108 {
		t.Fatalf("socket path exceeds sun_path: %s", socket)
	}
	listener, err := net.Listen("unix", socket)
	if err != nil {
		t.Fatal(err)
	}
	fake := &fakeCodex{t: t, socket: socket, message: message, modes: modes, stall: make(chan struct{})}
	server := &http.Server{Handler: http.HandlerFunc(fake.serve)}
	go func() { _ = server.Serve(listener) }()
	t.Cleanup(func() {
		close(fake.stall)
		_ = server.Close()
	})
	return fake
}

func (f *fakeCodex) received() []string {
	f.mu.Lock()
	defer f.mu.Unlock()
	return append([]string(nil), f.seen...)
}

func (f *fakeCodex) serve(w http.ResponseWriter, r *http.Request) {
	f.mu.Lock()
	mode := f.modes[min(f.conns, len(f.modes)-1)]
	f.conns++
	f.mu.Unlock()
	if mode == "refuse" {
		http.Error(w, "refused", http.StatusForbidden)
		return
	}
	conn, err := websocket.Accept(w, r, nil)
	if err != nil {
		return
	}
	defer conn.CloseNow()
	ctx := r.Context()
	// The real app-server announces unrelated notifications right after connecting.
	_ = f.send(ctx, conn, map[string]any{"method": "remoteControl/status/changed", "params": map[string]any{"status": "disabled"}})
	for {
		_, data, err := conn.Read(ctx)
		if err != nil {
			return
		}
		var frame map[string]any
		if json.Unmarshal(data, &frame) != nil {
			f.t.Errorf("client sent invalid JSON: %q", data)
			return
		}
		method, _ := frame["method"].(string)
		if method == "" || method == "initialized" {
			continue
		}
		f.mu.Lock()
		f.seen = append(f.seen, method)
		f.mu.Unlock()
		if !f.handle(ctx, conn, mode, frame) {
			return
		}
	}
}

// handle answers one request. It returns false when the connection must end.
func (f *fakeCodex) handle(ctx context.Context, conn *websocket.Conn, mode string, frame map[string]any) bool {
	id := frame["id"]
	params, _ := frame["params"].(map[string]any)
	method, _ := frame["method"].(string)
	reply := func(result any) bool {
		return f.send(ctx, conn, map[string]any{"id": id, "result": result}) == nil
	}
	fail := func(code int, message string, data any) bool {
		body := map[string]any{"code": code, "message": message}
		if data != nil {
			body["data"] = data
		}
		return f.send(ctx, conn, map[string]any{"id": id, "error": body}) == nil
	}
	threadResult := map[string]any{"thread": map[string]any{"id": "thread-target"}}
	switch method {
	case "initialize":
		if mode == "init-drop" {
			return false
		}
		if mode == "init-stall" {
			<-f.stall
			return false
		}
		if mode == "init-error" {
			return fail(-32000, "app-server not ready", nil)
		}
		return reply(map[string]any{"userAgent": "fake-codex"})
	case "thread/resume":
		if mode == "resume-drop" {
			return false
		}
		if mode == "resume-reject" {
			return fail(-32600, "no rollout found", nil)
		}
		if params["threadId"] != "thread-target" {
			f.t.Errorf("resume of unexpected thread %v", params["threadId"])
			return false
		}
		if !reply(threadResult) {
			return false
		}
		switch mode {
		case "close-target":
			_ = f.send(ctx, conn, map[string]any{"method": "thread/closed", "params": map[string]any{"threadId": "thread-target"}})
		case "close-other":
			_ = f.send(ctx, conn, map[string]any{"method": "thread/closed", "params": map[string]any{"threadId": "thread-other"}})
		case "archive-target":
			_ = f.send(ctx, conn, map[string]any{"method": "thread/archived", "params": map[string]any{"threadId": "thread-target"}})
		}
		return true
	case "thread/read":
		if mode == "read-disconnect" {
			return false
		}
		if stage, ok := strings.CutSuffix(mode, "-before"); ok {
			return f.corrupt(ctx, conn, stage)
		}
		state := "idle"
		if mode == "busy" {
			state = "active"
		}
		if !reply(map[string]any{"thread": map[string]any{"id": "thread-target", "status": map[string]string{"type": state}}}) {
			return false
		}
		if mode == "stall-input" {
			// Stop reading, so the submission that follows cannot finish writing.
			<-f.stall
			return false
		}
		return true
	case "turn/start":
		if stage, ok := strings.CutSuffix(mode, "-after"); ok {
			return f.corrupt(ctx, conn, stage)
		}
		switch mode {
		case "disconnect":
			return false
		case "reject":
			return fail(-32602, "private host diagnostic", nil)
		case "busy-race":
			// Codex 0.163 reports a non-steerable active turn as an internal error.
			return fail(-32603, "ActiveTurnNotSteerable: a review turn is active", nil)
		case "busy-data":
			return fail(-32000, "busy", map[string]any{"codexErrorInfo": map[string]any{"activeTurnNotSteerable": map[string]any{"turnKind": "review"}}})
		case "internal-error":
			return fail(-32603, "internal error", nil)
		case "busy":
			f.t.Errorf("turn/start sent to an active thread")
			return false
		case "server-request":
			// A host request must be refused with -32601 before the turn proceeds.
			if f.send(ctx, conn, map[string]any{"id": "srv-1", "method": "item/tool/call", "params": map[string]any{}}) != nil {
				return false
			}
			_, data, err := conn.Read(ctx)
			var answer struct {
				ID    string `json:"id"`
				Error *struct {
					Code int `json:"code"`
				} `json:"error"`
			}
			if err != nil || json.Unmarshal(data, &answer) != nil || answer.ID != "srv-1" || answer.Error == nil || answer.Error.Code != -32601 {
				f.t.Errorf("server request not refused with -32601: %q", data)
				return false
			}
		}
		if !f.validTurn(params) {
			f.t.Errorf("unexpected turn/start params: %v", params)
			return false
		}
		_ = f.send(ctx, conn, map[string]any{"method": "turn/started", "params": map[string]any{}})
		return reply(map[string]any{"turn": map[string]any{"id": "turn-one", "status": "inProgress"}})
	default:
		f.t.Errorf("unexpected method %q", method)
		return false
	}
}

// validTurn checks the external tool output and rejects host-owned overrides.
func (f *fakeCodex) validTurn(params map[string]any) bool {
	if params["threadId"] != "thread-target" {
		return false
	}
	if input, ok := params["input"].([]any); !ok || len(input) != 0 {
		return false
	}
	out, ok := params["toolOutput"].(map[string]any)
	if !ok || out["name"] != "attention_message" || out["namespace"] != "garden" {
		return false
	}
	text, _ := out["output"].(string)
	if !strings.Contains(text, f.message) {
		return false
	}
	for _, field := range []string{"approvalPolicy", "sandboxPolicy", "model", "cwd"} {
		if _, exists := params[field]; exists {
			return false
		}
	}
	return true
}

func (f *fakeCodex) send(ctx context.Context, conn *websocket.Conn, frame map[string]any) error {
	data, err := json.Marshal(frame)
	if err != nil {
		return err
	}
	return conn.Write(ctx, websocket.MessageText, data)
}

// corrupt sends a frame the adapter must refuse, then ends the connection.
func (f *fakeCodex) corrupt(ctx context.Context, conn *websocket.Conn, kind string) bool {
	switch kind {
	case "binary":
		_ = conn.Write(ctx, websocket.MessageBinary, []byte{0x00, 0x01})
	case "malformed":
		_ = conn.Write(ctx, websocket.MessageText, []byte("{not json"))
	case "oversized":
		_ = conn.Write(ctx, websocket.MessageText, []byte(`{"id":1,"result":"`+strings.Repeat("x", 3<<20)+`"}`))
	}
	return false
}

// Setup and read requests that lose their connection, or stall until the caller's
// deadline, are unavailable: nothing was submitted.
func TestCodexSetupFailuresAreUnavailable(t *testing.T) {
	for _, tc := range []struct {
		name    string
		mode    string
		timeout time.Duration
	}{
		{name: "initialize dropped", mode: "init-drop"},
		{name: "initialize stalled", mode: "init-stall", timeout: 200 * time.Millisecond},
		{name: "resume dropped", mode: "resume-drop"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			ctx, cancel := context.WithCancel(context.Background())
			defer cancel()
			fake := newFakeCodex(t, "msg-one", tc.mode)
			if tc.timeout > 0 {
				var stop context.CancelFunc
				ctx, stop = context.WithTimeout(ctx, tc.timeout)
				defer stop()
			}
			host, err := startCodex(ctx, fake.socket, "thread-target")
			if !errors.Is(err, ErrUnavailable) {
				t.Fatalf("got host=%v err=%v, want unavailable", host, err)
			}
		})
	}
}

func TestDoctorReadDisconnectIsUnavailable(t *testing.T) {
	fake := newFakeCodex(t, "msg-one", "read-disconnect")
	if err := checkCodexSocket(context.Background(), fake.socket, "thread-target"); !errors.Is(err, ErrUnavailable) {
		t.Fatalf("doctor read disconnect: %v", err)
	}
}

// Binary, malformed and oversized frames end the connection. Before submission the
// outcome is unavailable; during turn/start it is uncertain. Either way the
// connection must be released, so no reader is left parked.
func TestCodexRejectsCorruptFramesAndReleasesConnection(t *testing.T) {
	for _, kind := range []string{"binary", "malformed", "oversized"} {
		for _, stage := range []string{"before", "after"} {
			t.Run(kind+"-"+stage, func(t *testing.T) {
				ctx, cancel := context.WithCancel(context.Background())
				defer cancel()
				fake := newFakeCodex(t, "msg-one", kind+"-"+stage)
				host, err := startCodex(ctx, fake.socket, "thread-target")
				if err != nil {
					t.Fatal(err)
				}
				defer host.Close()
				err = host.Deliver(ctx, Event{ID: "msg-one", Payload: json.RawMessage(`{}`)})
				want := ErrUnavailable
				if stage == "after" {
					want = ErrUncertain
				}
				if !errors.Is(err, want) {
					t.Fatalf("got %v, want %v", err, want)
				}
				select {
				case <-host.done:
				case <-time.After(2 * time.Second):
					t.Fatal("connection left open after a corrupt frame")
				}
			})
		}
	}
}
