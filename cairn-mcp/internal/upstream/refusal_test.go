package upstream

import (
	"context"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"
)

// Cairn refuses a frame it will not admit, such as notifications/cancelled, with 400 and a
// JSON-RPC error body. On 7 November 2026 one such refusal ended a Claude Code session's
// bridge: an interrupted recall sent notifications/cancelled, and every later call failed with
// "Connection closed".

const cairnRefusal = `{"jsonrpc":"2.0","id":%s,"error":{"code":-32601,"message":"Method not allowed","data":{"rule":"method_not_allowed"}}}`

func refusingServer(t *testing.T, status int, contentType, body string) *httptest.Server {
	t.Helper()
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", contentType)
		w.Header().Set("Mcp-Session-Id", "refused-session")
		w.WriteHeader(status)
		_, _ = io.WriteString(w, body)
	}))
	t.Cleanup(server.Close)
	return server
}

func TestSessionDropsRefusedNotification(t *testing.T) {
	s := newTestSession(t, refusingServer(t, http.StatusBadRequest, "application/json", strings.Replace(cairnRefusal, "%s", "null", 1)))
	cancelled := `{"jsonrpc":"2.0","method":"notifications/cancelled","params":{"requestId":7,"reason":"interrupted"}}`
	if err := s.Send(context.Background(), json.RawMessage(cancelled)); err != nil {
		t.Fatalf("a refused notification ended the session: %v", err)
	}
	select {
	case event := <-s.Events():
		t.Fatalf("a refused notification produced an event: %#v", event)
	case <-time.After(25 * time.Millisecond):
	}
	if s.sessionID != "" {
		t.Fatalf("a refused response installed session ID %q", s.sessionID)
	}
}

func TestSessionDeliversRefusedRequestAsItsError(t *testing.T) {
	for _, id := range []string{`7`, `"call-7"`} {
		body := strings.Replace(cairnRefusal, "%s", id, 1)
		s := newTestSession(t, refusingServer(t, http.StatusBadRequest, "application/json; charset=utf-8", body))
		err := s.Send(context.Background(), json.RawMessage(`{"jsonrpc":"2.0","id":`+id+`,"method":"tools/call","params":{"name":"x"}}`))
		if err != nil {
			t.Fatalf("id %s: a refused request ended the session: %v", id, err)
		}
		event := receiveEvent(t, s.Events())
		if event.Err != nil || string(event.Message) != body {
			t.Fatalf("id %s: event = %#v, want the refusal body", id, event)
		}
		if s.sessionID != "" {
			t.Fatalf("id %s: a refused response installed session ID %q", id, s.sessionID)
		}
	}
}

func TestSessionKeepsOtherRefusalsTerminal(t *testing.T) {
	request := `{"jsonrpc":"2.0","id":7,"method":"tools/call"}`
	tests := []struct {
		name        string
		status      int
		contentType string
		body        string
	}{
		{"server error", http.StatusInternalServerError, "application/json", strings.Replace(cairnRefusal, "%s", "7", 1)},
		{"html from an access proxy", http.StatusForbidden, "text/html", "<html>denied</html>"},
		{"other id", http.StatusBadRequest, "application/json", strings.Replace(cairnRefusal, "%s", "8", 1)},
		{"id as string", http.StatusBadRequest, "application/json", strings.Replace(cairnRefusal, "%s", `"7"`, 1)},
		{"no error object", http.StatusBadRequest, "application/json", `{"jsonrpc":"2.0","id":7}`},
		{"result and error", http.StatusBadRequest, "application/json", `{"jsonrpc":"2.0","id":7,"result":{},"error":{"code":1,"message":"m"}}`},
		{"error without code", http.StatusBadRequest, "application/json", `{"jsonrpc":"2.0","id":7,"error":{"message":"m"}}`},
		{"invalid JSON", http.StatusBadRequest, "application/json", `{`},
		{"oversized", http.StatusBadRequest, "application/json", `{"jsonrpc":"2.0","id":7,"error":{"code":1,"message":"` + strings.Repeat("x", maxRefusalBytes) + `"}}`},
		{"redirect", http.StatusMultipleChoices, "application/json", strings.Replace(cairnRefusal, "%s", "7", 1)},
	}
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			s := newTestSession(t, refusingServer(t, test.status, test.contentType, test.body))
			err := s.Send(context.Background(), json.RawMessage(request))
			if err == nil || !strings.Contains(err.Error(), "unsupported upstream status") {
				t.Fatalf("Send() error = %v, want terminal", err)
			}
			if s.sessionID != "" {
				t.Fatalf("a refused response installed session ID %q", s.sessionID)
			}
		})
	}
}

// A notification is dropped only when its refusal is a genuine JSON-RPC error (Val's review,
// 8 November 2026): anything else stays terminal, as for a request.
func TestSessionKeepsNonJSONRPCNotificationRefusalsTerminal(t *testing.T) {
	cancelled := `{"jsonrpc":"2.0","method":"notifications/cancelled","params":{"requestId":7}}`
	tests := []struct {
		name        string
		status      int
		contentType string
		body        string
	}{
		{"html from an access proxy", http.StatusForbidden, "text/html", "<html>denied</html>"},
		{"unauthorised without a body", http.StatusUnauthorized, "application/json", ""},
		{"rate limited", http.StatusTooManyRequests, "text/plain", "slow down"},
		{"error for a request id", http.StatusBadRequest, "application/json", strings.Replace(cairnRefusal, "%s", "7", 1)},
		{"error without an id", http.StatusBadRequest, "application/json", `{"jsonrpc":"2.0","error":{"code":-32600,"message":"denied"}}`},
	}
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			s := newTestSession(t, refusingServer(t, test.status, test.contentType, test.body))
			err := s.Send(context.Background(), json.RawMessage(cancelled))
			if err == nil || !strings.Contains(err.Error(), "unsupported upstream status") {
				t.Fatalf("Send() error = %v, want terminal", err)
			}
		})
	}
}
