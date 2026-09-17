package relay

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"errors"
	"io"
	"log"
	"mime"
	"net"
	"net/http"
	"strconv"
	"strings"
	"sync/atomic"
	"time"

	"github.com/veridian69/cairn/cairn-mcp/internal/config"
	"github.com/veridian69/cairn/cairn-mcp/internal/credentials"
	"github.com/veridian69/cairn/cairn-mcp/internal/headers"
)

// defaultWriteStallTimeout bounds a single write, and defaultStreamIdleTimeout
// the silence between reads on a GET stream, when RelayConfig is built in code
// rather than parsed; config.ParseConfig always supplies positive values.
const (
	defaultWriteStallTimeout = 30 * time.Second
	defaultStreamIdleTimeout = 300 * time.Second
)

type RelayApp struct {
	config      config.RelayConfig
	credentials *credentials.CredentialStore
	client      *http.Client
	logger      *log.Logger
	ownsClient  bool
	started     atomic.Bool
}

func NewRelayApp(cfg config.RelayConfig, store *credentials.CredentialStore, client *http.Client, logger *log.Logger) *RelayApp {
	ownsClient := client == nil
	if client == nil {
		client = NewHTTPClient(cfg, 0)
	}
	if logger == nil {
		logger = log.New(io.Discard, "", log.LstdFlags)
	}
	if cfg.WriteStallTimeout <= 0 {
		cfg.WriteStallTimeout = defaultWriteStallTimeout
	}
	if cfg.StreamIdleTimeout <= 0 {
		cfg.StreamIdleTimeout = defaultStreamIdleTimeout
	}
	return &RelayApp{
		config:      cfg,
		credentials: store,
		client:      client,
		logger:      logger,
		ownsClient:  ownsClient,
	}
}

func NewHTTPClient(cfg config.RelayConfig, timeout time.Duration) *http.Client {
	return &http.Client{
		Transport: &http.Transport{
			Proxy:                 http.ProxyFromEnvironment,
			DialContext:           (&net.Dialer{Timeout: cfg.ConnectTimeout}).DialContext,
			IdleConnTimeout:       cfg.PoolTimeout,
			ForceAttemptHTTP2:     true,
			ResponseHeaderTimeout: cfg.ReadTimeout,
			DisableCompression:    true,
		},
		Timeout: timeout,
		CheckRedirect: func(req *http.Request, via []*http.Request) error {
			return http.ErrUseLastResponse
		},
	}
}

func (app *RelayApp) SetStarted(started bool) {
	app.started.Store(started)
}

func (app *RelayApp) ServeHTTP(w http.ResponseWriter, request *http.Request) {
	if request.URL.Path == "/healthz" {
		app.handleHealthz(w, request)
		return
	}
	if request.URL.Path != "/mcp" {
		app.writeError(w, http.StatusNotFound, "not_found")
		return
	}
	if request.Method != http.MethodPost && request.Method != http.MethodGet && request.Method != http.MethodDelete {
		app.writeError(w, http.StatusMethodNotAllowed, "method_not_allowed")
		return
	}
	if !app.started.Load() {
		app.writeError(w, http.StatusServiceUnavailable, "not_ready")
		return
	}

	snapshot := app.credentials.Snapshot()
	if !headers.Authorised(request.Header, snapshot.LocalToken) {
		app.writeError(w, http.StatusUnauthorized, "unauthorised")
		return
	}

	start := time.Now()
	status := app.forward(w, request, snapshot)
	durationMs := float64(time.Since(start).Microseconds()) / 1000.0
	app.logger.Printf("method=%s route=%s status_class=%d duration_ms=%.0f", request.Method, request.URL.Path, status/100, durationMs)
}

func (app *RelayApp) forward(w http.ResponseWriter, request *http.Request, snapshot credentials.CredentialSnapshot) int {
	var ctx context.Context
	var cancel context.CancelFunc
	if request.Method == http.MethodGet {
		// A standalone GET is a long-lived stream, so it gets no whole-request
		// deadline; the idle bound armed around the body copy below is what
		// ends it when upstream stops sending.
		ctx, cancel = context.WithCancel(request.Context())
	} else {
		ctx, cancel = context.WithTimeout(request.Context(), app.config.ReadTimeout)
	}
	defer cancel()

	var body io.Reader
	if request.Method == http.MethodPost {
		body = request.Body
	}

	upstreamRequest, err := http.NewRequestWithContext(ctx, request.Method, app.config.UpstreamURL, body)
	if err != nil {
		app.writeError(w, http.StatusBadGateway, "bad_gateway")
		return http.StatusBadGateway
	}
	upstreamRequest.Header = headers.UpstreamHeaders(request.Header, snapshot)

	response, err := app.client.Do(upstreamRequest)
	if err != nil {
		app.handleUpstreamError(w, ctx, err)
		if isTimeoutError(err) {
			return http.StatusGatewayTimeout
		}
		return http.StatusBadGateway
	}
	defer response.Body.Close()

	if response.StatusCode >= 300 && response.StatusCode < 400 {
		app.writeError(w, http.StatusBadGateway, "bad_gateway")
		return http.StatusBadGateway
	}

	forwardedHeaders := headers.DownstreamHeaders(response.Header)
	for name, values := range forwardedHeaders {
		for _, value := range values {
			w.Header().Add(name, value)
		}
	}
	w.WriteHeader(response.StatusCode)

	upstreamBody := io.Reader(response.Body)
	if request.Method == http.MethodGet {
		guard := newIdleReader(response.Body, app.config.StreamIdleTimeout, cancel)
		defer guard.stop()
		upstreamBody = guard
	}
	if err := copyResponse(app.bounded(w), upstreamBody, isEventStream(response.Header.Get("Content-Type"))); err != nil {
		return http.StatusBadGateway
	}
	return response.StatusCode
}

// idleReader bounds the silence between reads, never the life of the stream:
// the budget is armed before the first read and re-armed by every read that
// returns data — a (0, nil) read is silence, not progress, and buys nothing —
// so a stream that keeps producing runs for as long as upstream keeps
// it open however long the gaps between its events. A stream that goes
// completely silent — the socket alive, nothing ever arriving — is cancelled
// after one budget instead of blocking the handler goroutine and its upstream
// connection forever, which would leave the downstream client holding a GET
// that never ends and so never reconnects.
type idleReader struct {
	body   io.Reader
	timer  *time.Timer
	budget time.Duration
}

func newIdleReader(body io.Reader, budget time.Duration, onIdle func()) *idleReader {
	return &idleReader{body: body, timer: time.AfterFunc(budget, onIdle), budget: budget}
}

func (r *idleReader) Read(chunk []byte) (int, error) {
	count, err := r.body.Read(chunk)
	if err == nil && count > 0 {
		r.timer.Reset(r.budget)
	}
	return count, err
}

func (r *idleReader) stop() {
	r.timer.Stop()
}

func copyResponse(writer boundedWriter, body io.Reader, flush bool) error {
	if !flush {
		// io.Copy issues no Write at all for an empty body, so the deadline the
		// writer would otherwise arm never happens and the post-handler header
		// flush runs unbounded. 202 and 204 are routine replies here. A non-empty
		// body is already covered by boundedWriter.Write, so only arm here when
		// nothing armed one — arming before the copy would leave a stale deadline
		// covering a body that goes silent past the budget then ends empty.
		written, err := io.Copy(writer, body)
		if written == 0 && err == nil {
			if deadlineErr := extendWriteDeadline(writer.controller, writer.budget); deadlineErr != nil {
				return deadlineErr
			}
		}
		return err
	}
	if err := extendWriteDeadline(writer.controller, writer.budget); err != nil {
		return err
	}
	if err := writer.controller.Flush(); err != nil {
		return err
	}
	buffer := make([]byte, 32<<10)
	for {
		count, readErr := body.Read(buffer)
		if count > 0 {
			if _, err := writer.Write(buffer[:count]); err != nil {
				return err
			}
			// This flush runs under the deadline armed by the write above:
			// one event, one budget.
			if err := writer.controller.Flush(); err != nil {
				return err
			}
		}
		if readErr == io.EOF {
			return nil
		}
		if readErr != nil {
			return readErr
		}
	}
}

// bounded is the only way this handler writes a response body. Every write goes
// through it, because a deadline armed anywhere other than immediately before a
// write is a whole-request bound in disguise: a response emitted after a slow
// upstream round trip would be written under an expired deadline, land in the
// bufio buffer with no error, and then be dropped by the post-handler flush.
func (app *RelayApp) bounded(w http.ResponseWriter) boundedWriter {
	return boundedWriter{
		writer:     w,
		controller: http.NewResponseController(w),
		budget:     app.config.WriteStallTimeout,
	}
}

// boundedWriter gives every individual write its own deadline, so a client that
// stops reading is disconnected after one stalled write while a stream that
// keeps flowing runs for as long as upstream keeps it open.
type boundedWriter struct {
	writer     io.Writer
	controller *http.ResponseController
	budget     time.Duration
}

func (b boundedWriter) Write(chunk []byte) (int, error) {
	if err := extendWriteDeadline(b.controller, b.budget); err != nil {
		return 0, err
	}
	return b.writer.Write(chunk)
}

func extendWriteDeadline(controller *http.ResponseController, budget time.Duration) error {
	err := controller.SetWriteDeadline(time.Now().Add(budget))
	if err != nil && !errors.Is(err, http.ErrNotSupported) {
		return err
	}
	return nil
}

func isEventStream(value string) bool {
	mediaType, _, err := mime.ParseMediaType(value)
	return err == nil && mediaType == "text/event-stream"
}

func (app *RelayApp) handleHealthz(w http.ResponseWriter, request *http.Request) {
	if request.Method != http.MethodGet {
		app.writeError(w, http.StatusNotFound, "not_found")
		return
	}
	if !app.started.Load() {
		app.writeError(w, http.StatusServiceUnavailable, "not_ready")
		return
	}
	payload, _ := json.Marshal(map[string]string{"status": "ok"})
	w.Header().Set("content-type", "application/json")
	w.Header().Set("content-length", strconv.Itoa(len(payload)))
	w.WriteHeader(http.StatusOK)
	if _, err := app.bounded(w).Write(payload); err != nil {
		app.logger.Printf("health write failed: %v", err)
	}
}

func (app *RelayApp) handleUpstreamError(w http.ResponseWriter, requestContext context.Context, err error) {
	if isTimeoutError(err) || errors.Is(requestContext.Err(), context.DeadlineExceeded) {
		app.writeError(w, http.StatusGatewayTimeout, "gateway_timeout")
		return
	}
	app.writeError(w, http.StatusBadGateway, "bad_gateway")
}

func (app *RelayApp) writeError(w http.ResponseWriter, status int, errorCode string) {
	payload, _ := json.Marshal(map[string]string{
		"error":          errorCode,
		"correlation_id": newCorrelationID(),
	})
	w.Header().Set("content-type", "application/json")
	w.Header().Set("content-length", strconv.Itoa(len(payload)))
	w.WriteHeader(status)
	if _, err := app.bounded(w).Write(payload); err != nil {
		app.logger.Printf("write error response: %v", err)
	}
}

func (app *RelayApp) ReloadCredentials() error {
	return app.credentials.Reload()
}

func isTimeoutError(err error) bool {
	type timeout interface {
		Timeout() bool
	}
	if errors.Is(err, context.DeadlineExceeded) {
		return true
	}
	var candidate timeout
	if errors.As(err, &candidate) {
		return candidate.Timeout()
	}
	if strings.Contains(err.Error(), "timeout") {
		return true
	}
	return false
}

func (app *RelayApp) Close() {
	if app.ownsClient {
		app.client.CloseIdleConnections()
	}
}

func newCorrelationID() string {
	raw := make([]byte, 16)
	if _, err := rand.Read(raw); err != nil {
		return "00000000000000000000000000000000"
	}
	return hex.EncodeToString(raw)
}
