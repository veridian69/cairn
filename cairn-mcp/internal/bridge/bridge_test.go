package bridge

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"io"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/veridian69/cairn/cairn-mcp/internal/upstream"
)

const testTimeout = time.Second

type fakeRemote struct {
	events chan upstream.Event
	sends  chan json.RawMessage

	sendFn   func(context.Context, json.RawMessage) error
	closeFn  func(context.Context) error
	closeErr error

	mu           sync.Mutex
	activeSends  int
	maxSends     int
	closeCalls   int
	closeOverlap bool
}

type blockingWriter struct {
	started chan struct{}
	release chan struct{}
	once    sync.Once
}

func (w *blockingWriter) Write(payload []byte) (int, error) {
	w.once.Do(func() { close(w.started) })
	<-w.release
	return len(payload), nil
}

func newFakeRemote() *fakeRemote {
	return &fakeRemote{
		events: make(chan upstream.Event, 8),
		sends:  make(chan json.RawMessage, 64),
	}
}

func (f *fakeRemote) Send(ctx context.Context, msg json.RawMessage) error {
	f.mu.Lock()
	f.activeSends++
	if f.activeSends > f.maxSends {
		f.maxSends = f.activeSends
	}
	f.mu.Unlock()
	defer func() {
		f.mu.Lock()
		f.activeSends--
		f.mu.Unlock()
	}()

	copy := bytes.Clone(msg)
	f.sends <- copy
	if f.sendFn != nil {
		return f.sendFn(ctx, copy)
	}
	return nil
}

func (f *fakeRemote) Events() <-chan upstream.Event { return f.events }

func (f *fakeRemote) Close(ctx context.Context) error {
	f.mu.Lock()
	f.closeCalls++
	f.closeOverlap = f.closeOverlap || f.activeSends != 0
	closeFn := f.closeFn
	err := f.closeErr
	f.mu.Unlock()
	if closeFn != nil {
		return closeFn(ctx)
	}
	close(f.events)
	return err
}

func (f *fakeRemote) CloseCalls() int {
	f.mu.Lock()
	defer f.mu.Unlock()
	return f.closeCalls
}

func (f *fakeRemote) CloseOverlappedSend() bool {
	f.mu.Lock()
	defer f.mu.Unlock()
	return f.closeOverlap
}

func (f *fakeRemote) MaxActiveSends() int {
	f.mu.Lock()
	defer f.mu.Unlock()
	return f.maxSends
}

func (f *fakeRemote) waitForSends(t *testing.T, count int) {
	t.Helper()
	for i := 0; i < count; i++ {
		select {
		case <-f.sends:
		case <-time.After(testTimeout):
			t.Fatalf("received %d of %d sends", i, count)
		}
	}
}

func TestRunCleanEOFFlushesConcurrentSendEventsBeforeClose(t *testing.T) {
	inReader, inWriter := io.Pipe()
	remote := newFakeRemote()
	release := make(chan struct{})
	remote.sendFn = func(ctx context.Context, msg json.RawMessage) error {
		select {
		case <-release:
		case <-ctx.Done():
			return ctx.Err()
		}
		var response json.RawMessage
		switch string(msg) {
		case `{"jsonrpc":"2.0","id":1}`:
			response = json.RawMessage(`{"jsonrpc":"2.0","id":1,"result":{}}`)
		case `{"jsonrpc":"2.0","id":2}`:
			response = json.RawMessage(`{"jsonrpc":"2.0","id":2,"result":{}}`)
		default:
			return errors.New("unexpected request")
		}
		select {
		case remote.events <- upstream.Event{Message: response}:
			return nil
		case <-ctx.Done():
			return ctx.Err()
		}
	}

	var stdout bytes.Buffer
	done := make(chan error, 1)
	go func() { done <- Run(context.Background(), inReader, &stdout, remote) }()

	if _, err := io.WriteString(inWriter, "{\"jsonrpc\":\"2.0\",\"id\":1}\n{\"jsonrpc\":\"2.0\",\"id\":2}\n"); err != nil {
		t.Fatal(err)
	}
	remote.waitForSends(t, 2)
	if err := inWriter.Close(); err != nil {
		t.Fatal(err)
	}
	close(release)

	if err := waitForRun(t, done); err != nil {
		t.Fatal(err)
	}
	if remote.CloseCalls() != 1 {
		t.Fatalf("Close calls = %d, want 1", remote.CloseCalls())
	}
	if remote.CloseOverlappedSend() {
		t.Fatal("Close overlapped an active Send")
	}

	lines := bytes.Split(bytes.TrimSpace(stdout.Bytes()), []byte{'\n'})
	if len(lines) != 2 {
		t.Fatalf("stdout lines = %q, want 2 responses", stdout.String())
	}
	seen := make(map[int]bool)
	for _, line := range lines {
		var response struct {
			ID int `json:"id"`
		}
		if err := json.Unmarshal(line, &response); err != nil {
			t.Fatalf("decode stdout line %q: %v", line, err)
		}
		seen[response.ID] = true
	}
	if !seen[1] || !seen[2] {
		t.Fatalf("response IDs = %v, want 1 and 2", seen)
	}
}

func TestRunCleanEOFDrainTimeoutCancelsAndJoinsSendBeforeClose(t *testing.T) {
	remote := newFakeRemote()
	sendCanceled := make(chan struct{})
	remote.sendFn = func(ctx context.Context, _ json.RawMessage) error {
		<-ctx.Done()
		close(sendCanceled)
		return nil
	}
	done := make(chan error, 1)
	go func() {
		done <- runWithCleanEOFDrainTimeout(
			context.Background(),
			io.NopCloser(strings.NewReader("{\"jsonrpc\":\"2.0\",\"id\":1}\n")),
			io.Discard,
			remote,
			10*time.Millisecond,
		)
	}()

	remote.waitForSends(t, 1)
	if err := waitForRun(t, done); err != nil {
		t.Fatal(err)
	}
	assertClosed(t, sendCanceled, "clean-EOF timeout did not cancel the blocked Send")
	if remote.CloseOverlappedSend() {
		t.Fatal("Close overlapped an active Send")
	}
	if remote.CloseCalls() != 1 {
		t.Fatalf("Close calls = %d, want 1", remote.CloseCalls())
	}
}

func TestRunObservesEOFAfterAllSendSlotsAreOccupied(t *testing.T) {
	// This catches acquiring a send permit before reading the next frame: with
	// all permits occupied, that ordering prevents the bridge from observing EOF.
	inReader, inWriter := io.Pipe()
	remote := newFakeRemote()
	sendCanceled := make(chan struct{}, maxActiveSends)
	remote.sendFn = func(ctx context.Context, _ json.RawMessage) error {
		<-ctx.Done()
		sendCanceled <- struct{}{}
		return nil
	}
	done := make(chan error, 1)
	go func() {
		done <- runWithCleanEOFDrainTimeout(
			context.Background(),
			inReader,
			io.Discard,
			remote,
			10*time.Millisecond,
		)
	}()

	for i := 0; i < maxActiveSends; i++ {
		if _, err := io.WriteString(inWriter, "{\"jsonrpc\":\"2.0\",\"id\":1}\n"); err != nil {
			t.Fatal(err)
		}
	}
	remote.waitForSends(t, maxActiveSends)
	if err := inWriter.Close(); err != nil {
		t.Fatal(err)
	}

	if err := waitForRun(t, done); err != nil {
		t.Fatal(err)
	}
	if got := len(sendCanceled); got != maxActiveSends {
		t.Fatalf("canceled Sends = %d, want %d", got, maxActiveSends)
	}
	if got := remote.MaxActiveSends(); got != maxActiveSends {
		t.Fatalf("maximum active Sends = %d, want %d", got, maxActiveSends)
	}
	if remote.CloseOverlappedSend() {
		t.Fatal("Close overlapped an active Send")
	}
	if remote.CloseCalls() != 1 {
		t.Fatalf("Close calls = %d, want 1", remote.CloseCalls())
	}
}

func TestRunLimitsActiveSendsTo32AndBackpressuresInput(t *testing.T) {
	input := newGatedSendInput(34, 33)
	remote := newFakeRemote()
	release := make(chan struct{}, 1)
	remote.sendFn = func(ctx context.Context, _ json.RawMessage) error {
		select {
		case <-release:
			return nil
		case <-ctx.Done():
			return ctx.Err()
		}
	}
	done := make(chan error, 1)
	go func() { done <- Run(context.Background(), input, io.Discard, remote) }()

	remote.waitForSends(t, 32)
	waitForSignal(t, input.readAhead, "frame 33 was not read ahead")
	assertNotClosed(t, input.gatedRead, "frame 34 was requested before a Send slot was released")
	release <- struct{}{}
	waitForSignal(t, input.gatedRead, "frame 34 was not requested after a Send slot was released")
	input.AllowGatedRead()
	close(release)
	if err := waitForRun(t, done); err != nil {
		t.Fatal(err)
	}
	if got := remote.MaxActiveSends(); got != 32 {
		t.Fatalf("maximum active Sends = %d, want 32", got)
	}
	if remote.CloseOverlappedSend() {
		t.Fatal("Close overlapped an active Send")
	}
}

func TestRunCancellationUnblocksSendLimitAcquisition(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	input := newGatedSendInput(33, -1)
	remote := newFakeRemote()
	remote.sendFn = func(ctx context.Context, _ json.RawMessage) error {
		<-ctx.Done()
		return ctx.Err()
	}
	done := make(chan error, 1)
	go func() { done <- Run(ctx, input, io.Discard, remote) }()

	remote.waitForSends(t, 32)
	cancel()
	if err := waitForRun(t, done); err != nil {
		t.Fatalf("Run error = %v, want nil", err)
	}
	if !input.WasClosed() {
		t.Fatal("input was not closed after cancellation")
	}
	if remote.CloseOverlappedSend() {
		t.Fatal("Close overlapped an active Send")
	}
}

func TestRunSendErrorUnblocksInputAndReturnsError(t *testing.T) {
	wantErr := errors.New("send failed")
	inReader, inWriter := io.Pipe()
	remote := newFakeRemote()
	remote.sendFn = func(context.Context, json.RawMessage) error { return wantErr }
	done := make(chan error, 1)
	go func() { done <- Run(context.Background(), inReader, io.Discard, remote) }()

	if _, err := io.WriteString(inWriter, "{\"jsonrpc\":\"2.0\",\"id\":1}\n"); err != nil {
		t.Fatal(err)
	}
	if err := waitForRun(t, done); !errors.Is(err, wantErr) {
		t.Fatalf("Run error = %v, want %v", err, wantErr)
	}
	if remote.CloseCalls() != 1 {
		t.Fatalf("Close calls = %d, want 1", remote.CloseCalls())
	}
	if _, err := inWriter.Write([]byte("x")); err == nil {
		t.Fatal("stdin writer remained usable after terminal send error")
	}
}

func TestRunTerminalEventUnblocksBlockedInput(t *testing.T) {
	wantErr := errors.New("upstream listener failed")
	input := newBlockingReadCloser()
	remote := newFakeRemote()
	done := make(chan error, 1)
	go func() { done <- Run(context.Background(), input, io.Discard, remote) }()

	remote.events <- upstream.Event{Err: wantErr}
	if err := waitForRun(t, done); !errors.Is(err, wantErr) {
		t.Fatalf("Run error = %v, want %v", err, wantErr)
	}
	if !input.WasClosed() {
		t.Fatal("blocked stdin was not closed")
	}
	if remote.CloseCalls() != 1 {
		t.Fatalf("Close calls = %d, want 1", remote.CloseCalls())
	}
}

func TestRunTerminalEventWaitsForBlockedSendBeforeClose(t *testing.T) {
	wantErr := errors.New("upstream listener failed")
	inReader, inWriter := io.Pipe()
	remote := newFakeRemote()
	sendExited := make(chan struct{})
	remote.sendFn = func(ctx context.Context, _ json.RawMessage) error {
		<-ctx.Done()
		close(sendExited)
		return ctx.Err()
	}
	done := make(chan error, 1)
	go func() { done <- Run(context.Background(), inReader, io.Discard, remote) }()

	if _, err := io.WriteString(inWriter, "{\"jsonrpc\":\"2.0\",\"id\":1}\n"); err != nil {
		t.Fatal(err)
	}
	remote.waitForSends(t, 1)
	remote.events <- upstream.Event{Err: wantErr}

	if err := waitForRun(t, done); !errors.Is(err, wantErr) {
		t.Fatalf("Run error = %v, want %v", err, wantErr)
	}
	assertClosed(t, sendExited, "blocked Send did not join")
	if remote.CloseOverlappedSend() {
		t.Fatal("Close overlapped an active Send")
	}
	if remote.CloseCalls() != 1 {
		t.Fatalf("Close calls = %d, want 1", remote.CloseCalls())
	}
}

func TestRunCallerCancellationWaitsForBlockedSendBeforeClose(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	inReader, inWriter := io.Pipe()
	remote := newFakeRemote()
	sendExited := make(chan struct{})
	remote.sendFn = func(ctx context.Context, _ json.RawMessage) error {
		<-ctx.Done()
		close(sendExited)
		return ctx.Err()
	}
	done := make(chan error, 1)
	go func() { done <- Run(ctx, inReader, io.Discard, remote) }()

	if _, err := io.WriteString(inWriter, "{\"jsonrpc\":\"2.0\",\"id\":1}\n"); err != nil {
		t.Fatal(err)
	}
	remote.waitForSends(t, 1)
	cancel()
	if err := waitForRun(t, done); err != nil {
		t.Fatalf("Run error = %v, want nil", err)
	}
	assertClosed(t, sendExited, "blocked Send did not join")
	if remote.CloseOverlappedSend() {
		t.Fatal("Close overlapped an active Send")
	}
	if remote.CloseCalls() != 1 {
		t.Fatalf("Close calls = %d, want 1", remote.CloseCalls())
	}
	if _, err := inWriter.Write([]byte("x")); err == nil {
		t.Fatal("stdin writer remained usable after caller cancellation")
	}
}

func TestRunCallerCancellationBoundsBlockedOutputCleanup(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	input := newBlockingReadCloser()
	remote := newFakeRemote()
	output := &blockingWriter{started: make(chan struct{}), release: make(chan struct{})}
	defer close(output.release)
	done := make(chan error, 1)
	go func() {
		done <- runWithTimeouts(ctx, input, output, remote, 10*time.Millisecond, 10*time.Millisecond)
	}()

	remote.events <- upstream.Event{Message: json.RawMessage(`{"jsonrpc":"2.0","id":1,"result":{}}`)}
	waitForSignal(t, output.started, "stdout write did not start")
	cancel()
	if err := waitForRun(t, done); err != nil {
		t.Fatalf("Run error = %v, want nil", err)
	}
	if !input.WasClosed() {
		t.Fatal("blocked stdin was not closed")
	}
	if remote.CloseCalls() != 1 {
		t.Fatalf("Close calls = %d, want 1", remote.CloseCalls())
	}
}

func TestRunDrainsFinalFrameAfterCloseSpendsTheCleanupBudget(t *testing.T) {
	// An unreachable upstream makes the DELETE in Close burn the whole cleanup
	// budget; the stdout drain that follows must still get time of its own.
	remote := newFakeRemote()
	closeReturned := make(chan struct{})
	remote.closeFn = func(ctx context.Context) error {
		<-ctx.Done()
		close(remote.events)
		close(closeReturned)
		return nil
	}
	output := &gatedFrameWriter{
		gate:    closeReturned,
		delay:   50 * time.Millisecond,
		started: make(chan struct{}),
	}
	ctx, cancel := context.WithCancel(context.Background())
	input := newBlockingReadCloser()
	done := make(chan error, 1)
	go func() {
		done <- runWithTimeouts(ctx, input, output, remote, 10*time.Millisecond, 200*time.Millisecond)
	}()

	remote.events <- upstream.Event{Message: json.RawMessage(`{"jsonrpc":"2.0","id":1,"result":{}}`)}
	waitForSignal(t, output.started, "stdout write did not start")
	remote.events <- upstream.Event{Message: json.RawMessage(`{"jsonrpc":"2.0","id":2,"result":{}}`)}
	cancel()

	if err := waitForRun(t, done); err != nil {
		t.Fatalf("Run error = %v, want nil", err)
	}
	if frames := output.Frames(); len(frames) != 2 {
		t.Fatalf("stdout frames written before Run returned = %q, want both buffered frames", frames)
	}
	if remote.CloseCalls() != 1 {
		t.Fatalf("Close calls = %d, want 1", remote.CloseCalls())
	}
}

func TestRunPreCancelledContextOverridesCleanCloseError(t *testing.T) {
	wantCloseErr := errors.New("cleanup DELETE failed")
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	remote := newFakeRemote()
	remote.closeErr = wantCloseErr

	err := Run(ctx, io.NopCloser(strings.NewReader("")), io.Discard, remote)
	if err != nil {
		t.Fatalf("Run error = %v, want nil", err)
	}
	if remote.CloseCalls() != 1 {
		t.Fatalf("Close calls = %d, want 1", remote.CloseCalls())
	}
}

func TestRunReturnsMalformedInputError(t *testing.T) {
	remote := newFakeRemote()
	err := Run(context.Background(), io.NopCloser(strings.NewReader("not-json\n")), io.Discard, remote)
	if err == nil || err.Error() != "invalid JSON-RPC line on stdin" {
		t.Fatalf("Run error = %v, want malformed-input error", err)
	}
	if remote.CloseCalls() != 1 {
		t.Fatalf("Close calls = %d, want 1", remote.CloseCalls())
	}
}

func TestRunReturnsStdoutWriteErrorAndUnblocksInput(t *testing.T) {
	wantErr := errors.New("stdout closed")
	input := newBlockingReadCloser()
	remote := newFakeRemote()
	done := make(chan error, 1)
	go func() { done <- Run(context.Background(), input, errorWriter{err: wantErr}, remote) }()

	remote.events <- upstream.Event{Message: json.RawMessage(`{"jsonrpc":"2.0","id":1,"result":{}}`)}
	if err := waitForRun(t, done); !errors.Is(err, wantErr) {
		t.Fatalf("Run error = %v, want %v", err, wantErr)
	}
	if !input.WasClosed() {
		t.Fatal("blocked stdin was not closed")
	}
}

func TestRunReturnsCleanCloseErrorWithoutWritingStdout(t *testing.T) {
	wantErr := errors.New("cleanup DELETE failed")
	remote := newFakeRemote()
	remote.closeErr = wantErr
	var stdout bytes.Buffer

	err := Run(context.Background(), io.NopCloser(strings.NewReader("")), &stdout, remote)
	if !errors.Is(err, wantErr) {
		t.Fatalf("Run error = %v, want %v", err, wantErr)
	}
	if stdout.Len() != 0 {
		t.Fatalf("stdout = %q, want no protocol bytes", stdout.String())
	}
	if remote.CloseCalls() != 1 {
		t.Fatalf("Close calls = %d, want 1", remote.CloseCalls())
	}
}

func waitForRun(t *testing.T, done <-chan error) error {
	t.Helper()
	select {
	case err := <-done:
		return err
	case <-time.After(testTimeout):
		t.Fatal("Run did not terminate")
		return nil
	}
}

func assertClosed(t *testing.T, channel <-chan struct{}, message string) {
	t.Helper()
	select {
	case <-channel:
	default:
		t.Fatal(message)
	}
}

func assertNotClosed(t *testing.T, channel <-chan struct{}, message string) {
	t.Helper()
	select {
	case <-channel:
		t.Fatal(message)
	default:
	}
}

func waitForSignal(t *testing.T, channel <-chan struct{}, message string) {
	t.Helper()
	select {
	case <-channel:
	case <-time.After(testTimeout):
		t.Fatal(message)
	}
}

type blockingReadCloser struct {
	closed chan struct{}
	once   sync.Once
}

type gatedSendInput struct {
	total         int
	gateAt        int
	next          int
	readAhead     chan struct{}
	gatedRead     chan struct{}
	allowRead     chan struct{}
	closed        chan struct{}
	readAheadOnce sync.Once
	gatedReadOnce sync.Once
	allowOnce     sync.Once
	closeOnce     sync.Once
}

func newGatedSendInput(total, gateAt int) *gatedSendInput {
	return &gatedSendInput{
		total:     total,
		gateAt:    gateAt,
		readAhead: make(chan struct{}),
		gatedRead: make(chan struct{}),
		allowRead: make(chan struct{}),
		closed:    make(chan struct{}),
	}
}

func (r *gatedSendInput) Read(dst []byte) (int, error) {
	select {
	case <-r.closed:
		return 0, io.ErrClosedPipe
	default:
	}
	if r.next == r.gateAt {
		r.gatedReadOnce.Do(func() { close(r.gatedRead) })
		select {
		case <-r.allowRead:
		case <-r.closed:
			return 0, io.ErrClosedPipe
		}
	}
	if r.next == r.total {
		return 0, io.EOF
	}
	message := []byte(`{"jsonrpc":"2.0","id":1}` + "\n")
	n := copy(dst, message)
	r.next++
	if r.next == r.gateAt {
		r.readAheadOnce.Do(func() { close(r.readAhead) })
	}
	return n, nil
}

func (r *gatedSendInput) Close() error {
	r.closeOnce.Do(func() { close(r.closed) })
	return nil
}

func (r *gatedSendInput) AllowGatedRead() {
	r.allowOnce.Do(func() { close(r.allowRead) })
}

func (r *gatedSendInput) WasClosed() bool {
	select {
	case <-r.closed:
		return true
	default:
		return false
	}
}

func newBlockingReadCloser() *blockingReadCloser {
	return &blockingReadCloser{closed: make(chan struct{})}
}

func (r *blockingReadCloser) Read([]byte) (int, error) {
	<-r.closed
	return 0, errors.New("input closed")
}

func (r *blockingReadCloser) Close() error {
	r.once.Do(func() { close(r.closed) })
	return nil
}

func (r *blockingReadCloser) WasClosed() bool {
	select {
	case <-r.closed:
		return true
	default:
		return false
	}
}

// gatedFrameWriter models a stdout consumer whose first write only completes
// some time after gate is closed. Frames are recorded once written, so a
// caller can tell what actually reached stdout at a given moment.
type gatedFrameWriter struct {
	gate    <-chan struct{}
	delay   time.Duration
	started chan struct{}
	gated   bool

	mu     sync.Mutex
	frames []string
}

func (w *gatedFrameWriter) Write(payload []byte) (int, error) {
	if !w.gated {
		w.gated = true
		close(w.started)
		<-w.gate
		time.Sleep(w.delay)
	}
	w.mu.Lock()
	w.frames = append(w.frames, string(payload))
	w.mu.Unlock()
	return len(payload), nil
}

func (w *gatedFrameWriter) Frames() []string {
	w.mu.Lock()
	defer w.mu.Unlock()
	return append([]string(nil), w.frames...)
}

type errorWriter struct {
	err error
}

func (w errorWriter) Write([]byte) (int, error) { return 0, w.err }
