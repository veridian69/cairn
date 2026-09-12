package upstream

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"github.com/veridian69/cairn/cairn-mcp/internal/credentials"
)

type fixedAccess struct{ value credentials.AccessCredentials }

func (f fixedAccess) AccessSnapshot() credentials.AccessCredentials { return f.value }

type rotatingAccess struct {
	mu    sync.RWMutex
	value credentials.AccessCredentials
}

func (r *rotatingAccess) AccessSnapshot() credentials.AccessCredentials {
	r.mu.RLock()
	defer r.mu.RUnlock()
	return r.value
}

func (r *rotatingAccess) set(value credentials.AccessCredentials) {
	r.mu.Lock()
	defer r.mu.Unlock()
	r.value = value
}

func TestSessionTracksInitialiseMetadata(t *testing.T) {
	type requestMetadata struct {
		method          string
		contentType     string
		accept          string
		clientID        string
		clientSecret    string
		sessionID       string
		protocolVersion string
	}
	requests := make(chan requestMetadata, 2)
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Method == http.MethodDelete {
			w.WriteHeader(http.StatusNoContent)
			return
		}
		requests <- requestMetadata{
			method:          r.Method,
			contentType:     r.Header.Get("Content-Type"),
			accept:          r.Header.Get("Accept"),
			clientID:        r.Header.Get("CF-Access-Client-Id"),
			clientSecret:    r.Header.Get("CF-Access-Client-Secret"),
			sessionID:       r.Header.Get("Mcp-Session-Id"),
			protocolVersion: r.Header.Get("Mcp-Protocol-Version"),
		}
		w.Header().Set("Content-Type", "application/json; charset=utf-8")
		w.Header().Set("Mcp-Session-Id", "session-1")
		if r.Header.Get("Mcp-Session-Id") == "" {
			_, _ = io.WriteString(w, `{"jsonrpc":"2.0","id":1,"result":{"protocolVersion":"2026-07-28"}}`)
			return
		}
		_, _ = io.WriteString(w, `{"jsonrpc":"2.0","id":2,"result":{}}`)
	}))
	defer server.Close()

	s, err := NewSession(server.URL, server.Client(), fixedAccess{credentials.AccessCredentials{ClientID: "id", ClientSecret: "secret"}})
	if err != nil {
		t.Fatal(err)
	}
	defer func() { _ = s.Close(context.Background()) }()

	initialize := json.RawMessage(`{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2026-07-28"}}`)
	if err := s.Send(context.Background(), initialize); err != nil {
		t.Fatal(err)
	}
	if got := receiveEvent(t, s.Events()); got.Err != nil || string(got.Message) != `{"jsonrpc":"2.0","id":1,"result":{"protocolVersion":"2026-07-28"}}` {
		t.Fatalf("initialise event = %#v", got)
	}
	if err := s.Send(context.Background(), json.RawMessage(`{"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}}`)); err != nil {
		t.Fatal(err)
	}
	if got := receiveEvent(t, s.Events()); got.Err != nil || string(got.Message) != `{"jsonrpc":"2.0","id":2,"result":{}}` {
		t.Fatalf("tools/list event = %#v", got)
	}

	first, second := <-requests, <-requests
	if first.method != http.MethodPost || first.contentType != "application/json" || first.accept != "application/json, text/event-stream" {
		t.Fatalf("initial request metadata = %#v", first)
	}
	if first.clientID != "id" || first.clientSecret != "secret" {
		t.Fatalf("Cloudflare access headers = %#v", first)
	}
	if first.sessionID != "" || first.protocolVersion != "" {
		t.Fatalf("initial MCP headers = %#v", first)
	}
	if second.sessionID != "session-1" || second.protocolVersion != "2026-07-28" {
		t.Fatalf("later MCP headers = %#v", second)
	}
}

func TestSessionDoesNotFollowRedirects(t *testing.T) {
	var targetCalls atomic.Int32
	target := httptest.NewServer(http.HandlerFunc(func(http.ResponseWriter, *http.Request) {
		targetCalls.Add(1)
	}))
	defer target.Close()

	origin := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Header.Get("CF-Access-Client-Id") != "id" || r.Header.Get("CF-Access-Client-Secret") != "secret" {
			t.Error("initial request is missing Cloudflare credentials")
		}
		http.Redirect(w, r, target.URL, http.StatusTemporaryRedirect)
	}))
	defer origin.Close()

	client := origin.Client()
	s, err := NewSession(origin.URL, client, fixedAccess{credentials.AccessCredentials{ClientID: "id", ClientSecret: "secret"}})
	if err != nil {
		t.Fatal(err)
	}
	defer func() { _ = s.Close(context.Background()) }()
	err = s.Send(context.Background(), json.RawMessage(`{"jsonrpc":"2.0","id":1,"method":"ping"}`))
	if err == nil || !strings.Contains(err.Error(), "status 307") {
		t.Fatalf("Send() error = %v, want unsupported status 307", err)
	}
	if got := targetCalls.Load(); got != 0 {
		t.Fatalf("redirect target calls = %d, want 0", got)
	}
	if client.CheckRedirect != nil {
		t.Fatal("NewSession mutated caller's redirect policy")
	}
}

func TestSessionAcceptedProducesNoEvent(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusAccepted)
	}))
	defer server.Close()

	s := newTestSession(t, server)
	if err := s.Send(context.Background(), json.RawMessage(`{"jsonrpc":"2.0","method":"notifications/initialized"}`)); err != nil {
		t.Fatal(err)
	}
	select {
	case event := <-s.Events():
		t.Fatalf("unexpected event: %#v", event)
	case <-time.After(25 * time.Millisecond):
	}
}

func TestSessionRejectsInvalidOutboundJSONBeforeRequest(t *testing.T) {
	var calls atomic.Int32
	server := httptest.NewServer(http.HandlerFunc(func(http.ResponseWriter, *http.Request) {
		calls.Add(1)
	}))
	defer server.Close()

	s := newTestSession(t, server)
	if err := s.Send(context.Background(), json.RawMessage(`{"jsonrpc":`)); err == nil {
		t.Fatal("Send() error = nil, want invalid JSON error")
	}
	if got := calls.Load(); got != 0 {
		t.Fatalf("upstream calls = %d, want 0", got)
	}
}

func TestSessionForwardsValidOutboundJSONWithoutInterpretingIt(t *testing.T) {
	bodies := make(chan string, 1)
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		body, err := io.ReadAll(r.Body)
		if err != nil {
			t.Error(err)
		}
		bodies <- string(body)
		w.WriteHeader(http.StatusAccepted)
	}))
	defer server.Close()

	s := newTestSession(t, server)
	message := json.RawMessage(`[{"jsonrpc":"2.0","method":"notifications/custom"}]`)
	if err := s.Send(context.Background(), message); err != nil {
		t.Fatal(err)
	}
	if got := <-bodies; got != string(message) {
		t.Fatalf("upstream body = %q, want %q", got, message)
	}
}

func TestSessionRejectsUnsupportedResponses(t *testing.T) {
	tests := []struct {
		name        string
		status      int
		contentType string
		body        string
		wantError   string
	}{
		{name: "status", status: http.StatusCreated, contentType: "application/json", body: `{}`, wantError: "status 201"},
		{name: "content type", status: http.StatusOK, contentType: "application/octet-stream", body: `{}`, wantError: "content type"},
		{name: "invalid JSON", status: http.StatusOK, contentType: "application/json", body: `{`, wantError: "invalid JSON"},
	}
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
				w.Header().Set("Content-Type", test.contentType)
				w.WriteHeader(test.status)
				_, _ = io.WriteString(w, test.body)
			}))
			defer server.Close()

			s := newTestSession(t, server)
			err := s.Send(context.Background(), json.RawMessage(`{"jsonrpc":"2.0","id":1,"method":"ping"}`))
			if err == nil || !strings.Contains(err.Error(), test.wantError) {
				t.Fatalf("Send() error = %v, want containing %q", err, test.wantError)
			}
		})
	}
}

func TestSessionRejectedResponseCannotInstallSessionID(t *testing.T) {
	tests := []struct {
		name        string
		status      int
		contentType string
		body        string
	}{
		{name: "status", status: http.StatusCreated, contentType: "application/json", body: `{}`},
		{name: "content type", status: http.StatusOK, contentType: "application/octet-stream", body: `{}`},
		{name: "body", status: http.StatusOK, contentType: "application/json", body: `{`},
	}
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			var posts atomic.Int32
			var deletes atomic.Int32
			server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				if r.Method == http.MethodDelete {
					deletes.Add(1)
					w.WriteHeader(http.StatusNoContent)
					return
				}
				if posts.Add(1) == 1 {
					w.Header().Set("Mcp-Session-Id", "poison")
					w.Header().Set("Content-Type", test.contentType)
					w.WriteHeader(test.status)
					_, _ = io.WriteString(w, test.body)
					return
				}
				if got := r.Header.Get("Mcp-Session-Id"); got != "" {
					t.Errorf("later request session ID = %q, want empty", got)
				}
				w.WriteHeader(http.StatusAccepted)
			}))
			defer server.Close()

			s := newTestSession(t, server)
			message := json.RawMessage(`{"jsonrpc":"2.0","id":1,"method":"ping"}`)
			if err := s.Send(context.Background(), message); err == nil {
				t.Fatal("first Send() error = nil, want rejected response")
			}
			if err := s.Send(context.Background(), message); err != nil {
				t.Fatalf("second Send() error = %v", err)
			}
			if err := s.Close(context.Background()); err != nil {
				t.Fatal(err)
			}
			if got := deletes.Load(); got != 0 {
				t.Fatalf("cleanup DELETE calls = %d, want 0", got)
			}
		})
	}
}

func TestSessionFailedInitialiseCannotPoisonReusedID(t *testing.T) {
	var posts atomic.Int32
	protocolHeaders := make(chan string, 1)
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch posts.Add(1) {
		case 1:
			w.WriteHeader(http.StatusBadGateway)
		case 2:
			w.Header().Set("Content-Type", "application/json")
			_, _ = io.WriteString(w, `{"jsonrpc":"2.0","id":7,"result":{"protocolVersion":"poison"}}`)
		case 3:
			protocolHeaders <- r.Header.Get("Mcp-Protocol-Version")
			w.WriteHeader(http.StatusAccepted)
		}
	}))
	defer server.Close()

	s := newTestSession(t, server)
	if err := s.Send(context.Background(), json.RawMessage(`{"jsonrpc":"2.0","id":7,"method":"initialize"}`)); err == nil {
		t.Fatal("initialize Send() error = nil, want rejected response")
	}
	if err := s.Send(context.Background(), json.RawMessage(`{"jsonrpc":"2.0","id":7,"method":"custom/reused"}`)); err != nil {
		t.Fatal(err)
	}
	_ = receiveEvent(t, s.Events())
	if err := s.Send(context.Background(), json.RawMessage(`{"jsonrpc":"2.0","id":8,"method":"tools/list"}`)); err != nil {
		t.Fatal(err)
	}
	if got := <-protocolHeaders; got != "" {
		t.Fatalf("later protocol version = %q, want empty", got)
	}
}

func TestSessionForwardsJSONWithoutInterpretingResult(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_, _ = io.WriteString(w, `{"jsonrpc":"2.0","id":1,"result":true}`)
	}))
	defer server.Close()

	s := newTestSession(t, server)
	if err := s.Send(context.Background(), json.RawMessage(`{"jsonrpc":"2.0","id":1,"method":"custom/raw"}`)); err != nil {
		t.Fatal(err)
	}
	if got := receiveEvent(t, s.Events()); got.Err != nil || string(got.Message) != `{"jsonrpc":"2.0","id":1,"result":true}` {
		t.Fatalf("raw response event = %#v", got)
	}
}

func TestSessionAcceptsExactFourMiBJSONResponse(t *testing.T) {
	const messageSize = 4 << 20
	response := `"` + strings.Repeat("x", messageSize-2) + `"`
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_, _ = io.WriteString(w, response)
	}))
	defer server.Close()

	s := newTestSession(t, server)
	if err := s.Send(context.Background(), json.RawMessage(`{"jsonrpc":"2.0","id":1,"method":"ping"}`)); err != nil {
		t.Fatal(err)
	}
	if got := receiveEvent(t, s.Events()); got.Err != nil || len(got.Message) != messageSize {
		t.Fatalf("response event = error %v, size %d; want size %d", got.Err, len(got.Message), messageSize)
	}
}

func TestSessionRejectsJSONResponseOverFourMiB(t *testing.T) {
	const messageSize = 4 << 20
	response := `"` + strings.Repeat("x", messageSize-1) + `"`
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_, _ = io.WriteString(w, response)
	}))
	defer server.Close()

	s := newTestSession(t, server)
	err := s.Send(context.Background(), json.RawMessage(`{"jsonrpc":"2.0","id":1,"method":"ping"}`))
	if err == nil || !strings.Contains(err.Error(), "exceeds 4 MiB") {
		t.Fatalf("Send() error = %v, want 4 MiB limit error", err)
	}
	select {
	case event := <-s.Events():
		t.Fatalf("oversized response emitted event: %#v", event)
	default:
	}
}

func TestSessionSnapshotsRotatedCredentialsForEveryRequest(t *testing.T) {
	type accessPair struct{ id, secret string }
	requests := make(chan accessPair, 2)
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		requests <- accessPair{r.Header.Get("CF-Access-Client-Id"), r.Header.Get("CF-Access-Client-Secret")}
		w.WriteHeader(http.StatusAccepted)
	}))
	defer server.Close()

	source := &rotatingAccess{value: credentials.AccessCredentials{ClientID: "first-id", ClientSecret: "first-secret"}}
	s, err := NewSession(server.URL, server.Client(), source)
	if err != nil {
		t.Fatal(err)
	}
	defer func() { _ = s.Close(context.Background()) }()

	message := json.RawMessage(`{"jsonrpc":"2.0","method":"notifications/progress"}`)
	if err := s.Send(context.Background(), message); err != nil {
		t.Fatal(err)
	}
	source.set(credentials.AccessCredentials{ClientID: "second-id", ClientSecret: "second-secret"})
	if err := s.Send(context.Background(), message); err != nil {
		t.Fatal(err)
	}
	if got := <-requests; got != (accessPair{"first-id", "first-secret"}) {
		t.Fatalf("first credentials = %#v", got)
	}
	if got := <-requests; got != (accessPair{"second-id", "second-secret"}) {
		t.Fatalf("second credentials = %#v", got)
	}
}

func TestSessionCloseDeletesOnceAndAlwaysClosesEvents(t *testing.T) {
	tests := []struct {
		status    int
		wantError bool
	}{
		{status: http.StatusNoContent},
		{status: http.StatusNotFound},
		{status: http.StatusMethodNotAllowed},
		{status: http.StatusBadGateway, wantError: true},
	}
	for _, test := range tests {
		t.Run(fmt.Sprintf("status_%d", test.status), func(t *testing.T) {
			type deleteMetadata struct {
				clientID, clientSecret, sessionID, protocolVersion string
			}
			deletes := make(chan deleteMetadata, 2)
			server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				if r.Method == http.MethodDelete {
					deletes <- deleteMetadata{
						r.Header.Get("CF-Access-Client-Id"),
						r.Header.Get("CF-Access-Client-Secret"),
						r.Header.Get("Mcp-Session-Id"),
						r.Header.Get("Mcp-Protocol-Version"),
					}
					w.WriteHeader(test.status)
					return
				}
				w.Header().Set("Content-Type", "application/json")
				w.Header().Set("Mcp-Session-Id", "session-close")
				_, _ = io.WriteString(w, `{"jsonrpc":"2.0","id":"init","result":{"protocolVersion":"2026-07-28"}}`)
			}))
			defer server.Close()

			s, err := NewSession(server.URL, server.Client(), fixedAccess{credentials.AccessCredentials{ClientID: "id", ClientSecret: "secret"}})
			if err != nil {
				t.Fatal(err)
			}
			if err := s.Send(context.Background(), json.RawMessage(`{"jsonrpc":"2.0","id":"init","method":"initialize"}`)); err != nil {
				t.Fatal(err)
			}
			_ = receiveEvent(t, s.Events())

			firstErr := s.Close(context.Background())
			if (firstErr != nil) != test.wantError {
				t.Fatalf("Close() error = %v, wantError %t", firstErr, test.wantError)
			}
			secondErr := s.Close(context.Background())
			if fmt.Sprint(secondErr) != fmt.Sprint(firstErr) {
				t.Fatalf("second Close() error = %v, first = %v", secondErr, firstErr)
			}
			got := <-deletes
			if got != (deleteMetadata{"id", "secret", "session-close", "2026-07-28"}) {
				t.Fatalf("DELETE metadata = %#v", got)
			}
			select {
			case extra := <-deletes:
				t.Fatalf("unexpected second DELETE: %#v", extra)
			default:
			}
			if _, ok := <-s.Events(); ok {
				t.Fatal("Events channel remains open after Close")
			}
		})
	}
}

func TestSessionPostSSEEmitsEveryDataEvent(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "text/event-stream; charset=utf-8")
		_, _ = io.WriteString(w, ": keepalive\n\ndata: {\"jsonrpc\":\"2.0\",\"method\":\"notifications/tools/list_changed\"}\n\ndata: {\"jsonrpc\":\"2.0\",\"id\":2,\"result\":{}}\n\n")
	}))
	defer server.Close()

	s := newTestSession(t, server)
	if err := s.Send(context.Background(), json.RawMessage(`{"jsonrpc":"2.0","id":2,"method":"tools/list"}`)); err != nil {
		t.Fatal(err)
	}
	want := []string{
		`{"jsonrpc":"2.0","method":"notifications/tools/list_changed"}`,
		`{"jsonrpc":"2.0","id":2,"result":{}}`,
	}
	for i := range want {
		got := receiveEvent(t, s.Events())
		if got.Err != nil || string(got.Message) != want[i] {
			t.Fatalf("event %d = %#v, want message %s", i, got, want[i])
		}
	}
}

func TestSessionLegacyListenerUsesCurrentMetadataOnceAndAccepts405(t *testing.T) {
	type getMetadata struct {
		accept, clientID, clientSecret, sessionID, protocolVersion string
	}
	gets := make(chan getMetadata, 2)
	var getCalls atomic.Int32
	source := &rotatingAccess{value: credentials.AccessCredentials{ClientID: "post-id", ClientSecret: "post-secret"}}
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch r.Method {
		case http.MethodPost:
			source.set(credentials.AccessCredentials{ClientID: "get-id", ClientSecret: "get-secret"})
			w.Header().Set("Content-Type", "application/json")
			w.Header().Set("Mcp-Session-Id", "legacy-session")
			_, _ = io.WriteString(w, `{"jsonrpc":"2.0","id":1,"result":{"protocolVersion":"2025-06-18"}}`)
		case http.MethodGet:
			getCalls.Add(1)
			gets <- getMetadata{
				r.Header.Get("Accept"),
				r.Header.Get("CF-Access-Client-Id"),
				r.Header.Get("CF-Access-Client-Secret"),
				r.Header.Get("Mcp-Session-Id"),
				r.Header.Get("Mcp-Protocol-Version"),
			}
			w.WriteHeader(http.StatusMethodNotAllowed)
		case http.MethodDelete:
			w.WriteHeader(http.StatusNoContent)
		}
	}))
	defer server.Close()

	s, err := NewSession(server.URL, server.Client(), source)
	if err != nil {
		t.Fatal(err)
	}
	defer func() { _ = s.Close(context.Background()) }()
	initialize := json.RawMessage(`{"jsonrpc":"2.0","id":1,"method":"initialize"}`)
	for i := 0; i < 2; i++ {
		if err := s.Send(context.Background(), initialize); err != nil {
			t.Fatal(err)
		}
		if got := receiveEvent(t, s.Events()); got.Err != nil {
			t.Fatalf("initialise event = %#v", got)
		}
	}

	select {
	case got := <-gets:
		want := getMetadata{"text/event-stream", "get-id", "get-secret", "legacy-session", "2025-06-18"}
		if got != want {
			t.Fatalf("GET metadata = %#v, want %#v", got, want)
		}
	case <-time.After(time.Second):
		t.Fatal("timed out waiting for legacy GET")
	}
	time.Sleep(25 * time.Millisecond)
	if got := getCalls.Load(); got != 1 {
		t.Fatalf("legacy GET calls = %d, want 1", got)
	}
	select {
	case event := <-s.Events():
		t.Fatalf("405 produced event: %#v", event)
	default:
	}
}

func TestSessionOptionDisablesLegacyListener(t *testing.T) {
	var getCalls atomic.Int32
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch r.Method {
		case http.MethodPost:
			w.Header().Set("Content-Type", "application/json")
			_, _ = io.WriteString(w, `{"jsonrpc":"2.0","id":1,"result":{"protocolVersion":"2025-06-18"}}`)
		case http.MethodGet:
			getCalls.Add(1)
			w.WriteHeader(http.StatusMethodNotAllowed)
		}
	}))
	defer server.Close()

	session, err := NewSession(server.URL, server.Client(), fixedAccess{}, WithoutLegacyListener())
	if err != nil {
		t.Fatal(err)
	}
	if err := session.Send(context.Background(), json.RawMessage(`{"jsonrpc":"2.0","id":1,"method":"initialize"}`)); err != nil {
		t.Fatal(err)
	}
	if event := receiveEvent(t, session.Events()); event.Err != nil {
		t.Fatal(event.Err)
	}
	if err := session.Close(context.Background()); err != nil {
		t.Fatal(err)
	}
	if calls := getCalls.Load(); calls != 0 {
		t.Fatalf("legacy GET calls = %d, want 0", calls)
	}
}

func TestSessionLegacyListenerEmitsSSEMessages(t *testing.T) {
	const message = `{"jsonrpc":"2.0","method":"notifications/resources/list_changed","params":{}}`
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch r.Method {
		case http.MethodPost:
			w.Header().Set("Content-Type", "application/json")
			w.Header().Set("Mcp-Session-Id", "legacy-session")
			_, _ = io.WriteString(w, `{"jsonrpc":"2.0","id":1,"result":{"protocolVersion":"2025-06-18"}}`)
		case http.MethodGet:
			if got := r.Header.Get("Accept"); got != "text/event-stream" {
				t.Errorf("GET Accept = %q, want text/event-stream", got)
			}
			if got := r.Header.Get("CF-Access-Client-Id"); got != "client-id" {
				t.Errorf("GET client ID = %q, want client-id", got)
			}
			if got := r.Header.Get("CF-Access-Client-Secret"); got != "client-secret" {
				t.Errorf("GET client secret = %q, want client-secret", got)
			}
			if got := r.Header.Get("Mcp-Session-Id"); got != "legacy-session" {
				t.Errorf("GET session ID = %q, want legacy-session", got)
			}
			if got := r.Header.Get("Mcp-Protocol-Version"); got != "2025-06-18" {
				t.Errorf("GET protocol version = %q, want 2025-06-18", got)
			}
			w.Header().Set("Content-Type", "text/event-stream")
			_, _ = fmt.Fprintf(w, "data: %s\n\n", message)
		case http.MethodDelete:
			w.WriteHeader(http.StatusNoContent)
		}
	}))
	defer server.Close()

	s, err := NewSession(server.URL, server.Client(), fixedAccess{credentials.AccessCredentials{
		ClientID:     "client-id",
		ClientSecret: "client-secret",
	}})
	if err != nil {
		t.Fatal(err)
	}
	defer func() { _ = s.Close(context.Background()) }()
	if err := s.Send(context.Background(), json.RawMessage(`{"jsonrpc":"2.0","id":1,"method":"initialize"}`)); err != nil {
		t.Fatal(err)
	}
	if got := receiveEvent(t, s.Events()); got.Err != nil {
		t.Fatalf("initialise event = %#v", got)
	}
	got := receiveEvent(t, s.Events())
	if got.Err != nil || string(got.Message) != message {
		t.Fatalf("standalone GET event = %#v, want message %s", got, message)
	}
}

func TestSessionCurrentProtocolDoesNotOpenLegacyListener(t *testing.T) {
	for _, version := range []string{"2026-07-28", "2026-11-25"} {
		t.Run(version, func(t *testing.T) {
			var getCalls atomic.Int32
			server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				if r.Method == http.MethodGet {
					getCalls.Add(1)
					w.WriteHeader(http.StatusMethodNotAllowed)
					return
				}
				w.Header().Set("Content-Type", "application/json")
				_, _ = fmt.Fprintf(w, `{"jsonrpc":"2.0","id":1,"result":{"protocolVersion":%q}}`, version)
			}))
			defer server.Close()

			s := newTestSession(t, server)
			if err := s.Send(context.Background(), json.RawMessage(`{"jsonrpc":"2.0","id":1,"method":"initialize"}`)); err != nil {
				t.Fatal(err)
			}
			_ = receiveEvent(t, s.Events())
			time.Sleep(25 * time.Millisecond)
			if got := getCalls.Load(); got != 0 {
				t.Fatalf("legacy GET calls = %d, want 0", got)
			}
		})
	}
}

func TestSessionLegacyListenerEmitsOneTerminalError(t *testing.T) {
	tests := []struct {
		name      string
		get       func() (*http.Response, error)
		wantError string
	}{
		{
			name: "request failure",
			get: func() (*http.Response, error) {
				return nil, errors.New("listener unavailable")
			},
			wantError: "send upstream GET",
		},
		{
			name: "unexpected content type",
			get: func() (*http.Response, error) {
				return testHTTPResponse(http.StatusOK, "application/json", `{}`), nil
			},
			wantError: "content type",
		},
		{
			name: "server error",
			get: func() (*http.Response, error) {
				return testHTTPResponse(http.StatusBadGateway, "text/event-stream", ""), nil
			},
			wantError: "status 502",
		},
		{
			name: "malformed SSE",
			get: func() (*http.Response, error) {
				return testHTTPResponse(http.StatusOK, "text/event-stream", "data: {broken}\n\n"), nil
			},
			wantError: "invalid JSON",
		},
	}
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			client := &http.Client{Transport: roundTripFunc(func(r *http.Request) (*http.Response, error) {
				switch r.Method {
				case http.MethodPost:
					response := testHTTPResponse(http.StatusOK, "application/json", `{"jsonrpc":"2.0","id":1,"result":{"protocolVersion":"2025-06-18"}}`)
					response.Header.Set("Mcp-Session-Id", "legacy-session")
					return response, nil
				case http.MethodGet:
					return test.get()
				case http.MethodDelete:
					return testHTTPResponse(http.StatusNoContent, "", ""), nil
				default:
					return nil, fmt.Errorf("unexpected method %s", r.Method)
				}
			})}
			s, err := NewSession("http://upstream.test/mcp", client, fixedAccess{})
			if err != nil {
				t.Fatal(err)
			}
			defer func() { _ = s.Close(context.Background()) }()

			if err := s.Send(context.Background(), json.RawMessage(`{"jsonrpc":"2.0","id":1,"method":"initialize"}`)); err != nil {
				t.Fatal(err)
			}
			if got := receiveEvent(t, s.Events()); got.Err != nil {
				t.Fatalf("initialise event = %#v", got)
			}
			got := receiveEvent(t, s.Events())
			if got.Err == nil || !strings.Contains(got.Err.Error(), test.wantError) || got.Message != nil {
				t.Fatalf("terminal event = %#v, want error containing %q", got, test.wantError)
			}
			select {
			case extra := <-s.Events():
				t.Fatalf("extra listener event = %#v", extra)
			default:
			}
		})
	}
}

func TestSessionCloseJoinsLegacyListenerBeforeClosingEvents(t *testing.T) {
	getStarted := make(chan struct{}, 1)
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch r.Method {
		case http.MethodPost:
			w.Header().Set("Content-Type", "application/json")
			_, _ = io.WriteString(w, `{"jsonrpc":"2.0","id":1,"result":{"protocolVersion":"2025-06-18"}}`)
		case http.MethodGet:
			w.Header().Set("Content-Type", "text/event-stream")
			w.WriteHeader(http.StatusOK)
			w.(http.Flusher).Flush()
			getStarted <- struct{}{}
			<-r.Context().Done()
		}
	}))
	defer server.Close()

	s := newTestSession(t, server)
	if err := s.Send(context.Background(), json.RawMessage(`{"jsonrpc":"2.0","id":1,"method":"initialize"}`)); err != nil {
		t.Fatal(err)
	}
	_ = receiveEvent(t, s.Events())
	select {
	case <-getStarted:
	case <-time.After(time.Second):
		t.Fatal("timed out waiting for legacy listener")
	}
	if err := s.Close(context.Background()); err != nil {
		t.Fatal(err)
	}
	if _, ok := <-s.Events(); ok {
		t.Fatal("Events channel remains open after Close")
	}
}

type roundTripFunc func(*http.Request) (*http.Response, error)

func (f roundTripFunc) RoundTrip(request *http.Request) (*http.Response, error) {
	return f(request)
}

func testHTTPResponse(status int, contentType, body string) *http.Response {
	header := make(http.Header)
	if contentType != "" {
		header.Set("Content-Type", contentType)
	}
	return &http.Response{
		StatusCode: status,
		Header:     header,
		Body:       io.NopCloser(strings.NewReader(body)),
	}
}

func newTestSession(t *testing.T, server *httptest.Server) *Session {
	t.Helper()
	s, err := NewSession(server.URL, server.Client(), fixedAccess{})
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = s.Close(context.Background()) })
	return s
}

func receiveEvent(t *testing.T, events <-chan Event) Event {
	t.Helper()
	select {
	case event, ok := <-events:
		if !ok {
			t.Fatal("Events channel closed before event")
		}
		return event
	case <-time.After(time.Second):
		t.Fatal("timed out waiting for event")
		return Event{}
	}
}
