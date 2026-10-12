package attention

import (
	"context"
	"encoding/json"
	"errors"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"
)

func TestOpenCodeDeliversAttributedInputToSelectedIdleSession(t *testing.T) {
	var submitted map[string]any
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Header.Get("Authorization") != "Basic b3BlbmNvZGU6dGVzdA==" {
			t.Error("host credential missing")
		}
		switch r.URL.Path {
		case "/session/ses_target/message/msg_garden_message1":
			if submitted == nil {
				w.WriteHeader(http.StatusNotFound)
				return
			}
			_ = json.NewEncoder(w).Encode(map[string]any{"info": map[string]string{"id": "msg_garden_message1", "role": "user", "sessionID": "ses_target"}, "parts": submitted["parts"]})
		case "/session/status":
			_, _ = w.Write([]byte(`{"ses_target":{"type":"idle"}}`))
		case "/session/ses_target/prompt_async":
			if r.Method != http.MethodPost {
				t.Error("prompt was not posted")
			}
			if err := json.NewDecoder(r.Body).Decode(&submitted); err != nil {
				t.Error(err)
			}
			w.WriteHeader(http.StatusNoContent)
		default:
			t.Errorf("unexpected host path %s", r.URL.Path)
			w.WriteHeader(http.StatusNotFound)
		}
	}))
	defer srv.Close()
	host, err := NewOpenCode(srv.URL, "ses_target", "opencode", "test")
	if err != nil {
		t.Fatal(err)
	}
	err = host.Deliver(context.Background(), Event{ID: "message-1", Sender: "Spike", Recipient: "Val", Payload: json.RawMessage(`{"content":"Please review."}`)})
	if err != nil {
		t.Fatal(err)
	}
	parts, ok := submitted["parts"].([]any)
	if !ok || len(parts) != 1 {
		t.Fatalf("missing message parts: %#v", submitted)
	}
	text := parts[0].(map[string]any)["text"].(string)
	for _, want := range []string{"message-1", "Spike", "Val", "Please review.", "not human authorisation"} {
		if !strings.Contains(text, want) {
			t.Errorf("missing %q from attributed input", want)
		}
	}
	if _, ok := submitted["system"]; ok {
		t.Fatal("external message overwrote system instructions")
	}
}

func TestOpenCodeBusyDoesNotSubmitOrAcknowledge(t *testing.T) {
	posts := 0
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if strings.Contains(r.URL.Path, "/message/") {
			w.WriteHeader(http.StatusNotFound)
			return
		}
		if r.Method == http.MethodPost {
			posts++
		}
		_, _ = w.Write([]byte(`{"ses_target":{"type":"busy"}}`))
	}))
	defer srv.Close()
	host, err := NewOpenCode(srv.URL, "ses_target", "", "")
	if err != nil {
		t.Fatal(err)
	}
	if err := host.Deliver(context.Background(), Event{ID: "m", Payload: json.RawMessage(`{}`)}); !errors.Is(err, ErrBusy) {
		t.Fatalf("got %v", err)
	}
	if posts != 0 {
		t.Fatal("submitted into busy session")
	}
}

func TestOpenCodeRedirectNeverForwardsHostCredential(t *testing.T) {
	contacted := false
	target := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { contacted = true }))
	defer target.Close()
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		http.Redirect(w, r, target.URL, http.StatusTemporaryRedirect)
	}))
	defer srv.Close()
	host, err := NewOpenCode(srv.URL, "ses_target", "opencode", "secret")
	if err != nil {
		t.Fatal(err)
	}
	if err := host.Deliver(context.Background(), Event{}); err == nil {
		t.Fatal("redirect accepted")
	}
	if contacted {
		t.Fatal("followed credential-bearing redirect")
	}
}

func TestOpenCodeRejectsUnexpectedCompressionBeforeReadingBody(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Header.Get("Accept-Encoding") != "identity" {
			t.Error("compression must be disabled")
		}
		w.Header().Set("Content-Encoding", "gzip")
		_, _ = w.Write([]byte("unread encoded body"))
	}))
	defer srv.Close()
	host, err := NewOpenCode(srv.URL, "ses_target", "", "")
	if err != nil {
		t.Fatal(err)
	}
	if err = host.Deliver(context.Background(), Event{ID: "m", Payload: json.RawMessage(`{}`)}); !errors.Is(err, ErrRejected) {
		t.Fatalf("unexpected encoded response: %v", err)
	}
}

func TestOpenCodeAmbiguousPromptFailureIsNotRetried(t *testing.T) {
	posts := 0
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if strings.Contains(r.URL.Path, "/message/") {
			w.WriteHeader(http.StatusNotFound)
			return
		}
		if r.Method == http.MethodGet {
			_, _ = w.Write([]byte(`{}`))
			return
		}
		posts++
		w.WriteHeader(http.StatusInternalServerError)
		_, _ = w.Write([]byte("secret upstream details"))
	}))
	defer srv.Close()
	host, err := NewOpenCode(srv.URL, "ses_target", "", "")
	if err != nil {
		t.Fatal(err)
	}
	host.confirmTimeout = 20 * time.Millisecond
	err = host.Deliver(context.Background(), Event{ID: "m", Payload: json.RawMessage(`{}`)})
	if !errors.Is(err, ErrUncertain) || strings.Contains(err.Error(), "secret") {
		t.Fatalf("unsafe error: %v", err)
	}
	if posts != 1 {
		t.Fatalf("posted %d times", posts)
	}
}

func TestOpenCodeRequiresReadbackNotOnlyAsync204(t *testing.T) {
	posts := 0
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if strings.Contains(r.URL.Path, "/message/") {
			w.WriteHeader(http.StatusNotFound)
			return
		}
		if r.Method == http.MethodGet {
			_, _ = w.Write([]byte(`{}`))
			return
		}
		posts++
		w.WriteHeader(http.StatusNoContent)
	}))
	defer srv.Close()
	host, err := NewOpenCode(srv.URL, "ses_target", "", "")
	if err != nil {
		t.Fatal(err)
	}
	host.confirmTimeout = 20 * time.Millisecond
	err = host.Deliver(context.Background(), Event{ID: "m", Payload: json.RawMessage(`{}`)})
	if !errors.Is(err, ErrUncertain) {
		t.Fatalf("204 was mistaken for acceptance: %v", err)
	}
	if posts != 1 {
		t.Fatalf("posts=%d", posts)
	}
}
