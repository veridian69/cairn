package attention

import (
	"bufio"
	"context"
	"encoding/json"
	"errors"
	"io"
	"sync"
	"time"
)

// Tool is the MCP tool description advertised by the remote Garden proxy.
type Tool struct {
	Name        string          `json:"name"`
	Description string          `json:"description"`
	InputSchema json.RawMessage `json:"inputSchema"`
}

type Tools interface {
	List(context.Context) ([]Tool, error)
	Call(context.Context, string, json.RawMessage) (any, error)
}

const (
	// claudeChannelMethod is Claude Code's channel notification, and the custom
	// capability that announces it, as used by the inspected TeleClaude plugin.
	claudeChannelMethod = "notifications/claude/channel"
	// openCodeMessageMethod delivers a Garden message to the OpenCode plugin, which injects it.
	openCodeMessageMethod = "notifications/garden/message"
	// openCodeSessionMethod is the plugin's report of the session the human last prompted.
	openCodeSessionMethod = "notifications/garden/session"
	// openCodeFailureMethod is the plugin's report that it could not inject a delivered message.
	openCodeFailureMethod = "notifications/garden/delivery_failed"
	// openCodeInstructionsMethod replaces the instructions the plugin pushes into the system prompt.
	openCodeInstructionsMethod = "notifications/garden/instructions"
)

// Stdio is an MCP proxy with optional channel delivery: Claude Code channel events by
// default, or the Garden notification the OpenCode plugin consumes after UseOpenCode.
type Stdio struct {
	out          io.Writer
	tools        Tools
	channel      bool
	method       string
	instructions string
	writeMu      sync.Mutex
	mu           sync.Mutex
	pending      string
	ack          chan struct{}
	probeID      string
	probeFrame   any
	probeDone    string
	onProbe      func()
	onSession    func(string)
	onFailure    func(string)
	// onThread receives the Codex thread ID that each tools/call reports in its _meta; nil for other harnesses.
	onThread func(string)
	// failed carries the reason for the pending delivery when the plugin reports a failure.
	failed chan string
}

func NewStdio(out io.Writer, tools Tools, channel bool, instructions string) *Stdio {
	s := &Stdio{out: out, tools: tools, channel: channel, instructions: instructions}
	if channel {
		s.method = claudeChannelMethod
	}
	return s
}

// UseOpenCode switches a connected session's delivery to the OpenCode plugin. onSession
// receives each session ID the plugin reports, and onFailure each reason it reports for a
// delivery it could not inject. Neither is called for other harnesses.
func (s *Stdio) UseOpenCode(onSession func(string), onFailure func(string)) {
	s.method = openCodeMessageMethod
	s.onSession = onSession
	s.onFailure = onFailure
}

// UseCodex makes the Codex thread ID that each tools/call reports in its _meta visible to onThread.
// Codex starts one MCP server per thread, so this is how the core learns which thread it serves.
func (s *Stdio) UseCodex(onThread func(string)) {
	s.onThread = onThread
}

// SetInstructions replaces the instructions an OpenCode plugin pushes into the system prompt.
// The new text is sent only when it differs from the current text. Claude sessions have no
// refresh path, so this does nothing for them.
func (s *Stdio) SetInstructions(text string) {
	s.mu.Lock()
	if s.method != openCodeMessageMethod || text == s.instructions {
		s.mu.Unlock()
		return
	}
	s.instructions = text
	s.mu.Unlock()
	_ = s.emit(map[string]any{"jsonrpc": "2.0", "method": openCodeInstructionsMethod, "params": map[string]string{"text": text}})
}

// failDelivery ends the pending delivery when the plugin reports it could not inject it. A
// report for any other message is ignored, so a stale report cannot end a live delivery.
func (s *Stdio) failDelivery(id, reason string) {
	s.mu.Lock()
	if id == "" || id != s.pending || s.failed == nil {
		s.mu.Unlock()
		return
	}
	select {
	case s.failed <- reason:
	default:
	}
	onFailure := s.onFailure
	s.mu.Unlock()
	if onFailure != nil {
		onFailure(reason)
	}
}

func (s *Stdio) emit(frame any) error {
	s.writeMu.Lock()
	defer s.writeMu.Unlock()
	return json.NewEncoder(s.out).Encode(frame)
}

func (s *Stdio) Deliver(ctx context.Context, event Event) error {
	if !s.channel {
		return ErrRejected
	}
	text, err := event.Text()
	if err != nil {
		return err
	}
	s.mu.Lock()
	if s.pending != "" {
		s.mu.Unlock()
		return ErrBusy
	}
	ack := make(chan struct{})
	failed := make(chan string, 1)
	s.pending = event.ID
	s.ack = ack
	s.failed = failed
	s.mu.Unlock()
	defer func() { s.mu.Lock(); s.pending = ""; s.ack = nil; s.failed = nil; s.mu.Unlock() }()
	if err := s.emit(map[string]any{"jsonrpc": "2.0", "method": s.method, "params": map[string]any{"content": text, "meta": s.meta(event.ID, event.Sender, event.Recipient)}}); err != nil {
		return ErrUncertain
	}
	select {
	case <-ack:
		return nil
	case <-failed:
		// Not accepted: the Runner pauses and polls again, and the gateway keeps the message pending.
		return ErrUnavailable
	case <-ctx.Done():
		return ErrUncertain
	}
}

func (s *Stdio) acknowledge(id string) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	if id == "" || id != s.pending || s.ack == nil {
		return errors.New("message is not pending for this channel")
	}
	select {
	case <-s.ack:
	default:
		close(s.ack)
	}
	return nil
}

// Probe emits the startup channel event that asks the agent to confirm delivery. onAck runs
// once when that probe ID is acknowledged; the acknowledgement is handled here, never forwarded.
func (s *Stdio) Probe(id, identity string, onAck func()) error {
	if !s.channel {
		return ErrRejected
	}
	text := "Garden connected as " + identity + "; call acknowledge_delivery with message_id " + id + " to confirm."
	frame := map[string]any{"jsonrpc": "2.0", "method": s.method, "params": map[string]any{"content": text, "meta": s.meta(id, "garden", identity)}}
	s.mu.Lock()
	s.probeID = id
	s.probeFrame = frame
	s.onProbe = onAck
	s.mu.Unlock()
	return s.emit(frame)
}

// Reprobe emits the startup probe again while it is unacknowledged, because Claude may register
// its channel handler only after the first emit. It reports whether the probe was still pending.
func (s *Stdio) Reprobe() (bool, error) {
	s.mu.Lock()
	frame := s.probeFrame
	pending := s.probeID != ""
	s.mu.Unlock()
	if !pending {
		return false, nil
	}
	return true, s.emit(frame)
}

// meta is the notification metadata. Claude Code sets its own source attribute on the channel
// tag, so a Garden source key there would render twice; the OpenCode plugin relies on it.
func (s *Stdio) meta(id, sender, recipient string) map[string]string {
	meta := map[string]string{"message_id": id, "sender": sender, "recipient": recipient}
	if s.method != claudeChannelMethod {
		meta["source"] = "garden"
	}
	return meta
}

// acknowledgeProbe consumes the probe ID, if it matches, and runs its callback once. A repeated
// acknowledgement of the same probe, from a re-emitted copy, also succeeds.
func (s *Stdio) acknowledgeProbe(id string) bool {
	s.mu.Lock()
	if id != "" && id == s.probeDone {
		s.mu.Unlock()
		return true
	}
	if s.probeID == "" || id != s.probeID {
		s.mu.Unlock()
		return false
	}
	onAck := s.onProbe
	s.probeDone = id
	s.probeID, s.probeFrame, s.onProbe = "", nil, nil
	s.mu.Unlock()
	if onAck != nil {
		onAck()
	}
	return true
}

// learnThread reports the thread in a tools/call _meta object, if it is well formed.
func (s *Stdio) learnThread(meta json.RawMessage) {
	var m struct {
		ThreadID string `json:"threadId"`
	}
	if json.Unmarshal(meta, &m) == nil && safeSession(m.ThreadID) {
		s.onThread(m.ThreadID)
	}
}

type stdioRequest struct {
	JSONRPC string          `json:"jsonrpc"`
	ID      json.RawMessage `json:"id"`
	Method  string          `json:"method"`
	Params  json.RawMessage `json:"params"`
}

// Run owns the input until EOF or cancellation. onReady runs once after the
// client acknowledges initialisation. It should wait until ctx is cancelled.
func (s *Stdio) Run(ctx context.Context, in io.ReadCloser, onReady func(context.Context) error) error {
	ctx, cancel := context.WithCancel(ctx)
	defer func() { cancel(); _ = in.Close() }()
	frames := make(chan []byte)
	readError := make(chan error, 1)
	go func() {
		scan := bufio.NewScanner(in)
		scan.Buffer(make([]byte, 4096), 256*1024)
		for scan.Scan() {
			data := append([]byte(nil), scan.Bytes()...)
			select {
			case frames <- data:
			case <-ctx.Done():
				return
			}
		}
		readError <- scan.Err()
	}()
	stopClose := context.AfterFunc(ctx, func() { _ = in.Close() })
	defer stopClose()
	worker := make(chan error, 1)
	initialised, ready := false, false
	finish := func(cause error) error {
		cancel()
		_ = in.Close()
		if ready && onReady != nil {
			select {
			case err := <-worker:
				if err != nil {
					return err
				}
			case <-time.After(time.Second):
				return ErrUncertain
			}
		}
		return cause
	}
	for {
		select {
		case <-ctx.Done():
			return finish(nil)
		case err := <-readError:
			return finish(err)
		case err := <-worker:
			return err
		case data := <-frames:
			var req stdioRequest
			if json.Unmarshal(data, &req) != nil || req.JSONRPC != "2.0" {
				if err := s.rpcError(nil, -32700, "invalid JSON-RPC request"); err != nil {
					return err
				}
				continue
			}
			if len(req.ID) == 0 {
				switch {
				case req.Method == "notifications/initialized" && initialised && !ready:
					ready = true
					if onReady != nil {
						go func() { worker <- onReady(ctx) }()
					}
				case req.Method == openCodeSessionMethod && s.method == openCodeMessageMethod && s.onSession != nil:
					var p struct {
						SessionID string `json:"session_id"`
					}
					if json.Unmarshal(req.Params, &p) == nil && safeSession(p.SessionID) {
						s.onSession(p.SessionID)
					}
				case req.Method == openCodeFailureMethod && s.method == openCodeMessageMethod:
					var p struct {
						MessageID string `json:"message_id"`
						Reason    string `json:"reason"`
					}
					if json.Unmarshal(req.Params, &p) == nil {
						s.failDelivery(p.MessageID, p.Reason)
					}
				}
				continue
			}
			var result any
			switch req.Method {
			case "initialize":
				if initialised {
					if err := s.rpcError(req.ID, -32600, "already initialised"); err != nil {
						return err
					}
					continue
				}
				var p struct {
					ProtocolVersion string `json:"protocolVersion"`
				}
				if json.Unmarshal(req.Params, &p) != nil {
					if err := s.rpcError(req.ID, -32602, "invalid initialise parameters"); err != nil {
						return err
					}
					continue
				}
				switch p.ProtocolVersion {
				case "2024-11-05", "2025-03-26", "2025-06-18", "2025-11-25":
				default:
					p.ProtocolVersion = "2025-11-25"
				}
				caps := map[string]any{"tools": map[string]any{}}
				if s.method == claudeChannelMethod {
					caps["experimental"] = map[string]any{"claude/channel": map[string]any{}}
				}
				result = map[string]any{"protocolVersion": p.ProtocolVersion, "serverInfo": map[string]string{"name": "garden", "version": "1"}, "capabilities": caps, "instructions": s.instructions}
				initialised = true
			case "ping":
				result = map[string]any{}
			default:
				if !ready {
					if err := s.rpcError(req.ID, -32000, "initialise the connection first"); err != nil {
						return err
					}
					continue
				}
				switch req.Method {
				case "tools/list":
					tools, err := s.tools.List(ctx)
					if err != nil {
						if err := s.rpcError(req.ID, -32603, "Garden tool discovery failed"); err != nil {
							return err
						}
						continue
					}
					if s.channel {
						tools = append(tools, Tool{Name: "acknowledge_delivery", Description: "Confirm receipt of the pending Garden channel message, not completion of its request.", InputSchema: json.RawMessage(`{"type":"object","properties":{"message_id":{"type":"string"}},"required":["message_id"],"additionalProperties":false}`)})
					}
					result = map[string]any{"tools": tools}
				case "tools/call":
					var p struct {
						Name      string          `json:"name"`
						Arguments json.RawMessage `json:"arguments"`
						Meta      json.RawMessage `json:"_meta"`
					}
					if json.Unmarshal(req.Params, &p) != nil {
						if err := s.rpcError(req.ID, -32602, "invalid tool call"); err != nil {
							return err
						}
						continue
					}
					if s.onThread != nil {
						s.learnThread(p.Meta)
					}
					var value any
					var err error
					if p.Name == "acknowledge_delivery" && s.channel {
						var args struct {
							ID string `json:"message_id"`
						}
						err = json.Unmarshal(p.Arguments, &args)
						if err == nil && !s.acknowledgeProbe(args.ID) {
							err = s.acknowledge(args.ID)
						}
						value = map[string]bool{"acknowledged": err == nil}
					} else {
						value, err = s.tools.Call(ctx, p.Name, p.Arguments)
					}
					if err != nil {
						text := "Garden operation failed; delivery remains pending where applicable."
						var safe *SafeError
						if errors.As(err, &safe) {
							text = safe.Message
						}
						result = map[string]any{"isError": true, "content": []map[string]string{{"type": "text", "text": text}}}
					} else {
						encoded, marshalErr := json.Marshal(value)
						if marshalErr != nil {
							return marshalErr
						}
						result = map[string]any{"content": []map[string]string{{"type": "text", "text": string(encoded)}}, "structuredContent": value}
					}
				default:
					if err := s.rpcError(req.ID, -32601, "method not found"); err != nil {
						return err
					}
					continue
				}
			}
			if err := s.emit(map[string]any{"jsonrpc": "2.0", "id": req.ID, "result": result}); err != nil {
				return err
			}
		}
	}
}

func (s *Stdio) rpcError(id json.RawMessage, code int, message string) error {
	return s.emit(map[string]any{"jsonrpc": "2.0", "id": id, "error": map[string]any{"code": code, "message": message}})
}
