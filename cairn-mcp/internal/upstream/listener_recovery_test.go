package upstream

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync/atomic"
	"testing"
	"time"
)

func TestListenerReconnectsAndResumesWithoutReplayingPOST(t *testing.T) {
	for _, unclean := range []bool{false, true} {
		t.Run(fmt.Sprintf("unclean=%t", unclean), func(t *testing.T) {
			var posts, gets atomic.Int32
			resumed := make(chan string, 1)
			server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				switch r.Method {
				case http.MethodPost:
					posts.Add(1)
					w.Header().Set("Content-Type", "application/json")
					w.Header().Set("Mcp-Session-Id", "recovery-session")
					_, _ = io.WriteString(w, `{"jsonrpc":"2.0","id":1,"result":{"protocolVersion":"2025-06-18"}}`)
				case http.MethodGet:
					w.Header().Set("Content-Type", "text/event-stream")
					if gets.Add(1) == 1 {
						_, _ = io.WriteString(w, "id: cursor-1\ndata: {\"jsonrpc\":\"2.0\",\"method\":\"notifications/first\"}\n\n")
						w.(http.Flusher).Flush()
						if unclean {
							conn, _, err := w.(http.Hijacker).Hijack()
							if err != nil {
								t.Error(err)
								return
							}
							_ = conn.Close()
						}
						return
					}
					resumed <- r.Header.Get("Last-Event-ID")
					if r.Header.Get("Mcp-Session-Id") != "recovery-session" {
						t.Error("lost session metadata")
					}
					_, _ = io.WriteString(w, "id: cursor-2\ndata: {\"jsonrpc\":\"2.0\",\"method\":\"notifications/second\"}\n\n")
					w.(http.Flusher).Flush()
					<-r.Context().Done()
				case http.MethodDelete:
					w.WriteHeader(http.StatusNoContent)
				}
			}))
			defer server.Close()
			s, err := NewSession(server.URL, server.Client(), fixedAccess{})
			if err != nil {
				t.Fatal(err)
			}
			defer func() { _ = s.Close(context.Background()) }()
			if err := s.Send(context.Background(), json.RawMessage(`{"jsonrpc":"2.0","id":1,"method":"initialize"}`)); err != nil {
				t.Fatal(err)
			}
			for _, want := range []string{`"id":1`, "notifications/first", "notifications/second"} {
				select {
				case event := <-s.Events():
					if event.Err != nil || !strings.Contains(string(event.Message), want) {
						t.Fatalf("event = %#v, want %s", event, want)
					}
				case <-time.After(3 * time.Second):
					t.Fatalf("missing %s after stream drop", want)
				}
			}
			if got := <-resumed; got != "cursor-1" {
				t.Fatalf("resume cursor = %q", got)
			}
			if posts.Load() != 1 {
				t.Fatalf("POST replayed: %d calls", posts.Load())
			}
		})
	}
}

func TestListenerRepeatedDisconnectsTerminate(t *testing.T) {
	var gets atomic.Int32
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch r.Method {
		case http.MethodPost:
			w.Header().Set("Content-Type", "application/json")
			_, _ = io.WriteString(w, `{"jsonrpc":"2.0","id":1,"result":{"protocolVersion":"2025-06-18"}}`)
		case http.MethodGet:
			gets.Add(1)
			w.Header().Set("Content-Type", "text/event-stream")
		case http.MethodDelete:
			w.WriteHeader(http.StatusNoContent)
		}
	}))
	defer server.Close()
	s, err := NewSession(server.URL, server.Client(), fixedAccess{})
	if err != nil {
		t.Fatal(err)
	}
	defer func() { _ = s.Close(context.Background()) }()
	if err := s.Send(context.Background(), json.RawMessage(`{"jsonrpc":"2.0","id":1,"method":"initialize"}`)); err != nil {
		t.Fatal(err)
	}
	_ = receiveEvent(t, s.Events())
	select {
	case event := <-s.Events():
		if event.Err == nil || !strings.Contains(event.Err.Error(), "reconnect budget exhausted") {
			t.Fatalf("event = %#v", event)
		}
	case <-time.After(5 * time.Second):
		t.Fatal("disconnected listener never signalled terminal failure")
	}
	if gets.Load() != 4 {
		t.Fatalf("GET calls = %d, want initial plus three retries", gets.Load())
	}
}

func TestListenerFailedHandshakeDoesNotEarnHealthyTime(t *testing.T) {
	for _, timeout := range []bool{false, true} {
		t.Run(fmt.Sprintf("timeout=%t", timeout), func(t *testing.T) {
			server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				if timeout {
					<-r.Context().Done()
					return
				}
				w.WriteHeader(http.StatusGatewayTimeout)
			}))
			defer server.Close()
			client := server.Client()
			client.Timeout = 10 * time.Millisecond
			s, err := NewSession(server.URL, client, fixedAccess{})
			if err != nil {
				t.Fatal(err)
			}
			defer s.cancel()
			cursor := ""
			healthy, err := s.listenLegacy(&cursor)
			if err == nil || healthy != 0 {
				t.Fatalf("failed handshake earned %s health, error=%v", healthy, err)
			}
		})
	}
}
