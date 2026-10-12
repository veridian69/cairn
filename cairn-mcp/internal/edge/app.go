package edge

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"errors"
	"io"
	"log"
	"net/http"
	"strconv"
	"strings"
	"sync/atomic"
	"time"

	"github.com/veridian69/cairn/cairn-mcp/internal/accessjwt"
	"github.com/veridian69/cairn/cairn-mcp/internal/config"
	"github.com/veridian69/cairn/cairn-mcp/internal/headers"
	"github.com/veridian69/cairn/cairn-mcp/internal/relay"
)

const (
	assertionHeader = "Cf-Access-Jwt-Assertion"
	maxBodyBytes    = 4 << 20
	// defaultWriteStall matches the relay's default --write-stall-timeout.
	defaultWriteStall = 30 * time.Second
)

type App struct {
	cfg      Config
	verifier *accessjwt.Verifier
	tokens   *RouteTokens
	client   *http.Client
	logger   *log.Logger
	expect   accessjwt.Expectation
	started  atomic.Bool
	// writeStall bounds each write to the client, not the life of a response.
	writeStall time.Duration
}

func NewApp(cfg Config, verifier *accessjwt.Verifier, tokens *RouteTokens, client *http.Client, logger *log.Logger) *App {
	if client == nil {
		client = relay.NewHTTPClient(config.RelayConfig{
			ConnectTimeout: cfg.ConnectTimeout,
			ReadTimeout:    cfg.ReadTimeout,
			PoolTimeout:    5 * time.Second,
		}, 0)
	}
	if logger == nil {
		logger = log.New(io.Discard, "", 0)
	}
	return &App{
		cfg: cfg, verifier: verifier, tokens: tokens, client: client, logger: logger,
		expect:     accessjwt.Expectation{Issuer: cfg.Issuer(), Audience: cfg.Audience, Email: cfg.AllowedEmail},
		writeStall: defaultWriteStall,
	}
}

func (a *App) SetStarted(started bool) { a.started.Store(started) }

// Reload re-reads every route credential; a failure keeps the old set.
func (a *App) Reload() error { return a.tokens.Reload() }

func (a *App) ServeHTTP(w http.ResponseWriter, request *http.Request) {
	start := time.Now()
	w = &stallBoundedWriter{ResponseWriter: w, controller: http.NewResponseController(w), budget: a.writeStall}
	status, route, decision := a.handle(w, request)
	a.logger.Printf("method=%s route=%s status_class=%d decision=%s duration_ms=%.0f",
		request.Method, route, status/100, decision, float64(time.Since(start))/float64(time.Millisecond))
}

// handle returns the status, a fixed route label (a configured route path,
// "healthz" or "unmapped", never client-supplied text) and the decision.
func (a *App) handle(w http.ResponseWriter, request *http.Request) (int, string, string) {
	if request.URL.Path == "/healthz" {
		if request.Method != http.MethodGet {
			return a.writeError(w, http.StatusNotFound, "not_found"), "healthz", "not_found"
		}
		if !a.started.Load() {
			return a.writeError(w, http.StatusServiceUnavailable, "not_ready"), "healthz", "not_ready"
		}
		return a.writeJSON(w, http.StatusOK, map[string]string{"status": "ok"}), "healthz", "healthz"
	}
	token, ok := a.tokens.Lookup(request.URL.Path)
	if !ok {
		return a.writeError(w, http.StatusNotFound, "not_found"), "unmapped", "not_found"
	}
	route := request.URL.Path // equals the configured key: Lookup matched it exactly
	if request.Method != http.MethodPost {
		return a.writeError(w, http.StatusMethodNotAllowed, "method_not_allowed"), route, "method_not_allowed"
	}
	if !a.started.Load() {
		return a.writeError(w, http.StatusServiceUnavailable, "not_ready"), route, "not_ready"
	}
	assertion := request.Header.Get(assertionHeader)
	if assertion == "" {
		return a.writeError(w, http.StatusUnauthorized, "unauthorised"), route, "missing_assertion"
	}
	if _, err := a.verifier.Verify(request.Context(), assertion, a.expect); err != nil {
		switch {
		case errors.Is(err, accessjwt.ErrIdentity) || errors.Is(err, accessjwt.ErrServiceToken):
			return a.writeError(w, http.StatusForbidden, "forbidden"), route, err.Error()
		case errors.Is(err, accessjwt.ErrKeysUnavailable):
			return a.writeError(w, http.StatusServiceUnavailable, "unavailable"), route, "keys_unavailable"
		}
		return a.writeError(w, http.StatusUnauthorized, "unauthorised"), route, err.Error()
	}
	return a.forward(w, request, token), route, "allow"
}

func (a *App) forward(w http.ResponseWriter, request *http.Request, token []byte) int {
	body, err := io.ReadAll(http.MaxBytesReader(w, request.Body, maxBodyBytes))
	if err != nil {
		var tooLarge *http.MaxBytesError
		if errors.As(err, &tooLarge) {
			return a.writeError(w, http.StatusRequestEntityTooLarge, "payload_too_large")
		}
		return a.writeError(w, http.StatusBadRequest, "bad_request")
	}
	ctx, cancel := context.WithTimeout(request.Context(), a.cfg.ReadTimeout)
	defer cancel()
	upstreamRequest, err := http.NewRequestWithContext(ctx, http.MethodPost, a.cfg.UpstreamURL, strings.NewReader(string(body)))
	if err != nil {
		return a.writeError(w, http.StatusBadGateway, "bad_gateway")
	}
	upstreamRequest.Header = headers.BearerHeaders(request.Header, token)
	upstreamRequest.ContentLength = int64(len(body))
	response, err := a.client.Do(upstreamRequest)
	if err != nil {
		if errors.Is(ctx.Err(), context.DeadlineExceeded) || isTimeout(err) {
			return a.writeError(w, http.StatusGatewayTimeout, "gateway_timeout")
		}
		return a.writeError(w, http.StatusBadGateway, "bad_gateway")
	}
	defer response.Body.Close()
	if response.StatusCode >= 300 && response.StatusCode < 400 {
		return a.writeError(w, http.StatusBadGateway, "bad_gateway")
	}
	for name, values := range headers.DownstreamHeaders(response.Header) {
		for _, value := range values {
			w.Header().Add(name, value)
		}
	}
	w.WriteHeader(response.StatusCode)
	if strings.HasPrefix(response.Header.Get("Content-Type"), "text/event-stream") {
		flushCopy(w, response.Body)
	} else {
		_, _ = io.Copy(w, response.Body)
	}
	return response.StatusCode
}

// stallBoundedWriter gives every write, header write and flush its own
// deadline, so a client that stops reading is cut off after one stalled write
// while a stream that keeps flowing runs as long as upstream keeps it open.
type stallBoundedWriter struct {
	http.ResponseWriter
	controller *http.ResponseController
	budget     time.Duration
}

func (b *stallBoundedWriter) arm() {
	// ErrNotSupported just means there is no deadline to set.
	_ = b.controller.SetWriteDeadline(time.Now().Add(b.budget))
}

func (b *stallBoundedWriter) WriteHeader(status int) {
	b.arm()
	b.ResponseWriter.WriteHeader(status)
}

func (b *stallBoundedWriter) Write(chunk []byte) (int, error) {
	b.arm()
	return b.ResponseWriter.Write(chunk)
}

func (b *stallBoundedWriter) Flush() {
	b.arm()
	_ = b.controller.Flush()
}

func (b *stallBoundedWriter) Unwrap() http.ResponseWriter { return b.ResponseWriter }

func flushCopy(w http.ResponseWriter, body io.Reader) {
	flusher, _ := w.(http.Flusher)
	buffer := make([]byte, 32*1024)
	for {
		n, err := body.Read(buffer)
		if n > 0 {
			if _, writeErr := w.Write(buffer[:n]); writeErr != nil {
				return
			}
			if flusher != nil {
				flusher.Flush()
			}
		}
		if err != nil {
			return
		}
	}
}

func isTimeout(err error) bool {
	var timeout interface{ Timeout() bool }
	return errors.As(err, &timeout) && timeout.Timeout()
}

func (a *App) writeJSON(w http.ResponseWriter, status int, payload any) int {
	data, _ := json.Marshal(payload)
	w.Header().Set("content-type", "application/json")
	w.Header().Set("content-length", strconv.Itoa(len(data)))
	w.WriteHeader(status)
	_, _ = w.Write(data)
	return status
}

func (a *App) writeError(w http.ResponseWriter, status int, code string) int {
	var raw [16]byte
	_, _ = rand.Read(raw[:])
	return a.writeJSON(w, status, map[string]string{"error": code, "correlation_id": hex.EncodeToString(raw[:])})
}
