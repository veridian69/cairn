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
	host, err := NewOpenCode(srv.URL, "ses_target", "opencode", "test", "1")
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
	host, err := NewOpenCode(srv.URL, "ses_target", "", "", "1")
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
	host, err := NewOpenCode(srv.URL, "ses_target", "opencode", "secret", "1")
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
	host, err := NewOpenCode(srv.URL, "ses_target", "", "", "1")
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
	host, err := NewOpenCode(srv.URL, "ses_target", "", "", "1")
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
	host, err := NewOpenCode(srv.URL, "ses_target", "", "", "1")
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

// v2Fake serves the OpenCode 2.x routes with their {"data": ...} envelopes.
// Admitted prompts wait in the inbox, as the real server holds them until
// delivery, and delivered messages move to the message list. Message IDs are
// global across sessions, as they are on the 2.x server, so reuse is a 409.
type v2Fake struct {
	t          *testing.T
	activeBody string
	inbox      map[string]v2Item
	messages   map[string]v2Item
	posts      int
	postStatus int
	postAdmits bool
	postBody   map[string]any
}

type v2Item struct{ session, text string }

func newV2Fake(t *testing.T) *v2Fake {
	return &v2Fake{t: t, activeBody: `{"data":{}}`, inbox: map[string]v2Item{}, messages: map[string]v2Item{}}
}

func (f *v2Fake) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	if r.Header.Get("Authorization") != "Basic b3BlbmNvZGU6dGVzdA==" {
		w.WriteHeader(http.StatusUnauthorized)
		_, _ = w.Write([]byte(`{"_tag":"UnauthorizedError","message":"Authentication required"}`))
		return
	}
	parts := strings.Split(r.URL.Path, "/")
	if r.Method == http.MethodGet && r.URL.Path == "/api/session/active" {
		_, _ = w.Write([]byte(f.activeBody))
		return
	}
	if len(parts) < 5 || parts[1] != "api" || parts[2] != "session" || !strings.HasPrefix(parts[3], "ses") {
		f.t.Errorf("unexpected 2.x request %s %s", r.Method, r.URL.Path)
		w.WriteHeader(http.StatusNotFound)
		return
	}
	session := parts[3]
	switch {
	case r.Method == http.MethodGet && parts[4] == "inbox" && len(parts) == 5:
		items := []map[string]any{}
		for id, item := range f.inbox {
			if item.session == session {
				items = append(items, map[string]any{"id": id, "type": "user", "payload": map[string]string{"text": item.text}})
			}
		}
		_ = json.NewEncoder(w).Encode(map[string]any{"data": items})
	case r.Method == http.MethodGet && parts[4] == "message" && len(parts) == 6:
		id := parts[5]
		item, ok := f.messages[id]
		if !ok || item.session != session {
			w.WriteHeader(http.StatusNotFound)
			_, _ = w.Write([]byte(`{"_tag":"MessageNotFoundError","sessionID":"` + session + `","messageID":"` + id + `","message":"Message not found"}`))
			return
		}
		_ = json.NewEncoder(w).Encode(map[string]any{"data": map[string]any{"id": id, "type": "user", "text": item.text}})
	case r.Method == http.MethodPost && parts[4] == "prompt" && len(parts) == 5:
		f.posts++
		if err := json.NewDecoder(r.Body).Decode(&f.postBody); err != nil {
			f.t.Error(err)
		}
		id, text := f.postBody["id"].(string), f.postBody["text"].(string)
		for _, store := range []map[string]v2Item{f.inbox, f.messages} {
			if item, ok := store[id]; ok && item.session != session {
				w.WriteHeader(http.StatusConflict)
				_, _ = w.Write([]byte(`{"_tag":"ConflictError","message":"Message ID already used"}`))
				return
			}
		}
		if f.postAdmits {
			f.inbox[id] = v2Item{session, text}
		}
		if f.postStatus != 0 {
			w.WriteHeader(f.postStatus)
			return
		}
		_, _ = w.Write([]byte(`{"data":{"id":"` + id + `"}}`))
	default:
		f.t.Errorf("unexpected 2.x request %s %s", r.Method, r.URL.Path)
		w.WriteHeader(http.StatusNotFound)
	}
}

func v2Event(t *testing.T) (Event, string) {
	event := Event{ID: "message-1", Sender: "Spike", Recipient: "Val", Payload: json.RawMessage(`{"content":"Please review."}`)}
	text, err := event.Text()
	if err != nil {
		t.Fatal(err)
	}
	return event, text
}

func TestOpenCodeV2DeliversToIdleSessionAndConfirmsAdmission(t *testing.T) {
	fake := newV2Fake(t)
	fake.postAdmits = true
	srv := httptest.NewServer(fake)
	defer srv.Close()
	host, err := NewOpenCode(srv.URL, "ses_target", "opencode", "test", "2")
	if err != nil {
		t.Fatal(err)
	}
	event, text := v2Event(t)
	if err := host.Deliver(context.Background(), event); err != nil {
		t.Fatal(err)
	}
	if fake.posts != 1 {
		t.Fatalf("posts=%d", fake.posts)
	}
	if fake.postBody["id"] != host.messageID(event.ID) || fake.postBody["text"] != text || fake.postBody["delivery"] != "queue" {
		t.Fatalf("unexpected prompt body: %#v", fake.postBody)
	}
	metadata := fake.postBody["metadata"].(map[string]any)
	if metadata["source"] != "garden" || metadata["trust"] != "external_untrusted" || metadata["message_id"] != "message-1" {
		t.Fatalf("missing Garden provenance: %#v", metadata)
	}
	for _, v1Only := range []string{"messageID", "parts", "resume"} {
		if _, ok := fake.postBody[v1Only]; ok {
			t.Fatalf("prompt carries %q", v1Only)
		}
	}
}

func TestOpenCodeV2SameEventAdmittedInEachRecipientSession(t *testing.T) {
	fake := newV2Fake(t)
	fake.postAdmits = true
	srv := httptest.NewServer(fake)
	defer srv.Close()
	event, _ := v2Event(t)
	hosts := map[string]*OpenCode{}
	for _, session := range []string{"ses_target", "ses_other"} {
		host, err := NewOpenCode(srv.URL, session, "opencode", "test", "2")
		if err != nil {
			t.Fatal(err)
		}
		hosts[session] = host
		if err := host.Deliver(context.Background(), event); err != nil {
			t.Fatalf("%s: %v", session, err)
		}
	}
	if fake.posts != 2 {
		t.Fatalf("posts=%d", fake.posts)
	}
	if hosts["ses_target"].messageID(event.ID) == hosts["ses_other"].messageID(event.ID) {
		t.Fatal("recipient sessions share a message ID")
	}
	for session, host := range hosts {
		if err := host.Deliver(context.Background(), event); err != nil {
			t.Fatalf("replay on %s: %v", session, err)
		}
	}
	if fake.posts != 2 {
		t.Fatalf("replay resubmitted: posts=%d", fake.posts)
	}
}

func TestOpenCodeV2BusyDoesNotSubmit(t *testing.T) {
	fake := newV2Fake(t)
	fake.activeBody = `{"data":{"ses_target":{"type":"running"}}}`
	srv := httptest.NewServer(fake)
	defer srv.Close()
	host, err := NewOpenCode(srv.URL, "ses_target", "opencode", "test", "2")
	if err != nil {
		t.Fatal(err)
	}
	event, _ := v2Event(t)
	if err := host.Deliver(context.Background(), event); !errors.Is(err, ErrBusy) {
		t.Fatalf("got %v", err)
	}
	if fake.posts != 0 {
		t.Fatal("submitted into running session")
	}
}

func TestOpenCodeV2ReplayAcceptsAdmittedOrDeliveredPromptWithoutResubmitting(t *testing.T) {
	event, text := v2Event(t)
	for name, seed := range map[string]func(*v2Fake, string){
		"inbox":     func(f *v2Fake, id string) { f.inbox[id] = v2Item{"ses_target", text} },
		"delivered": func(f *v2Fake, id string) { f.messages[id] = v2Item{"ses_target", text} },
	} {
		fake := newV2Fake(t)
		srv := httptest.NewServer(fake)
		host, err := NewOpenCode(srv.URL, "ses_target", "opencode", "test", "2")
		if err != nil {
			t.Fatal(err)
		}
		seed(fake, host.messageID(event.ID))
		if err := host.Deliver(context.Background(), event); err != nil {
			t.Fatalf("%s: %v", name, err)
		}
		if fake.posts != 0 {
			t.Fatalf("%s: resubmitted an accepted prompt", name)
		}
		srv.Close()
	}
}

func TestOpenCodeV2ConfirmsAmbiguousPromptWithoutResubmitting(t *testing.T) {
	fake := newV2Fake(t)
	fake.postStatus = http.StatusInternalServerError
	fake.postAdmits = true
	srv := httptest.NewServer(fake)
	defer srv.Close()
	host, err := NewOpenCode(srv.URL, "ses_target", "opencode", "test", "2")
	if err != nil {
		t.Fatal(err)
	}
	event, _ := v2Event(t)
	if err := host.Deliver(context.Background(), event); err != nil {
		t.Fatalf("admitted prompt reported as failed: %v", err)
	}
	if fake.posts != 1 {
		t.Fatalf("posts=%d", fake.posts)
	}
}

func TestOpenCodeV2UnconfirmedPromptIsUncertainAndNotRetried(t *testing.T) {
	fake := newV2Fake(t)
	fake.postStatus = http.StatusInternalServerError
	srv := httptest.NewServer(fake)
	defer srv.Close()
	host, err := NewOpenCode(srv.URL, "ses_target", "opencode", "test", "2")
	if err != nil {
		t.Fatal(err)
	}
	host.confirmTimeout = 20 * time.Millisecond
	event, _ := v2Event(t)
	if err := host.Deliver(context.Background(), event); !errors.Is(err, ErrUncertain) {
		t.Fatalf("got %v", err)
	}
	if fake.posts != 1 {
		t.Fatalf("posts=%d", fake.posts)
	}
}

func TestOpenCodeV2PermanentConflictIsRejectedWithoutRetryOrAcknowledgement(t *testing.T) {
	fake := newV2Fake(t)
	fake.postStatus = http.StatusConflict
	srv := httptest.NewServer(fake)
	defer srv.Close()
	host, err := NewOpenCode(srv.URL, "ses_target", "opencode", "test", "2")
	if err != nil {
		t.Fatal(err)
	}
	event, _ := v2Event(t)
	err = host.Deliver(context.Background(), event)
	if !errors.Is(err, ErrRejected) || errors.Is(err, ErrBusy) {
		t.Fatalf("conflict classified as %v", err)
	}
	if fake.posts != 1 {
		t.Fatalf("retried conflict: posts=%d", fake.posts)
	}
}

func TestOpenCodeV2RejectsWrongGenerationAndMismatchedReadback(t *testing.T) {
	event, _ := v2Event(t)
	t.Run("html from 1.x route on 2.x", func(t *testing.T) {
		fake := newV2Fake(t)
		fake.activeBody = `<!doctype html><html></html>`
		srv := httptest.NewServer(fake)
		defer srv.Close()
		host, err := NewOpenCode(srv.URL, "ses_target", "opencode", "test", "2")
		if err != nil {
			t.Fatal(err)
		}
		if err := host.Deliver(context.Background(), event); !errors.Is(err, ErrRejected) {
			t.Fatalf("got %v", err)
		}
		if fake.posts != 0 {
			t.Fatal("submitted after route mismatch")
		}
	})
	t.Run("missing data envelope", func(t *testing.T) {
		srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			_, _ = w.Write([]byte(`{"items":[]}`))
		}))
		defer srv.Close()
		host, err := NewOpenCode(srv.URL, "ses_target", "opencode", "test", "2")
		if err != nil {
			t.Fatal(err)
		}
		if err := host.Deliver(context.Background(), event); !errors.Is(err, ErrRejected) {
			t.Fatalf("got %v", err)
		}
	})
	t.Run("admitted text differs", func(t *testing.T) {
		fake := newV2Fake(t)
		srv := httptest.NewServer(fake)
		defer srv.Close()
		host, err := NewOpenCode(srv.URL, "ses_target", "opencode", "test", "2")
		if err != nil {
			t.Fatal(err)
		}
		fake.inbox[host.messageID(event.ID)] = v2Item{"ses_target", "other text"}
		if err := host.Deliver(context.Background(), event); !errors.Is(err, ErrRejected) {
			t.Fatalf("got %v", err)
		}
		if fake.posts != 0 {
			t.Fatal("submitted over a conflicting admission")
		}
	})
}

func TestOpenCodeV2AuthFailureIsRejectedBeforeSubmission(t *testing.T) {
	fake := newV2Fake(t)
	srv := httptest.NewServer(fake)
	defer srv.Close()
	host, err := NewOpenCode(srv.URL, "ses_target", "opencode", "wrong", "2")
	if err != nil {
		t.Fatal(err)
	}
	event, _ := v2Event(t)
	if err := host.Deliver(context.Background(), event); !errors.Is(err, ErrRejected) {
		t.Fatalf("got %v", err)
	}
	if fake.posts != 0 {
		t.Fatal("submitted with failed authentication")
	}
}

func TestOpenCodeV2DoctorReadsEnvelopeOnly(t *testing.T) {
	for name, body := range map[string]string{"envelope": `{"data":{"id":"ses_target"}}`, "1.x shape": `{"id":"ses_target"}`} {
		srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			if r.URL.Path != "/api/session/ses_target" {
				t.Errorf("doctor requested %s", r.URL.Path)
			}
			_, _ = w.Write([]byte(body))
		}))
		err := checkOpenCode(context.Background(), Profile{Adapter: "opencode", HostEndpoint: srv.URL, SessionID: "ses_target", HostAPIVersion: "2"})
		srv.Close()
		if name == "envelope" && err != nil {
			t.Fatal(err)
		}
		if name == "1.x shape" && !errors.Is(err, ErrRejected) {
			t.Fatalf("1.x shape accepted under 2.x: %v", err)
		}
	}
}

func TestOpenCodeRequiresExplicitAPIVersion(t *testing.T) {
	for _, api := range []string{"", "3", "v2"} {
		if _, err := NewOpenCode("http://127.0.0.1:4096", "ses_target", "", "", api); err == nil {
			t.Fatalf("API version %q accepted", api)
		}
	}
}
