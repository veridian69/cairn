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
	"sync/atomic"
	"time"

	"github.com/coder/websocket"
)

const (
	// codexCallTimeout bounds each request, including the connection handshake.
	codexCallTimeout = 20 * time.Second
	// codexMaxMessageBytes matches the frame limit of the former line-JSON proxy.
	codexMaxMessageBytes = 2 * 1024 * 1024
)

// Codex attaches to an explicitly selected existing app-server control socket.
// Codex 0.163 serves JSON-RPC as WebSocket text frames on that Unix socket, and
// its `app-server proxy` only relays bytes, so Garden dials the socket directly.
// It does not start a separate agent server or select the latest task.
type Codex struct {
	conn      *websocket.Conn
	frames    chan rpcResponse
	done      chan struct{}
	lifecycle chan struct{}
	mu        sync.Mutex
	closeOnce sync.Once
	lifeOnce  sync.Once
	terminal  atomic.Bool
	seq       int
	thread    string
}

type rpcResponse struct {
	ID     int             `json:"id"`
	Result json.RawMessage `json:"result"`
	Error  *struct {
		Code    int             `json:"code"`
		Message string          `json:"message"`
		Data    json.RawMessage `json:"data"`
	} `json:"error"`
}

// CodexAdapter reconnects a lost control socket only before message submission.
// It keeps the explicitly selected host and task; uncertain turns are never retried.
type CodexAdapter struct {
	mu         sync.Mutex
	connection *Codex
	open       func(context.Context) (*Codex, error)
	done       chan struct{}
	doneOnce   sync.Once
	closed     bool
}

func NewCodex(ctx context.Context, socket, thread string) (*CodexAdapter, error) {
	if !filepath.IsAbs(socket) || !safeSession(thread) {
		return nil, errors.New("Codex requires an absolute control socket and explicit task ID")
	}
	open := func(callCtx context.Context) (*Codex, error) {
		return startCodex(callCtx, socket, thread)
	}
	connection, err := open(ctx)
	if err != nil {
		return nil, err
	}
	return newCodexAdapter(connection, open), nil
}

func newCodexAdapter(connection *Codex, open func(context.Context) (*Codex, error)) *CodexAdapter {
	h := &CodexAdapter{connection: connection, open: open, done: make(chan struct{})}
	if connection != nil {
		h.watch(connection)
	}
	return h
}

// Done closes when the explicitly selected Codex task is closed or archived.
// The channel belongs to the adapter and remains stable across proxy reconnects.
func (h *CodexAdapter) Done() <-chan struct{} { return h.done }

func (h *CodexAdapter) watch(connection *Codex) {
	go func() {
		select {
		case <-connection.lifecycle:
		case <-connection.done:
		case <-h.done:
			return
		}
		// A terminal notification and proxy shutdown can make both channels
		// ready. The recorded state is authoritative regardless of which arm
		// select chooses.
		if !connection.terminal.Load() {
			return
		}
		h.doneOnce.Do(func() { close(h.done) })
		_ = connection.Close()
		h.mu.Lock()
		h.closed = true
		if h.connection == connection {
			h.connection = nil
		}
		h.mu.Unlock()
	}()
}

func (h *CodexAdapter) Deliver(ctx context.Context, event Event) error {
	h.mu.Lock()
	defer h.mu.Unlock()
	if h.closed {
		return ErrRejected
	}
	select {
	case <-h.done:
		return ErrRejected
	default:
	}
	if h.connection == nil {
		connection, err := h.open(ctx)
		if err != nil {
			if errors.Is(err, ErrRejected) {
				return ErrRejected
			}
			return ErrUnavailable
		}
		h.connection = connection
		h.watch(connection)
	}
	err := h.connection.Deliver(ctx, event)
	if errors.Is(err, ErrUnavailable) {
		_ = h.connection.Close()
		h.connection = nil
	}
	return err
}

func (h *CodexAdapter) Close() error {
	h.mu.Lock()
	defer h.mu.Unlock()
	h.closed = true
	h.doneOnce.Do(func() { close(h.done) })
	if h.connection != nil {
		return h.connection.Close()
	}
	return nil
}

func startCodex(ctx context.Context, socket, thread string) (*Codex, error) {
	return startCodexConnection(ctx, socket, thread, true)
}

func startCodexConnection(ctx context.Context, socket, thread string, resume bool) (*Codex, error) {
	// The Unix socket is dialled for every connection; the URL host is only the
	// HTTP Host header that the app-server expects for its WebSocket upgrade.
	var dialer net.Dialer
	transport := &http.Transport{DialContext: func(ctx context.Context, _, _ string) (net.Conn, error) {
		return dialer.DialContext(ctx, "unix", socket)
	}}
	dialCtx, cancelDial := context.WithTimeout(ctx, codexCallTimeout)
	defer cancelDial()
	conn, _, err := websocket.Dial(dialCtx, "ws://localhost/", &websocket.DialOptions{
		HTTPClient: &http.Client{Transport: transport},
		// Negotiating permessage-deflate makes the 0.154 control socket drop the upgrade.
		CompressionMode: websocket.CompressionDisabled,
	})
	if err != nil {
		return nil, ErrUnavailable
	}
	conn.SetReadLimit(codexMaxMessageBytes)
	h := &Codex{conn: conn, frames: make(chan rpcResponse, 8), done: make(chan struct{}), lifecycle: make(chan struct{}), thread: thread}
	go h.read()
	if _, err := h.call(ctx, "initialize", map[string]any{"clientInfo": map[string]string{"name": "garden-attention", "version": "1"}, "capabilities": map[string]bool{"experimentalApi": true}}); err != nil {
		_ = h.Close()
		return nil, beforeSubmission(err)
	}
	if err := h.write(ctx, map[string]any{"method": "initialized"}); err != nil {
		_ = h.Close()
		return nil, ErrUnavailable
	}
	if resume {
		if _, err := h.call(ctx, "thread/resume", map[string]any{"threadId": thread, "excludeTurns": true}); err != nil {
			_ = h.Close()
			return nil, beforeSubmission(err)
		}
	}
	return h, nil
}

func (h *Codex) read() {
	// Any reader exit ends the connection: pending calls fail and the watcher sees done.
	defer h.Close()
	defer close(h.frames)
	for {
		// Only text frames carry JSON-RPC; anything else ends the connection.
		typ, data, err := h.conn.Read(context.Background())
		if err != nil || typ != websocket.MessageText {
			return
		}
		var head struct {
			ID     json.RawMessage `json:"id"`
			Method string          `json:"method"`
		}
		if json.Unmarshal(data, &head) != nil {
			return
		}
		if head.Method != "" {
			// Never grant a permission or answer a host request on an agent's behalf.
			if len(head.ID) > 0 {
				ctx, cancel := context.WithTimeout(context.Background(), codexCallTimeout)
				err := h.write(ctx, map[string]any{"id": head.ID, "error": map[string]any{"code": -32601, "message": "Garden adapter does not handle host approvals or requests"}})
				cancel()
				if err != nil {
					return
				}
			} else if head.Method == "thread/closed" || head.Method == "thread/archived" {
				var notice struct {
					Params struct {
						ThreadID string `json:"threadId"`
					} `json:"params"`
				}
				if json.Unmarshal(data, &notice) == nil && notice.Params.ThreadID == h.thread {
					h.signalLifecycle()
				}
			}
			continue
		}
		if len(head.ID) == 0 {
			continue
		}
		var frame rpcResponse
		if json.Unmarshal(data, &frame) != nil {
			return
		}
		select {
		case h.frames <- frame:
		case <-h.done:
			return
		}
	}
}

func (h *Codex) signalLifecycle() {
	h.terminal.Store(true)
	h.lifeOnce.Do(func() { close(h.lifecycle) })
}

func (h *Codex) write(ctx context.Context, frame any) error {
	data, err := json.Marshal(frame)
	if err != nil {
		return err
	}
	// The WebSocket library permits concurrent writes, so no extra lock is needed.
	return h.conn.Write(ctx, websocket.MessageText, data)
}

func (h *Codex) call(ctx context.Context, method string, params any) (json.RawMessage, error) {
	h.mu.Lock()
	defer h.mu.Unlock()
	ctx, cancel := context.WithTimeout(ctx, codexCallTimeout)
	defer cancel()
	// Closing this connection unblocks a stalled write as well as a response
	// wait. The app-server itself and its task remain owned by the host.
	stopClose := context.AfterFunc(ctx, func() { _ = h.Close() })
	defer stopClose()
	h.seq++
	id := h.seq
	if err := h.write(ctx, map[string]any{"id": id, "method": method, "params": params}); err != nil {
		return nil, ErrUncertain
	}
	for {
		select {
		case frame, ok := <-h.frames:
			if !ok {
				return nil, ErrUncertain
			}
			if frame.ID != id {
				continue
			}
			if frame.Error != nil {
				if method == "turn/start" && activeTurnNotSteerable(frame.Error.Code, frame.Error.Message, frame.Error.Data) {
					return nil, ErrBusy
				}
				return nil, ErrRejected
			}
			return frame.Result, nil
		case <-ctx.Done():
			return nil, ErrUncertain
		}
	}
}

func (h *Codex) Deliver(ctx context.Context, event Event) error {
	text, err := event.Text()
	if err != nil {
		return err
	}
	status, err := h.call(ctx, "thread/read", map[string]any{"threadId": h.thread, "includeTurns": false})
	if err != nil {
		if errors.Is(err, ErrRejected) {
			return ErrRejected
		}
		return ErrUnavailable
	}
	var task struct {
		Thread struct {
			ID     string `json:"id"`
			Status struct {
				Type string `json:"type"`
			} `json:"status"`
		} `json:"thread"`
	}
	if json.Unmarshal(status, &task) != nil || task.Thread.ID != h.thread {
		return ErrRejected
	}
	if task.Thread.Status.Type == "active" {
		return ErrBusy
	}
	if task.Thread.Status.Type != "idle" {
		return ErrUnavailable
	}
	result, err := h.call(ctx, "turn/start", map[string]any{"threadId": h.thread, "input": []any{}, "toolOutput": map[string]any{"namespace": "garden", "name": "attention_message", "output": text}})
	if err != nil {
		return err
	}
	var response struct {
		Turn struct {
			ID string `json:"id"`
		} `json:"turn"`
	}
	if json.Unmarshal(result, &response) != nil || response.Turn.ID == "" {
		return ErrUncertain
	}
	return nil
}

func (h *Codex) Close() error {
	h.closeOnce.Do(func() {
		close(h.done)
		_ = h.conn.CloseNow()
	})
	return nil
}

// beforeSubmission keeps transport uncertainty from setup and read requests
// unambiguous: none of them can have submitted a turn, so only turn/start may
// return ErrUncertain.
func beforeSubmission(err error) error {
	if errors.Is(err, ErrUncertain) {
		return ErrUnavailable
	}
	return err
}

// activeTurnNotSteerable reports that turn/start was refused because a review or
// compaction turn is already running. Codex 0.163 returns an internal error whose
// message names the variant; the structured codexErrorInfo form from the schema
// is accepted too. Any other refusal stays a rejection.
func activeTurnNotSteerable(code int, message string, data json.RawMessage) bool {
	if code == -32603 && strings.Contains(strings.ToLower(message), "activeturnnotsteerable") {
		return true
	}
	var info struct {
		CodexErrorInfo map[string]json.RawMessage `json:"codexErrorInfo"`
	}
	if json.Unmarshal(data, &info) != nil {
		return false
	}
	detail, ok := info.CodexErrorInfo["activeTurnNotSteerable"]
	return ok && len(detail) > 0 && detail[0] == '{'
}
