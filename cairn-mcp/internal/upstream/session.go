package upstream

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"mime"
	"net"
	"net/http"
	"net/url"
	"sync"
	"syscall"
	"time"

	"github.com/veridian69/cairn/cairn-mcp/internal/credentials"
)

const (
	eventBufferSize              = 32
	maxJSONResponseBytes         = 4 << 20
	legacyListenerProtocolCutoff = "2026-07-28"
	listenerMaxReconnects        = 3
	listenerRetryDelay           = 250 * time.Millisecond
	listenerHealthyPeriod        = 30 * time.Second
)

type AccessSource interface {
	AccessSnapshot() credentials.AccessCredentials
}

type Event struct {
	Message json.RawMessage
	Err     error
}

type SessionOption func(*sessionOptions)

type sessionOptions struct {
	legacyListener bool
}

func WithoutLegacyListener() SessionOption {
	return func(options *sessionOptions) {
		options.legacyListener = false
	}
}

type Session struct {
	endpoint string
	client   *http.Client
	access   AccessSource
	events   chan Event
	ctx      context.Context
	cancel   context.CancelFunc

	mu              sync.RWMutex
	sessionID       string
	protocolVersion string
	legacyListener  bool

	closeOnce sync.Once
	closeErr  error

	listenerOnce sync.Once
	listenerWG   sync.WaitGroup
}

func NewSession(endpoint string, client *http.Client, source AccessSource, optionFunctions ...SessionOption) (*Session, error) {
	parsed, err := url.Parse(endpoint)
	if err != nil || parsed.Scheme == "" || parsed.Host == "" {
		return nil, fmt.Errorf("invalid upstream endpoint %q", endpoint)
	}
	if client == nil {
		return nil, fmt.Errorf("upstream HTTP client is required")
	}
	if source == nil {
		return nil, fmt.Errorf("access source is required")
	}

	sessionClient := *client
	sessionClient.CheckRedirect = func(*http.Request, []*http.Request) error {
		return http.ErrUseLastResponse
	}
	ctx, cancel := context.WithCancel(context.Background())
	options := sessionOptions{legacyListener: true}
	for _, configure := range optionFunctions {
		configure(&options)
	}
	return &Session{
		endpoint:       endpoint,
		client:         &sessionClient,
		access:         source,
		events:         make(chan Event, eventBufferSize),
		ctx:            ctx,
		cancel:         cancel,
		legacyListener: options.legacyListener,
	}, nil
}

func (s *Session) Events() <-chan Event {
	return s.events
}

func (s *Session) Send(ctx context.Context, message json.RawMessage) error {
	if !json.Valid(message) {
		return fmt.Errorf("outbound message contains invalid JSON")
	}
	initializeID, err := initializeID(message)
	if err != nil {
		return err
	}

	requestCtx, cleanup := s.requestContext(ctx)
	request, err := http.NewRequestWithContext(requestCtx, http.MethodPost, s.endpoint, bytes.NewReader(message))
	if err != nil {
		cleanup()
		return fmt.Errorf("create upstream POST: %w", err)
	}
	request.Header.Set("Content-Type", "application/json")
	request.Header.Set("Accept", "application/json, text/event-stream")
	s.applySessionHeaders(request.Header)
	s.applyAccessHeaders(request.Header)

	response, err := s.client.Do(request)
	if err != nil {
		cleanup()
		return fmt.Errorf("send upstream POST: %w", err)
	}
	if response.StatusCode == http.StatusAccepted {
		s.captureSessionID(response.Header.Get("Mcp-Session-Id"))
		_ = response.Body.Close()
		cleanup()
		return nil
	}
	if response.StatusCode != http.StatusOK {
		_ = response.Body.Close()
		cleanup()
		return fmt.Errorf("unsupported upstream status %d", response.StatusCode)
	}

	contentType, _, err := mime.ParseMediaType(response.Header.Get("Content-Type"))
	if err != nil {
		_ = response.Body.Close()
		cleanup()
		return fmt.Errorf("unsupported upstream content type %q", response.Header.Get("Content-Type"))
	}
	switch contentType {
	case "application/json":
		defer cleanup()
		defer response.Body.Close()
		body, err := io.ReadAll(io.LimitReader(response.Body, maxJSONResponseBytes+1))
		if err != nil {
			return fmt.Errorf("read upstream JSON response: %w", err)
		}
		if len(body) > maxJSONResponseBytes {
			return fmt.Errorf("upstream JSON response exceeds 4 MiB limit")
		}
		if !json.Valid(body) {
			return fmt.Errorf("upstream response contains invalid JSON")
		}
		s.captureSessionID(response.Header.Get("Mcp-Session-Id"))
		return s.handleMessage(requestCtx, body, initializeID)
	case "text/event-stream":
		defer cleanup()
		defer response.Body.Close()
		s.captureSessionID(response.Header.Get("Mcp-Session-Id"))
		if err := readSSE(response.Body, func(message json.RawMessage) error {
			return s.handleMessage(requestCtx, message, initializeID)
		}); err != nil {
			return fmt.Errorf("read upstream SSE response: %w", err)
		}
		return nil
	default:
		_ = response.Body.Close()
		cleanup()
		return fmt.Errorf("unsupported upstream content type %q", contentType)
	}
}

// Close stops the standalone listener, deletes the upstream session, and closes Events.
// The caller must ensure all Send calls have returned before calling Close.
func (s *Session) Close(ctx context.Context) error {
	s.closeOnce.Do(func() {
		s.cancel()
		s.listenerWG.Wait()
		s.closeErr = s.deleteSession(ctx)
		close(s.events)
	})
	return s.closeErr
}

func (s *Session) requestContext(caller context.Context) (context.Context, func()) {
	ctx, cancel := context.WithCancel(s.ctx)
	stopCaller := context.AfterFunc(caller, cancel)
	return ctx, func() {
		stopCaller()
		cancel()
	}
}

func (s *Session) applyAccessHeaders(header http.Header) {
	access := s.access.AccessSnapshot()
	header.Set("CF-Access-Client-Id", access.ClientID)
	header.Set("CF-Access-Client-Secret", access.ClientSecret)
}

func (s *Session) applySessionHeaders(header http.Header) {
	s.mu.RLock()
	defer s.mu.RUnlock()
	if s.sessionID != "" {
		header.Set("Mcp-Session-Id", s.sessionID)
	}
	if s.protocolVersion != "" {
		header.Set("Mcp-Protocol-Version", s.protocolVersion)
	}
}

func (s *Session) captureSessionID(sessionID string) {
	if sessionID == "" {
		return
	}
	s.mu.Lock()
	s.sessionID = sessionID
	s.mu.Unlock()
}

func initializeID(message json.RawMessage) (string, error) {
	var request map[string]json.RawMessage
	if err := json.Unmarshal(message, &request); err != nil {
		return "", nil
	}
	var method string
	if err := json.Unmarshal(request["method"], &method); err != nil || method != "initialize" {
		return "", nil
	}
	idValue := request["id"]
	if len(idValue) == 0 {
		return "", nil
	}
	id, err := normalizeID(idValue)
	if err != nil {
		return "", fmt.Errorf("decode initialize ID: %w", err)
	}
	return id, nil
}

func normalizeID(raw json.RawMessage) (string, error) {
	decoder := json.NewDecoder(bytes.NewReader(raw))
	decoder.UseNumber()
	var value any
	if err := decoder.Decode(&value); err != nil {
		return "", err
	}
	normalized, err := json.Marshal(value)
	if err != nil {
		return "", err
	}
	return string(normalized), nil
}

func (s *Session) captureProtocolVersion(message json.RawMessage, initializeID string) error {
	if initializeID == "" {
		return nil
	}
	var response struct {
		ID     json.RawMessage `json:"id"`
		Result json.RawMessage `json:"result"`
	}
	if err := json.Unmarshal(message, &response); err != nil {
		return nil
	}
	if len(response.ID) == 0 || len(response.Result) == 0 {
		return nil
	}
	id, err := normalizeID(response.ID)
	if err != nil {
		return nil
	}

	if id != initializeID {
		return nil
	}

	var result struct {
		ProtocolVersion string `json:"protocolVersion"`
	}
	if err := json.Unmarshal(response.Result, &result); err != nil || result.ProtocolVersion == "" {
		return nil
	}
	s.mu.Lock()
	s.protocolVersion = result.ProtocolVersion
	s.mu.Unlock()
	return nil
}

func (s *Session) handleMessage(ctx context.Context, message json.RawMessage, initializeID string) error {
	if err := s.captureProtocolVersion(message, initializeID); err != nil {
		return fmt.Errorf("inspect upstream response: %w", err)
	}
	if err := s.emitMessage(ctx, message); err != nil {
		return err
	}
	s.startLegacyListener()
	return nil
}

func (s *Session) startLegacyListener() {
	if !s.legacyListener {
		return
	}
	s.mu.RLock()
	protocolVersion := s.protocolVersion
	s.mu.RUnlock()
	if protocolVersion == "" || protocolVersion >= legacyListenerProtocolCutoff {
		return
	}

	s.listenerOnce.Do(func() {
		s.listenerWG.Add(1)
		go func() {
			defer s.listenerWG.Done()
			if err := s.runLegacyListener(); err != nil && s.ctx.Err() == nil {
				_ = s.emitEvent(s.ctx, Event{Err: err})
			}
		}()
	})
}

// Only the standalone GET is retried. Send and its POST body are never replayed.
func (s *Session) runLegacyListener() error {
	lastEventID := ""
	retries := 0
	for {
		connectedFor, err := s.listenLegacy(&lastEventID)
		if err == nil || s.ctx.Err() != nil {
			return err
		}
		if !recoverableListenerDrop(err) {
			return err
		}
		// A healthy long-lived stream earns a fresh recovery budget. Rapid
		// open/close cycles cannot keep a broken listener alive indefinitely.
		if connectedFor >= listenerHealthyPeriod {
			retries = 0
		}
		if retries == listenerMaxReconnects {
			return fmt.Errorf("upstream GET reconnect budget exhausted: %w", err)
		}
		timer := time.NewTimer(listenerRetryDelay << retries)
		retries++
		select {
		case <-timer.C:
		case <-s.ctx.Done():
			timer.Stop()
			return s.ctx.Err()
		}
	}
}

func recoverableListenerDrop(err error) bool {
	if errors.Is(err, io.EOF) || errors.Is(err, io.ErrUnexpectedEOF) ||
		errors.Is(err, syscall.ECONNRESET) || errors.Is(err, syscall.EPIPE) {
		return true
	}
	var networkError net.Error
	return errors.As(err, &networkError) && networkError.Timeout()
}

func (s *Session) listenLegacy(lastEventID *string) (time.Duration, error) {
	request, err := http.NewRequestWithContext(s.ctx, http.MethodGet, s.endpoint, nil)
	if err != nil {
		return 0, fmt.Errorf("create upstream GET: %w", err)
	}
	request.Header.Set("Accept", "text/event-stream")
	if *lastEventID != "" {
		request.Header.Set("Last-Event-ID", *lastEventID)
	}
	s.applySessionHeaders(request.Header)
	s.applyAccessHeaders(request.Header)
	response, err := s.client.Do(request)
	if err != nil {
		return 0, fmt.Errorf("send upstream GET: %w", err)
	}
	defer response.Body.Close()

	if response.StatusCode == http.StatusMethodNotAllowed {
		return 0, nil
	}
	if response.StatusCode != http.StatusOK {
		return 0, fmt.Errorf("upstream GET returned status %d", response.StatusCode)
	}
	contentType, _, err := mime.ParseMediaType(response.Header.Get("Content-Type"))
	if err != nil || contentType != "text/event-stream" {
		return 0, fmt.Errorf("unsupported upstream GET content type %q", response.Header.Get("Content-Type"))
	}
	connected := time.Now()
	err = readSSEWithCursor(response.Body, lastEventID, func(message json.RawMessage) error {
		return s.emitMessage(s.ctx, message)
	})
	connectedFor := time.Since(connected)
	if err != nil {
		return connectedFor, fmt.Errorf("read upstream GET SSE: %w", err)
	}
	return connectedFor, io.EOF
}

func (s *Session) emitMessage(ctx context.Context, message json.RawMessage) error {
	copied := append(json.RawMessage(nil), message...)
	return s.emitEvent(ctx, Event{Message: copied})
}

func (s *Session) emitEvent(ctx context.Context, event Event) error {
	select {
	case s.events <- event:
		return nil
	case <-ctx.Done():
		return ctx.Err()
	}
}

func (s *Session) deleteSession(ctx context.Context) error {
	s.mu.RLock()
	sessionID := s.sessionID
	s.mu.RUnlock()
	if sessionID == "" {
		return nil
	}

	request, err := http.NewRequestWithContext(ctx, http.MethodDelete, s.endpoint, nil)
	if err != nil {
		return fmt.Errorf("create upstream DELETE: %w", err)
	}
	s.applySessionHeaders(request.Header)
	s.applyAccessHeaders(request.Header)
	response, err := s.client.Do(request)
	if err != nil {
		return fmt.Errorf("send upstream DELETE: %w", err)
	}
	defer response.Body.Close()

	if response.StatusCode == http.StatusNotFound || response.StatusCode == http.StatusMethodNotAllowed {
		return nil
	}
	if response.StatusCode < http.StatusOK || response.StatusCode >= http.StatusMultipleChoices {
		return fmt.Errorf("upstream DELETE returned status %d", response.StatusCode)
	}
	return nil
}
