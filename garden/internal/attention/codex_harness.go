package attention

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"os"
	"os/exec"
	"strconv"
	"strings"
	"sync"
	"time"
)

const (
	// codexBinary is looked up on PATH, as the Claude and OpenCode plugins look up garden.
	codexBinary = "codex"
	// codexQueueTimeout bounds one `codex queue`. A call that is killed after this may have queued
	// the message, so its outcome is treated as uncertain.
	codexQueueTimeout = 20 * time.Second
	// codexMaxMessage keeps the message inside the argv limit of one process argument.
	codexMaxMessage = 96 * 1024
	// codexBackoffFirst and codexBackoffMax bound the wait after a definite queue failure.
	codexBackoffFirst = 2 * time.Second
	codexBackoffMax   = 60 * time.Second
)

// codexDaemonReason is the status reason in Codex's shared app-server mode.
const codexDaemonReason = "Codex is running on its shared background server, which cannot pair Garden with one session; start Codex with a -c option, for example codex -c plugins.\"garden@garden-local\".enabled=true, as the Garden Codex plugin README describes"

// codexEmbeddedParent reports whether this core was started by a Codex TUI launched with a -c
// option (embedded mode), rather than by the shared app-server daemon. The daemon's MCP servers
// get the daemon's environment and cannot be paired with a session, so the core takes no claim
// there. Unreadable parent argv counts as daemon mode, so an unknown parent never connects.
func codexEmbeddedParent() bool {
	raw, err := os.ReadFile("/proc/" + strconv.Itoa(os.Getppid()) + "/cmdline")
	if err != nil || len(raw) == 0 {
		return false
	}
	argv := strings.Split(strings.TrimSuffix(string(raw), "\x00"), "\x00")
	return !codexDaemonArgv(argv)
}

// codexDaemonArgv reports whether a parent's argv names the shared app-server daemon.
func codexDaemonArgv(argv []string) bool {
	for _, arg := range argv {
		if arg == "app-server" {
			return true
		}
	}
	return strings.Contains(argv[0], "app-server-daemon")
}

// CodexQueue delivers Garden messages into one embedded Codex thread with `codex queue`. The core
// acknowledges a message only after the queue call succeeds. Codex offers no idempotency key on
// queue, so a retry after an uncertain outcome is never made: that outcome ends delivery instead.
// See the Codex plugin README for the residual risk.
type CodexQueue struct {
	remote  *Remote
	binary  string
	timeout time.Duration

	mu     sync.Mutex
	thread string
	fails  int
}

// learn records the thread the first time one is reported. A later, different thread is refused.
func (q *CodexQueue) learn(thread string) bool {
	q.mu.Lock()
	defer q.mu.Unlock()
	if q.thread != "" || !safeSession(thread) {
		return false
	}
	q.thread = thread
	return true
}

// Deliver queues the message into the learnt thread. Definite failures return RetryAfter with a
// bounded backoff, so the Runner retries the same unacknowledged message after that wait. A call
// that was killed, timed out or cancelled may have queued the message, so it returns ErrUncertain.
func (q *CodexQueue) Deliver(ctx context.Context, event Event) error {
	text, err := event.Text()
	if err != nil {
		return err
	}
	if len(text) > codexMaxMessage {
		// Rejecting would leave the message pending and stop every later session at it, so a
		// notice is queued and acknowledged instead; the full text stays in Garden history.
		if text, err = codexOmitted(event, len(text)); err != nil {
			return err
		}
	}
	q.mu.Lock()
	thread := q.thread
	q.mu.Unlock()
	if thread == "" {
		return ErrUnavailable
	}
	timeout := q.timeout
	if timeout <= 0 {
		timeout = codexQueueTimeout
	}
	callCtx, cancel := context.WithTimeout(ctx, timeout)
	defer cancel()
	binary := q.binary
	if binary == "" {
		binary = codexBinary
	}
	cmd := exec.CommandContext(callCtx, binary, "queue", "--thread", thread, "--message", text)
	// Output may echo the message, so it is discarded rather than reported.
	var discard bytes.Buffer
	cmd.Stdout, cmd.Stderr = &discard, &discard
	err = cmd.Run()
	if err == nil {
		q.succeeded()
		return nil
	}
	if ctx.Err() != nil || callCtx.Err() != nil {
		return ErrUncertain
	}
	reason := "codex queue could not start"
	var exit *exec.ExitError
	if errors.As(err, &exit) {
		// A child killed by a signal or crashing (ExitCode -1) may already have queued the
		// message, so its outcome is uncertain and it is not retried automatically.
		if exit.ExitCode() < 0 {
			return ErrUncertain
		}
		reason = "codex queue exited with status " + strconv.Itoa(exit.ExitCode())
	}
	// The Runner keeps the message unacknowledged and retries it after this wait.
	return RetryAfter(q.failed(reason))
}

// codexOmitted is the marked notice queued in place of an event too large for one process
// argument. It keeps the event's provenance and gives the read_messages arguments that return it.
func codexOmitted(event Event, size int) (string, error) {
	var message struct {
		Sequence uint64 `json:"sequence"`
	}
	_ = json.Unmarshal(event.Payload, &message)
	read := map[string]any{"limit": 1}
	if message.Sequence > 1 {
		read["after_seq"] = message.Sequence - 1
		read["generation"] = event.Generation
	}
	notice, err := json.Marshal(map[string]any{
		"omitted":       "message too large to queue into Codex; read it with read_messages and these arguments",
		"bytes":         size,
		"sequence":      message.Sequence,
		"read_messages": read,
	})
	if err != nil {
		return "", err
	}
	event.Payload = notice
	return event.Text()
}

func (q *CodexQueue) succeeded() {
	q.mu.Lock()
	q.fails = 0
	q.mu.Unlock()
	if q.remote != nil {
		q.remote.noteDelivery("")
	}
}

// failed records a definite failure and returns the bounded backoff before the next attempt.
func (q *CodexQueue) failed(reason string) time.Duration {
	q.mu.Lock()
	q.fails++
	wait := codexBackoffFirst << min(q.fails-1, 10)
	if wait > codexBackoffMax || wait <= 0 {
		wait = codexBackoffMax
	}
	q.mu.Unlock()
	if q.remote != nil {
		q.remote.noteDelivery(reason)
	}
	return wait
}

// CodexQueue is the Host the Runner delivers through for a Codex session.
var _ Host = (*CodexQueue)(nil)
