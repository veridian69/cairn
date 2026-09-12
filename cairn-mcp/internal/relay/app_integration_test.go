package relay

import (
	"bufio"
	"bytes"
	"compress/gzip"
	"context"
	"encoding/json"
	"errors"
	"io"
	"log"
	"net"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"github.com/veridian69/cairn/cairn-mcp/internal/config"
	"github.com/veridian69/cairn/cairn-mcp/internal/credentials"
)

type capturedRequest struct {
	method  string
	body    []byte
	headers map[string][]string
}

type failRoundTripper struct{}

func (f failRoundTripper) RoundTrip(*http.Request) (*http.Response, error) {
	return nil, errors.New("forced upstream failure")
}

func TestNewHTTPClientWithoutDeadline(t *testing.T) {
	client := NewHTTPClient(config.RelayConfig{
		ConnectTimeout: time.Second,
		ReadTimeout:    2 * time.Second,
		PoolTimeout:    3 * time.Second,
	}, 0)
	if client.Timeout != 0 {
		t.Fatalf("client timeout %s, want no deadline", client.Timeout)
	}
}

func TestRelayAppWritesDiagnosticsToInjectedLogger(t *testing.T) {
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusOK)
	}))
	defer upstream.Close()

	var diagnostics, global bytes.Buffer
	defaultWriter := log.Writer()
	log.SetOutput(&global)
	t.Cleanup(func() { log.SetOutput(defaultWriter) })
	app, server := newRelayTestServerWithLogger(t, upstream.URL, "relay-token", "client-id", "client-secret", nil, log.New(&diagnostics, "", 0))
	defer app.Close()
	defer server.Close()

	request, err := http.NewRequest(http.MethodGet, server.URL+"/mcp", nil)
	if err != nil {
		t.Fatalf("http.NewRequest() unexpected error: %v", err)
	}
	request.Header.Set("Authorization", "Bearer relay-token")
	response, err := http.DefaultClient.Do(request)
	if err != nil {
		t.Fatalf("http client unexpected error: %v", err)
	}
	response.Body.Close()

	if !strings.Contains(diagnostics.String(), "method=GET route=/mcp status_class=2") {
		t.Fatalf("diagnostics %q, want request log", diagnostics.String())
	}
	if global.Len() != 0 {
		t.Fatalf("global logger received %q", global.String())
	}
}

func TestRelayAppForwardsRequestAndInjectsHeaders(t *testing.T) {
	upstreamCalls := make(chan capturedRequest, 1)
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		body, err := io.ReadAll(r.Body)
		if err != nil {
			t.Fatalf("ReadAll(upstream body) unexpected error: %v", err)
		}
		upstreamCalls <- capturedRequest{
			method:  r.Method,
			body:    body,
			headers: cloneHeaders(r.Header),
		}

		w.Header().Set("Content-Type", "application/json")
		w.Header().Set("X-Ignored", "ignore")
		if _, err := w.Write([]byte(`{"ok":true}`)); err != nil {
			t.Fatalf("Write(upstream response) unexpected error: %v", err)
		}
	}))
	defer upstream.Close()

	app, server := newRelayTestServer(t, upstream.URL, "relay-token", "client-id", "client-secret", nil)
	defer app.Close()
	defer server.Close()

	request, err := http.NewRequest(http.MethodPost, server.URL+"/mcp", bytes.NewReader([]byte(`{"jsonrpc":"2.0"}`)))
	if err != nil {
		t.Fatalf("http.NewRequest() unexpected error: %v", err)
	}
	request.Header.Set("Authorization", "Bearer relay-token")
	request.Header.Set("MCP-Protocol-Version", "2025-03-26")
	request.Header.Set("Content-Type", "application/json")
	request.Header.Set("X-Ignored", "skip")

	response, err := http.DefaultClient.Do(request.WithContext(context.Background()))
	if err != nil {
		t.Fatalf("http client unexpected error: %v", err)
	}
	defer response.Body.Close()

	if response.StatusCode != http.StatusOK {
		t.Fatalf("status %d, want %d", response.StatusCode, http.StatusOK)
	}
	payload, _ := io.ReadAll(response.Body)
	if string(payload) != `{"ok":true}` {
		t.Fatalf("payload %q", string(payload))
	}
	if response.Header.Get("X-Ignored") != "" {
		t.Fatalf("unexpected upstream response header: %q", response.Header.Get("X-Ignored"))
	}
	if response.Header.Get("Content-Type") != "application/json" {
		t.Fatalf("content-type %q, want application/json", response.Header.Get("Content-Type"))
	}

	captured := readCapturedRequest(t, upstreamCalls)
	if captured.method != http.MethodPost {
		t.Fatalf("upstream method %q, want %q", captured.method, http.MethodPost)
	}
	if string(captured.body) != `{"jsonrpc":"2.0"}` {
		t.Fatalf("upstream body %q", string(captured.body))
	}
	headerVersion := http.Header(captured.headers).Get("MCP-Protocol-Version")
	if headerVersion != "2025-03-26" {
		t.Fatalf("upstream MCP-Protocol-Version %q", headerVersion)
	}
	if got := http.Header(captured.headers).Get("CF-Access-Client-Id"); got != "client-id" {
		t.Fatalf("CF id header %q", got)
	}
	if got := http.Header(captured.headers).Get("CF-Access-Client-Secret"); got != "client-secret" {
		t.Fatalf("CF secret header %q", got)
	}
	if _, ok := captured.headers["X-Ignored"]; ok {
		t.Fatalf("unexpected header X-Ignored forwarded upstream")
	}
}

// The relay must not reshape a compressed upstream response: whatever bytes
// and headers Cairn sends for Content-Encoding are what the downstream client
// must see. Go's default http.Transport silently undoes that by negotiating
// its own Accept-Encoding and transparently decompressing a gzip response,
// which is exactly the defect this test reproduces end to end through the
// relay's real HTTP client rather than by asserting a struct field.
func TestRelayAppPreservesGzipCompressedResponseBytes(t *testing.T) {
	plaintext := []byte(strings.Repeat("cairn relay byte-preservation ", 64))
	var compressed bytes.Buffer
	gz := gzip.NewWriter(&compressed)
	if _, err := gz.Write(plaintext); err != nil {
		t.Fatalf("gzip.Write() unexpected error: %v", err)
	}
	if err := gz.Close(); err != nil {
		t.Fatalf("gzip.Close() unexpected error: %v", err)
	}
	gzipBytes := compressed.Bytes()

	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		w.Header().Set("Content-Encoding", "gzip")
		if _, err := w.Write(gzipBytes); err != nil {
			t.Fatalf("upstream write: %v", err)
		}
	}))
	defer upstream.Close()

	app, server := newRelayTestServer(t, upstream.URL, "relay-token", "client-id", "client-secret", nil)
	defer app.Close()
	defer server.Close()

	request, err := http.NewRequest(http.MethodGet, server.URL+"/mcp", nil)
	if err != nil {
		t.Fatalf("http.NewRequest() unexpected error: %v", err)
	}
	request.Header.Set("Authorization", "Bearer relay-token")
	// Setting Accept-Encoding explicitly stops the *test's own* transport from
	// managing compression on the downstream leg, so the assertions below see
	// exactly what the relay wrote rather than what net/http decoded for us.
	request.Header.Set("Accept-Encoding", "gzip")

	response, err := http.DefaultClient.Do(request)
	if err != nil {
		t.Fatalf("http client unexpected error: %v", err)
	}
	defer response.Body.Close()

	if got := response.Header.Get("Content-Encoding"); got != "gzip" {
		t.Fatalf("Content-Encoding %q, want %q", got, "gzip")
	}
	body, err := io.ReadAll(response.Body)
	if err != nil {
		t.Fatalf("ReadAll(response body) unexpected error: %v", err)
	}
	if !bytes.Equal(body, gzipBytes) {
		t.Fatalf("body not byte-preserved: got %d bytes, want the %d gzip bytes upstream sent", len(body), len(gzipBytes))
	}
}

func TestRelayAppUnauthorizedWithoutBearer(t *testing.T) {
	calls := int64(0)
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		atomic.AddInt64(&calls, 1)
		w.WriteHeader(http.StatusOK)
	}))
	defer upstream.Close()

	app, server := newRelayTestServer(t, upstream.URL, "relay-token", "client-id", "client-secret", nil)
	defer app.Close()
	defer server.Close()

	request, err := http.NewRequest(http.MethodPost, server.URL+"/mcp", bytes.NewReader([]byte(`{}`)))
	if err != nil {
		t.Fatalf("http.NewRequest() unexpected error: %v", err)
	}
	response, err := http.DefaultClient.Do(request)
	if err != nil {
		t.Fatalf("http client unexpected error: %v", err)
	}
	defer response.Body.Close()

	if response.StatusCode != http.StatusUnauthorized {
		t.Fatalf("status %d, want %d", response.StatusCode, http.StatusUnauthorized)
	}
	if atomic.LoadInt64(&calls) != 0 {
		t.Fatalf("expected no upstream calls, got %d", atomic.LoadInt64(&calls))
	}
}

func TestRelayAppRejectsRedirect(t *testing.T) {
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		http.Redirect(w, r, "https://example.com/next", http.StatusFound)
	}))
	defer upstream.Close()

	app, server := newRelayTestServer(t, upstream.URL, "relay-token", "client-id", "client-secret", nil)
	defer app.Close()
	defer server.Close()

	request, err := http.NewRequest(http.MethodGet, server.URL+"/mcp", nil)
	if err != nil {
		t.Fatalf("http.NewRequest() unexpected error: %v", err)
	}
	request.Header.Set("Authorization", "Bearer relay-token")

	response, err := http.DefaultClient.Do(request)
	if err != nil {
		t.Fatalf("http client unexpected error: %v", err)
	}
	defer response.Body.Close()
	if response.StatusCode != http.StatusBadGateway {
		t.Fatalf("status %d, want %d", response.StatusCode, http.StatusBadGateway)
	}

	var payload struct {
		Error string `json:"error"`
	}
	if err := json.NewDecoder(response.Body).Decode(&payload); err != nil {
		t.Fatalf("decode error response: %v", err)
	}
	if payload.Error != "bad_gateway" {
		t.Fatalf("error code %q, want bad_gateway", payload.Error)
	}
}

func TestRelayAppStreamsSSEBody(t *testing.T) {
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "text/event-stream")
		if _, err := w.Write([]byte("data: hello\n\n")); err != nil {
			t.Fatalf("upstream write: %v", err)
		}
		if _, err := w.Write([]byte("data: world\n\n")); err != nil {
			t.Fatalf("upstream write: %v", err)
		}
	}))
	defer upstream.Close()

	app, server := newRelayTestServer(t, upstream.URL, "relay-token", "client-id", "client-secret", nil)
	defer app.Close()
	defer server.Close()

	request, err := http.NewRequest(http.MethodGet, server.URL+"/mcp", nil)
	if err != nil {
		t.Fatalf("http.NewRequest() unexpected error: %v", err)
	}
	request.Header.Set("Authorization", "Bearer relay-token")
	response, err := http.DefaultClient.Do(request)
	if err != nil {
		t.Fatalf("http client unexpected error: %v", err)
	}
	defer response.Body.Close()

	body, err := io.ReadAll(response.Body)
	if err != nil {
		t.Fatalf("ReadAll(response body) unexpected error: %v", err)
	}
	if response.Header.Get("Content-Type") != "text/event-stream" {
		t.Fatalf("content-type %q, want text/event-stream", response.Header.Get("Content-Type"))
	}
	if string(body) != "data: hello\n\ndata: world\n\n" {
		t.Fatalf("stream body %q", string(body))
	}
}

func TestRelayAppFlushesAndKeepsGETStreamBeyondReadTimeout(t *testing.T) {
	releaseSecond := make(chan struct{})
	var releaseOnce sync.Once
	release := func() { releaseOnce.Do(func() { close(releaseSecond) }) }
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "text/event-stream")
		_, _ = io.WriteString(w, "data: first\n\n")
		w.(http.Flusher).Flush()
		<-releaseSecond
		time.Sleep(80 * time.Millisecond)
		_, _ = io.WriteString(w, "data: second\n\n")
		w.(http.Flusher).Flush()
	}))
	defer upstream.Close()
	defer release()

	base := t.TempDir()
	cfg := config.RelayConfig{
		UpstreamURL:          upstream.URL,
		LocalTokenPath:       writeSecret(t, base, "token", "relay-token"),
		CFClientIDPath:       writeSecret(t, base, "client-id", "client-id"),
		CFClientSecretPath:   writeSecret(t, base, "client-secret", "client-secret"),
		ConnectTimeout:       time.Second,
		ReadTimeout:          40 * time.Millisecond,
		HeaderReadTimeout:    time.Second,
		PoolTimeout:          time.Second,
		ShutdownGraceSeconds: time.Second,
	}
	store, err := credentials.NewRelayStore(cfg.CFClientIDPath, cfg.CFClientSecretPath, cfg.LocalTokenPath)
	if err != nil {
		t.Fatal(err)
	}
	app := NewRelayApp(cfg, store, nil, log.New(io.Discard, "", 0))
	app.SetStarted(true)
	server := httptest.NewServer(app)
	defer app.Close()
	defer server.Close()

	request, err := http.NewRequest(http.MethodGet, server.URL+"/mcp", nil)
	if err != nil {
		t.Fatal(err)
	}
	request.Header.Set("Authorization", "Bearer relay-token")
	client := &http.Client{Timeout: time.Second}
	response, err := client.Do(request)
	if err != nil {
		t.Fatal(err)
	}
	defer response.Body.Close()
	reader := bufio.NewReader(response.Body)
	first, err := reader.ReadString('\n')
	if err != nil || first != "data: first\n" {
		t.Fatalf("first event line = %q, err = %v", first, err)
	}
	release()
	if _, err := reader.ReadString('\n'); err != nil {
		t.Fatalf("first event separator: %v", err)
	}
	second, err := reader.ReadString('\n')
	if err != nil || second != "data: second\n" {
		t.Fatalf("second event line = %q, err = %v", second, err)
	}
}

func TestRelayAppDisconnectsClientThatStopsReadingMidStream(t *testing.T) {
	assertWedgedClientIsDisconnected(t, "text/event-stream")
}

func TestRelayAppDisconnectsClientThatStopsReadingAPlainResponse(t *testing.T) {
	assertWedgedClientIsDisconnected(t, "application/json")
}

// assertWedgedClientIsDisconnected drives a downstream client that issues a
// request and then never reads the response while holding the socket open. Once
// the socket buffers fill, the relay's write blocks; without a per-write
// deadline it blocks forever, because Go clears the connection read deadline
// before the handler runs, so the request context is never cancelled and the
// handler goroutine, upstream connection and upstream session all leak.
func assertWedgedClientIsDisconnected(t *testing.T, contentType string) {
	t.Helper()
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", contentType)
		chunk := []byte(strings.Repeat("x", 64<<10))
		for {
			select {
			case <-r.Context().Done():
				return
			default:
			}
			if _, err := w.Write(chunk); err != nil {
				return
			}
			w.(http.Flusher).Flush()
		}
	}))
	defer upstream.Close()

	base := t.TempDir()
	cfg := config.RelayConfig{
		UpstreamURL:          upstream.URL,
		LocalTokenPath:       writeSecret(t, base, "token", "relay-token"),
		CFClientIDPath:       writeSecret(t, base, "client-id", "client-id"),
		CFClientSecretPath:   writeSecret(t, base, "client-secret", "client-secret"),
		ConnectTimeout:       time.Second,
		ReadTimeout:          10 * time.Second,
		HeaderReadTimeout:    time.Second,
		WriteStallTimeout:    150 * time.Millisecond,
		PoolTimeout:          time.Second,
		ShutdownGraceSeconds: time.Second,
	}
	store, err := credentials.NewRelayStore(cfg.CFClientIDPath, cfg.CFClientSecretPath, cfg.LocalTokenPath)
	if err != nil {
		t.Fatal(err)
	}
	app := NewRelayApp(cfg, store, nil, log.New(io.Discard, "", 0))
	app.SetStarted(true)
	defer app.Close()

	handlerReturned := make(chan struct{})
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		defer close(handlerReturned)
		app.ServeHTTP(w, r)
	}))
	defer server.Close()

	address := server.Listener.Addr().String()
	conn, err := net.Dial("tcp", address)
	if err != nil {
		t.Fatal(err)
	}
	// Closing the wedged client before the servers unblocks the handler even
	// when the relay has no write bound, so a failure reports rather than
	// hanging the package.
	defer conn.Close()
	request := "GET /mcp HTTP/1.1\r\nHost: " + address + "\r\nAuthorization: Bearer relay-token\r\n\r\n"
	if _, err := conn.Write([]byte(request)); err != nil {
		t.Fatal(err)
	}

	select {
	case <-handlerReturned:
	case <-time.After(5 * time.Second):
		t.Fatal("relay handler did not return while a wedged client held the connection open")
	}
}

// The per-write budget must bound a single stalled write, never the stream: an
// SSE stream that goes quiet for longer than the budget still delivers later
// events.
func TestRelayAppKeepsIdleSSEStreamBeyondWriteStallTimeout(t *testing.T) {
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "text/event-stream")
		_, _ = io.WriteString(w, "data: first\n\n")
		w.(http.Flusher).Flush()
		time.Sleep(300 * time.Millisecond)
		_, _ = io.WriteString(w, "data: second\n\n")
		w.(http.Flusher).Flush()
	}))
	defer upstream.Close()

	base := t.TempDir()
	cfg := config.RelayConfig{
		UpstreamURL:          upstream.URL,
		LocalTokenPath:       writeSecret(t, base, "token", "relay-token"),
		CFClientIDPath:       writeSecret(t, base, "client-id", "client-id"),
		CFClientSecretPath:   writeSecret(t, base, "client-secret", "client-secret"),
		ConnectTimeout:       time.Second,
		ReadTimeout:          10 * time.Second,
		HeaderReadTimeout:    time.Second,
		WriteStallTimeout:    50 * time.Millisecond,
		PoolTimeout:          time.Second,
		ShutdownGraceSeconds: time.Second,
	}
	store, err := credentials.NewRelayStore(cfg.CFClientIDPath, cfg.CFClientSecretPath, cfg.LocalTokenPath)
	if err != nil {
		t.Fatal(err)
	}
	app := NewRelayApp(cfg, store, nil, log.New(io.Discard, "", 0))
	app.SetStarted(true)
	defer app.Close()
	server := httptest.NewServer(app)
	defer server.Close()

	request, err := http.NewRequest(http.MethodGet, server.URL+"/mcp", nil)
	if err != nil {
		t.Fatal(err)
	}
	request.Header.Set("Authorization", "Bearer relay-token")
	response, err := (&http.Client{Timeout: 5 * time.Second}).Do(request)
	if err != nil {
		t.Fatal(err)
	}
	defer response.Body.Close()
	reader := bufio.NewReader(response.Body)
	for _, want := range []string{"data: first\n", "\n", "data: second\n"} {
		line, err := reader.ReadString('\n')
		if err != nil {
			t.Fatalf("reading %q: %v", want, err)
		}
		if line != want {
			t.Fatalf("line %q, want %q", line, want)
		}
	}
}

// A GET stream whose upstream stops sending while holding the socket open is
// logically dead: nothing will ever arrive again, yet every layer below sees a
// healthy connection. Without a per-read idle bound the relay blocks in
// body.Read forever, so the downstream client's GET never ends, it never
// reconnects, and every later server-to-client notification is lost.
func TestRelayAppEndsGETStreamThatGoesSilent(t *testing.T) {
	upstreamDone := make(chan struct{})
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "text/event-stream")
		_, _ = io.WriteString(w, "data: first\n\n")
		w.(http.Flusher).Flush()
		<-upstreamDone
	}))
	defer upstream.Close()

	base := t.TempDir()
	cfg := config.RelayConfig{
		UpstreamURL:          upstream.URL,
		LocalTokenPath:       writeSecret(t, base, "token", "relay-token"),
		CFClientIDPath:       writeSecret(t, base, "client-id", "client-id"),
		CFClientSecretPath:   writeSecret(t, base, "client-secret", "client-secret"),
		ConnectTimeout:       time.Second,
		ReadTimeout:          10 * time.Second,
		HeaderReadTimeout:    time.Second,
		WriteStallTimeout:    5 * time.Second,
		StreamIdleTimeout:    200 * time.Millisecond,
		PoolTimeout:          time.Second,
		ShutdownGraceSeconds: time.Second,
	}
	store, err := credentials.NewRelayStore(cfg.CFClientIDPath, cfg.CFClientSecretPath, cfg.LocalTokenPath)
	if err != nil {
		t.Fatal(err)
	}
	app := NewRelayApp(cfg, store, nil, log.New(io.Discard, "", 0))
	app.SetStarted(true)
	defer app.Close()

	handlerReturned := make(chan struct{})
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		defer close(handlerReturned)
		app.ServeHTTP(w, r)
	}))
	defer server.Close()
	// Releasing the upstream handler unblocks the relay even when it has no
	// idle bound, so a failure reports rather than wedging the package. It is
	// declared last so it runs before the two servers are closed.
	defer close(upstreamDone)

	request, err := http.NewRequest(http.MethodGet, server.URL+"/mcp", nil)
	if err != nil {
		t.Fatal(err)
	}
	request.Header.Set("Authorization", "Bearer relay-token")
	response, err := (&http.Client{Timeout: 10 * time.Second}).Do(request)
	if err != nil {
		t.Fatal(err)
	}
	defer response.Body.Close()
	reader := bufio.NewReader(response.Body)
	if line, err := reader.ReadString('\n'); err != nil || line != "data: first\n" {
		t.Fatalf("first event line = %q, err = %v", line, err)
	}

	select {
	case <-handlerReturned:
	case <-time.After(5 * time.Second):
		t.Fatal("relay handler did not return while upstream held a silent stream open")
	}
	if _, err := io.ReadAll(reader); err != nil && !errors.Is(err, io.ErrUnexpectedEOF) {
		t.Fatalf("draining the torn-down stream: %v", err)
	}
}

// The idle bound is per read, not per stream: every read that returns data
// buys another full budget, so a stream that keeps producing is never capped
// even when it outlives the budget several times over.
func TestRelayAppKeepsProducingGETStreamBeyondStreamIdleTimeout(t *testing.T) {
	const events = 4
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "text/event-stream")
		for index := 0; index < events; index++ {
			time.Sleep(60 * time.Millisecond)
			if _, err := io.WriteString(w, "data: event\n\n"); err != nil {
				return
			}
			w.(http.Flusher).Flush()
		}
	}))
	defer upstream.Close()

	base := t.TempDir()
	cfg := config.RelayConfig{
		UpstreamURL:          upstream.URL,
		LocalTokenPath:       writeSecret(t, base, "token", "relay-token"),
		CFClientIDPath:       writeSecret(t, base, "client-id", "client-id"),
		CFClientSecretPath:   writeSecret(t, base, "client-secret", "client-secret"),
		ConnectTimeout:       time.Second,
		ReadTimeout:          10 * time.Second,
		HeaderReadTimeout:    time.Second,
		WriteStallTimeout:    5 * time.Second,
		StreamIdleTimeout:    250 * time.Millisecond,
		PoolTimeout:          time.Second,
		ShutdownGraceSeconds: time.Second,
	}
	store, err := credentials.NewRelayStore(cfg.CFClientIDPath, cfg.CFClientSecretPath, cfg.LocalTokenPath)
	if err != nil {
		t.Fatal(err)
	}
	app := NewRelayApp(cfg, store, nil, log.New(io.Discard, "", 0))
	app.SetStarted(true)
	defer app.Close()
	server := httptest.NewServer(app)
	defer server.Close()

	request, err := http.NewRequest(http.MethodGet, server.URL+"/mcp", nil)
	if err != nil {
		t.Fatal(err)
	}
	request.Header.Set("Authorization", "Bearer relay-token")
	response, err := (&http.Client{Timeout: 10 * time.Second}).Do(request)
	if err != nil {
		t.Fatal(err)
	}
	defer response.Body.Close()
	reader := bufio.NewReader(response.Body)
	for index := 0; index < events; index++ {
		line, err := reader.ReadString('\n')
		if err != nil {
			t.Fatalf("event %d: %v", index, err)
		}
		if line != "data: event\n" {
			t.Fatalf("event %d line %q, want %q", index, line, "data: event\n")
		}
		if _, err := reader.ReadString('\n'); err != nil {
			t.Fatalf("event %d separator: %v", index, err)
		}
	}
}

// A response emitted after a slow upstream round trip must still reach the
// client. WriteStallTimeout is deliberately far smaller than ReadTimeout here,
// matching the production ratio (30s against 300s): any bound armed once per
// request rather than once per write has long expired by the time these
// responses are written, and the payload is then lost in the post-handler
// flush without any write ever returning an error.
func TestRelayAppDeliversResponsesWrittenAfterASlowUpstream(t *testing.T) {
	cases := []struct {
		name       string
		upstream   http.HandlerFunc
		wantStatus int
		wantBody   string
	}{
		{
			name: "gateway timeout after a slow upstream",
			upstream: func(w http.ResponseWriter, r *http.Request) {
				// Outlasts the relay's ReadTimeout, then returns under its own
				// steam so httptest.Server.Close never waits on it.
				time.Sleep(600 * time.Millisecond)
				w.WriteHeader(http.StatusOK)
			},
			wantStatus: http.StatusGatewayTimeout,
			wantBody:   "gateway_timeout",
		},
		{
			name: "slow response with an empty body",
			upstream: func(w http.ResponseWriter, r *http.Request) {
				time.Sleep(200 * time.Millisecond)
				w.WriteHeader(http.StatusNoContent)
			},
			wantStatus: http.StatusNoContent,
			wantBody:   "",
		},
		{
			name: "slow response with a body",
			upstream: func(w http.ResponseWriter, r *http.Request) {
				time.Sleep(200 * time.Millisecond)
				w.Header().Set("Content-Type", "application/json")
				_, _ = w.Write([]byte(`{"ok":true}`))
			},
			wantStatus: http.StatusOK,
			wantBody:   `{"ok":true}`,
		},
	}

	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			upstream := httptest.NewServer(c.upstream)
			defer upstream.Close()

			base := t.TempDir()
			cfg := config.RelayConfig{
				UpstreamURL:          upstream.URL,
				LocalTokenPath:       writeSecret(t, base, "token", "relay-token"),
				CFClientIDPath:       writeSecret(t, base, "client-id", "client-id"),
				CFClientSecretPath:   writeSecret(t, base, "client-secret", "client-secret"),
				ConnectTimeout:       time.Second,
				ReadTimeout:          400 * time.Millisecond,
				HeaderReadTimeout:    time.Second,
				WriteStallTimeout:    50 * time.Millisecond,
				PoolTimeout:          time.Second,
				ShutdownGraceSeconds: time.Second,
			}
			store, err := credentials.NewRelayStore(cfg.CFClientIDPath, cfg.CFClientSecretPath, cfg.LocalTokenPath)
			if err != nil {
				t.Fatal(err)
			}
			app := NewRelayApp(cfg, store, nil, log.New(io.Discard, "", 0))
			app.SetStarted(true)
			defer app.Close()
			server := httptest.NewServer(app)
			defer server.Close()

			request, err := http.NewRequest(http.MethodPost, server.URL+"/mcp", bytes.NewReader([]byte(`{}`)))
			if err != nil {
				t.Fatal(err)
			}
			request.Header.Set("Authorization", "Bearer relay-token")
			response, err := (&http.Client{Timeout: 5 * time.Second}).Do(request)
			if err != nil {
				t.Fatalf("response lost: %v", err)
			}
			defer response.Body.Close()
			if response.StatusCode != c.wantStatus {
				t.Fatalf("status %d, want %d", response.StatusCode, c.wantStatus)
			}
			payload, err := io.ReadAll(response.Body)
			if err != nil {
				t.Fatalf("read body: %v", err)
			}
			if !strings.Contains(string(payload), c.wantBody) {
				t.Fatalf("body %q, want it to contain %q", string(payload), c.wantBody)
			}
		})
	}
}

// deadlineRecorder logs write-deadline and write calls in the order they
// happen, so a test can assert not merely that a bound exists but that it is
// armed immediately before the write it is supposed to bound.
type deadlineRecorder struct {
	http.ResponseWriter
	events    []string
	deadlines []time.Time
}

func (d *deadlineRecorder) SetWriteDeadline(deadline time.Time) error {
	d.events = append(d.events, "deadline")
	d.deadlines = append(d.deadlines, deadline)
	return nil
}

func (d *deadlineRecorder) Write(chunk []byte) (int, error) {
	d.events = append(d.events, "write")
	return d.ResponseWriter.Write(chunk)
}

// Health and error responses never reach copyResponse, so each has to arm its
// own deadline. A ~90 byte body fits the socket buffer, so an end-to-end wedge
// is impossible here and this is asserted white-box.
func TestRelayAppBoundsResponsesThatSkipTheBodyCopy(t *testing.T) {
	base := t.TempDir()
	cfg := config.RelayConfig{
		UpstreamURL:          "http://127.0.0.1:1/mcp",
		LocalTokenPath:       writeSecret(t, base, "token", "relay-token"),
		CFClientIDPath:       writeSecret(t, base, "client-id", "client-id"),
		CFClientSecretPath:   writeSecret(t, base, "client-secret", "client-secret"),
		ConnectTimeout:       time.Second,
		ReadTimeout:          2 * time.Second,
		HeaderReadTimeout:    time.Second,
		WriteStallTimeout:    250 * time.Millisecond,
		PoolTimeout:          time.Second,
		ShutdownGraceSeconds: time.Second,
	}
	store, err := credentials.NewRelayStore(cfg.CFClientIDPath, cfg.CFClientSecretPath, cfg.LocalTokenPath)
	if err != nil {
		t.Fatal(err)
	}
	app := NewRelayApp(cfg, store, nil, log.New(io.Discard, "", 0))
	app.SetStarted(true)
	defer app.Close()

	cases := []struct {
		name   string
		method string
		target string
	}{
		{name: "healthz", method: http.MethodGet, target: "/healthz"},
		{name: "unauthorised", method: http.MethodPost, target: "/mcp"},
		{name: "not found", method: http.MethodGet, target: "/nope"},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			recorder := &deadlineRecorder{ResponseWriter: httptest.NewRecorder()}
			request := httptest.NewRequest(c.method, c.target, nil)
			before := time.Now()
			app.ServeHTTP(recorder, request)

			// Ordering alone cannot tell an entry-point deadline from a
			// just-before-write one when nothing happens in between; that
			// distinction is what TestRelayAppDeliversResponsesWrittenAfterASlowUpstream
			// exists for. This guards the weaker but still necessary property
			// that no direct write happens unbounded.
			if got := strings.Join(recorder.events, ","); got != "deadline,write" {
				t.Fatalf("call sequence %q, want %q", got, "deadline,write")
			}
			deadline := recorder.deadlines[0]
			if !deadline.After(before) || deadline.After(before.Add(2*cfg.WriteStallTimeout)) {
				t.Fatalf("write deadline %s is not within one stall budget of %s", deadline, before)
			}
		})
	}
}

// An empty upstream body issues no Write at all — io.Copy calls the writer zero
// times — so the deadline the write path normally arms never happens unless
// copyResponse arms one itself. 202 and 204 are the routine no-body replies in
// this protocol, so this is the common case, not an exotic one.
func TestRelayAppBoundsEmptyBodyResponses(t *testing.T) {
	cases := []struct {
		name   string
		status int
	}{
		{name: "accepted", status: http.StatusAccepted},
		{name: "no content", status: http.StatusNoContent},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				w.WriteHeader(c.status)
			}))
			defer upstream.Close()

			base := t.TempDir()
			cfg := config.RelayConfig{
				UpstreamURL:          upstream.URL,
				LocalTokenPath:       writeSecret(t, base, "token", "relay-token"),
				CFClientIDPath:       writeSecret(t, base, "client-id", "client-id"),
				CFClientSecretPath:   writeSecret(t, base, "client-secret", "client-secret"),
				ConnectTimeout:       time.Second,
				ReadTimeout:          2 * time.Second,
				HeaderReadTimeout:    time.Second,
				WriteStallTimeout:    250 * time.Millisecond,
				PoolTimeout:          time.Second,
				ShutdownGraceSeconds: time.Second,
			}
			store, err := credentials.NewRelayStore(cfg.CFClientIDPath, cfg.CFClientSecretPath, cfg.LocalTokenPath)
			if err != nil {
				t.Fatal(err)
			}
			app := NewRelayApp(cfg, store, nil, log.New(io.Discard, "", 0))
			app.SetStarted(true)
			defer app.Close()

			recorder := &deadlineRecorder{ResponseWriter: httptest.NewRecorder()}
			request := httptest.NewRequest(http.MethodPost, "/mcp", bytes.NewReader([]byte(`{}`)))
			request.Header.Set("Authorization", "Bearer relay-token")
			before := time.Now()
			app.ServeHTTP(recorder, request)

			if got := strings.Join(recorder.events, ","); got != "deadline" {
				t.Fatalf("call sequence %q, want %q", got, "deadline")
			}
			deadline := recorder.deadlines[0]
			if !deadline.After(before) || deadline.After(before.Add(2*cfg.WriteStallTimeout)) {
				t.Fatalf("write deadline %s is not within one stall budget of %s", deadline, before)
			}
		})
	}
}

func TestRelayAppConnectFailure(t *testing.T) {
	app, server := newRelayTestServer(t, "http://127.0.0.1:8765/mcp", "relay-token", "client-id", "client-secret", &http.Client{Transport: failRoundTripper{}})
	defer app.Close()
	defer server.Close()

	request, err := http.NewRequest(http.MethodGet, server.URL+"/mcp", nil)
	if err != nil {
		t.Fatalf("http.NewRequest() unexpected error: %v", err)
	}
	request.Header.Set("Authorization", "Bearer relay-token")

	response, err := http.DefaultClient.Do(request)
	if err != nil {
		t.Fatalf("http client unexpected error: %v", err)
	}
	defer response.Body.Close()
	if response.StatusCode != http.StatusBadGateway {
		t.Fatalf("status %d, want %d", response.StatusCode, http.StatusBadGateway)
	}
}

func TestRelayAppReloadsCredentials(t *testing.T) {
	base := t.TempDir()
	tokenPath := writeSecret(t, base, "token", "relay-token")
	cfg := config.RelayConfig{
		UpstreamURL:          "",
		LocalTokenPath:       tokenPath,
		CFClientIDPath:       writeSecret(t, base, "client-id", "client-id"),
		CFClientSecretPath:   writeSecret(t, base, "client-secret", "client-secret"),
		ReadTimeout:          2 * time.Second,
		ConnectTimeout:       2 * time.Second,
		HeaderReadTimeout:    2 * time.Second,
		PoolTimeout:          time.Second,
		ShutdownGraceSeconds: time.Second,
	}

	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusOK)
	}))
	defer upstream.Close()
	cfg.UpstreamURL = upstream.URL

	store, err := credentials.NewRelayStore(cfg.CFClientIDPath, cfg.CFClientSecretPath, cfg.LocalTokenPath)
	if err != nil {
		t.Fatalf("NewRelayStore() unexpected error: %v", err)
	}
	app := NewRelayApp(cfg, store, nil, log.New(io.Discard, "", log.LstdFlags))
	app.SetStarted(true)
	server := httptest.NewServer(app)
	defer app.Close()
	defer server.Close()

	requestWithToken := func(token string) int {
		request, err := http.NewRequest(http.MethodPost, server.URL+"/mcp", bytes.NewReader([]byte(`{}`)))
		if err != nil {
			t.Fatalf("http.NewRequest() unexpected error: %v", err)
		}
		request.Header.Set("Authorization", "Bearer "+token)
		response, err := http.DefaultClient.Do(request)
		if err != nil {
			t.Fatalf("http client unexpected error: %v", err)
		}
		status := response.StatusCode
		response.Body.Close()
		return status
	}

	if status := requestWithToken("relay-token"); status != http.StatusOK {
		t.Fatalf("status %d, want %d", status, http.StatusOK)
	}
	if err := os.WriteFile(tokenPath, []byte("rotated-token"), 0o600); err != nil {
		t.Fatalf("os.WriteFile() unexpected error: %v", err)
	}
	if err := app.ReloadCredentials(); err != nil {
		t.Fatalf("ReloadCredentials() unexpected error: %v", err)
	}
	if status := requestWithToken("relay-token"); status != http.StatusUnauthorized {
		t.Fatalf("status %d, want %d", status, http.StatusUnauthorized)
	}
	if status := requestWithToken("rotated-token"); status != http.StatusOK {
		t.Fatalf("status %d, want %d", status, http.StatusOK)
	}
}

func newRelayTestServer(t *testing.T, upstream, token, clientID, clientSecret string, client *http.Client) (*RelayApp, *httptest.Server) {
	return newRelayTestServerWithLogger(t, upstream, token, clientID, clientSecret, client, log.New(io.Discard, "", log.LstdFlags))
}

func newRelayTestServerWithLogger(t *testing.T, upstream, token, clientID, clientSecret string, client *http.Client, logger *log.Logger) (*RelayApp, *httptest.Server) {
	t.Helper()
	base := t.TempDir()
	cfg := config.RelayConfig{
		UpstreamURL:          upstream,
		LocalTokenPath:       writeSecret(t, base, "token", token),
		CFClientIDPath:       writeSecret(t, base, "client-id", clientID),
		CFClientSecretPath:   writeSecret(t, base, "client-secret", clientSecret),
		ConnectTimeout:       2 * time.Second,
		ReadTimeout:          2 * time.Second,
		HeaderReadTimeout:    2 * time.Second,
		PoolTimeout:          time.Second,
		ShutdownGraceSeconds: time.Second,
	}
	store, err := credentials.NewRelayStore(cfg.CFClientIDPath, cfg.CFClientSecretPath, cfg.LocalTokenPath)
	if err != nil {
		t.Fatalf("NewRelayStore() unexpected error: %v", err)
	}
	app := NewRelayApp(cfg, store, client, logger)
	app.SetStarted(true)
	server := httptest.NewServer(app)
	return app, server
}

func cloneHeaders(source http.Header) map[string][]string {
	output := make(map[string][]string, len(source))
	for key, values := range source {
		output[key] = append([]string(nil), values...)
	}
	return output
}

func readCapturedRequest(t *testing.T, ch chan capturedRequest) capturedRequest {
	t.Helper()
	select {
	case captured := <-ch:
		return captured
	default:
		t.Fatal("expected upstream request to be captured")
	}
	return capturedRequest{}
}

func writeSecret(t *testing.T, base, name string, value string) string {
	t.Helper()
	path := filepath.Join(base, name)
	if err := os.WriteFile(path, []byte(value), 0o600); err != nil {
		t.Fatalf("os.WriteFile() unexpected error: %v", err)
	}
	return path
}
