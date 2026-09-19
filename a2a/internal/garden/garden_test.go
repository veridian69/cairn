package garden

import (
	"context"
	"encoding/json"
	"errors"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/google/uuid"
	"github.com/modelcontextprotocol/go-sdk/mcp"
	"github.com/nats-io/nats.go/jetstream"
	"github.com/veridian69/cairn/a2a/internal/gardenauth"
	"github.com/veridian69/cairn/a2a/internal/model"
	"github.com/veridian69/cairn/a2a/internal/transport"
)

const principalA = "11111111-1111-4111-8111-111111111111"
const principalB = "22222222-2222-4222-8222-222222222222"
const instance = "33333333-3333-4333-8333-333333333333"

type fixture struct {
	t        *testing.T
	cfg      Config
	srv      *Server
	http     *httptest.Server
	stream   *transport.Stream
	ns       *transport.Server
	mu       sync.Mutex
	denied   map[string]bool
	readonly map[string]bool
}

func newFixture(t *testing.T) *fixture { return fixtureBeforeStart(t, nil) }
func fixtureBeforeStart(t *testing.T, before func(*fixture)) *fixture {
	t.Helper()
	f := &fixture{t: t, denied: map[string]bool{}, readonly: map[string]bool{}}
	auth := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		token := strings.TrimPrefix(r.Header.Get("Authorization"), "Bearer ")
		f.mu.Lock()
		denied, ro := f.denied[token], f.readonly[token]
		f.mu.Unlock()
		if denied || (token != "alice" && token != "bob") {
			w.WriteHeader(401)
			return
		}
		id := principalA
		if token == "bob" {
			id = principalB
		}
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(map[string]any{"instance_id": instance, "product_version": "0.1.0", "contract_identity": "cairn.memory/v1", "contract_digest": strings.Repeat("a", 64), "mcp_contract_digest": strings.Repeat("b", 64), "principal_id": id, "principal_kind": "workload", "scope": map[string]any{"realm": "test", "segments": []any{}}, "classification": "internal", "permissions": map[string]bool{"retrieve": true, "ingest": !ro, "promote": false, "invalidate": false}, "evaluated_at": "2026-09-17T00:00:00.000000Z", "permission_basis": "current_grants_only"})
	}))
	t.Cleanup(auth.Close)
	dir := t.TempDir()
	ns, err := transport.NewServer(dir)
	if err != nil {
		t.Fatal(err)
	}
	f.ns = ns
	t.Cleanup(func() { f.ns.Stop() })
	f.stream, err = transport.NewStream(ns.ClientURL())
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(f.stream.Close)
	urlfile := filepath.Join(dir, "daemon.url")
	if err = os.WriteFile(urlfile, []byte(ns.ClientURL()), 0600); err != nil {
		t.Fatal(err)
	}
	f.cfg = Config{Listen: "127.0.0.1:0", DataDir: dir, DaemonURLFile: urlfile, Auth: gardenauth.Config{Endpoint: auth.URL + "/memory/v1/diagnose", InstanceID: instance, Scope: gardenauth.Scope{Realm: "test", Segments: []gardenauth.Segment{}}, Classification: "internal"}, Principals: map[string]string{principalA: "alice", principalB: "bob"}}
	if before != nil {
		before(f)
	}
	f.start()
	t.Cleanup(func() { f.http.Close(); _ = f.srv.Close() })
	return f
}
func (f *fixture) start() {
	f.t.Helper()
	var err error
	f.srv, err = New(context.Background(), f.cfg)
	if err != nil {
		f.t.Fatal(err)
	}
	f.http = httptest.NewServer(f.srv.Handler())
}
func (f *fixture) client(token string) *Client {
	f.t.Helper()
	c, err := Dial(context.Background(), ClientConfig{Endpoint: f.http.URL + "/mcp", Token: token})
	if err != nil {
		f.t.Fatal(err)
	}
	f.t.Cleanup(func() { _ = c.Close() })
	return c
}
func requireCode(t *testing.T, err error, code string) {
	t.Helper()
	var e *Error
	if !errors.As(err, &e) || e.Code != code {
		t.Fatalf("want %s, got %v", code, err)
	}
}
func TestDeliverySurvivesRestartAndAcknowledgementIsOwned(t *testing.T) {
	f := newFixture(t)
	a, b := f.client("alice"), f.client("bob")
	ctx := context.Background()
	owner := uuid.NewString()
	if _, err := a.Send(ctx, SendArgs{Content: "room noise"}); err != nil {
		t.Fatal(err)
	}
	sent, err := a.Send(ctx, SendArgs{Content: "do this", Recipients: []string{"bob"}})
	if err != nil {
		t.Fatal(err)
	}
	p, err := b.Poll(ctx, PollArgs{ConsumerID: owner})
	if err != nil {
		t.Fatal(err)
	}
	if p.Message == nil || p.Message.ID != sent.ID || p.Message.AuthorName != "alice" || p.Message.Binding.InstanceID != instance {
		t.Fatalf("bad delivery: %+v", p)
	}
	again, err := b.Poll(ctx, PollArgs{ConsumerID: owner})
	if err != nil || again.Receipt != p.Receipt {
		t.Fatalf("retry consumed message: %+v %v", again, err)
	}
	_, err = b.Poll(ctx, PollArgs{ConsumerID: uuid.NewString()})
	requireCode(t, err, "inbox_busy")
	err = a.Acknowledge(ctx, AckArgs{ConsumerID: owner, Receipt: p.Receipt})
	requireCode(t, err, "invalid_receipt")
	f.http.Close()
	_ = f.srv.Close()
	f.start()
	b = f.client("bob")
	again, err = b.Poll(ctx, PollArgs{ConsumerID: owner})
	if err != nil || again.Receipt != p.Receipt {
		t.Fatalf("restart lost pending receipt: %+v %v", again, err)
	}
	for i := 0; i < 2; i++ {
		if err = b.Acknowledge(ctx, AckArgs{ConsumerID: owner, Receipt: p.Receipt}); err != nil {
			t.Fatal(err)
		}
	}
	empty, err := b.Poll(ctx, PollArgs{ConsumerID: owner})
	if err != nil || empty.Message != nil {
		t.Fatalf("ack did not advance: %+v %v", empty, err)
	}
	history, err := b.Read(ctx, ReadArgs{})
	if err != nil || len(history.Messages) != 2 {
		t.Fatalf("room history: %+v %v", history, err)
	}
}
func TestAuthenticationAndReadOnlyCannotSend(t *testing.T) {
	f := newFixture(t)
	ctx := context.Background()
	req, _ := http.NewRequest("POST", f.http.URL+"/mcp", strings.NewReader("{}"))
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	resp.Body.Close()
	if resp.StatusCode != 401 {
		t.Fatalf("missing auth: %d", resp.StatusCode)
	}
	a := f.client("alice")
	f.mu.Lock()
	f.readonly["alice"] = true
	f.mu.Unlock()
	_, err = a.Send(ctx, SendArgs{Content: "forbidden"})
	requireCode(t, err, "forbidden")
	if _, err = a.Status(ctx); err != nil {
		t.Fatal(err)
	}
	f.mu.Lock()
	f.denied["alice"] = true
	f.mu.Unlock()
	_, err = a.Read(ctx, ReadArgs{})
	requireCode(t, err, "forbidden")
	req, _ = http.NewRequest("POST", f.http.URL+"/mcp", strings.NewReader("{}"))
	req.Header.Set("Authorization", "Bearer bob")
	req.Header.Set("Origin", "https://example.com")
	resp, err = http.DefaultClient.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	resp.Body.Close()
	if resp.StatusCode != 403 {
		t.Fatalf("origin admitted: %d", resp.StatusCode)
	}
}
func TestPendingRetentionGapAndStreamReset(t *testing.T) {
	for _, reset := range []bool{false, true} {
		t.Run(map[bool]string{false: "gap", true: "reset"}[reset], func(t *testing.T) {
			f := newFixture(t)
			a, b := f.client("alice"), f.client("bob")
			ctx := context.Background()
			owner := uuid.NewString()
			_, err := a.Send(ctx, SendArgs{Content: "pending", Recipients: []string{"bob"}})
			if err != nil {
				t.Fatal(err)
			}
			p, err := b.Poll(ctx, PollArgs{ConsumerID: owner})
			if err != nil {
				t.Fatal(err)
			}
			js, err := jetstream.New(f.stream.NATSConn())
			if err != nil {
				t.Fatal(err)
			}
			if reset {
				if err = js.DeleteStream(ctx, transport.StreamName); err != nil {
					t.Fatal(err)
				}
				st, e := transport.NewStream(f.stream.NATSConn().ConnectedUrl())
				if e != nil {
					t.Fatal(e)
				}
				defer st.Close()
			} else {
				st, e := js.Stream(ctx, transport.StreamName)
				if e != nil {
					t.Fatal(e)
				}
				if err = st.DeleteMsg(ctx, p.Message.Sequence); err != nil {
					t.Fatal(err)
				}
			}
			_, err = b.Poll(ctx, PollArgs{ConsumerID: owner})
			want := "retention_gap"
			if reset {
				want = "stream_reset"
			}
			requireCode(t, err, want)
		})
	}
}
func TestDelayedPollRechecksRevocation(t *testing.T) {
	f := newFixture(t)
	a, b := f.client("alice"), f.client("bob")
	done := make(chan error, 1)
	go func() {
		_, err := b.Poll(context.Background(), PollArgs{ConsumerID: uuid.NewString(), WaitSeconds: 2})
		done <- err
	}()
	time.Sleep(150 * time.Millisecond)
	f.mu.Lock()
	f.denied["bob"] = true
	f.mu.Unlock()
	if _, err := a.Send(context.Background(), SendArgs{Content: "secret", Recipients: []string{"bob"}}); err != nil {
		t.Fatal(err)
	}
	requireCode(t, <-done, "forbidden")
}
func TestBindingRefusesReuse(t *testing.T) {
	f := newFixture(t)
	f.http.Close()
	_ = f.srv.Close()
	cfg := f.cfg
	cfg.Auth.Classification = "restricted"
	s, err := New(context.Background(), cfg)
	if err == nil {
		_ = s.Close()
		t.Fatal("rebound existing data")
	}
	f.start()
}
func TestOfflineInboxRetainsMessages(t *testing.T) {
	f := newFixture(t)
	f.http.Close()
	_ = f.srv.Close()
	m := model.NewMessage(model.Participant{ID: uuid.NewString(), Name: "local"}, "offline", nil)
	m.Metadata["garden_recipients"] = []string{"bob"}
	if err := f.stream.Publish(context.Background(), m); err != nil {
		t.Fatal(err)
	}
	f.start()
	p, err := f.client("bob").Poll(context.Background(), PollArgs{ConsumerID: uuid.NewString()})
	if err != nil {
		t.Fatal(err)
	}
	if p.Message == nil || p.Message.ID != m.ID {
		t.Fatal("offline message lost")
	}
}

// These reject body-controlled identity and accidental cross-deployment reuse.
func TestSpoofingAndConcurrentIdentityRemainIsolated(t *testing.T) {
	f := newFixture(t)
	a, b := f.client("alice"), f.client("bob")
	ctx := context.Background()
	var wg sync.WaitGroup
	for _, pair := range []struct {
		c    *Client
		name string
	}{{a, "alice"}, {b, "bob"}} {
		pair := pair
		wg.Add(1)
		go func() {
			defer wg.Done()
			for i := 0; i < 10; i++ {
				v, err := pair.c.Status(ctx)
				if err != nil || v.Participant != pair.name {
					t.Errorf("identity crossed: %+v %v", v, err)
				}
			}
		}()
	}
	wg.Wait()
	result, err := a.session.CallTool(ctx, &mcp.CallToolParams{Name: "send_message", Arguments: map[string]any{"content": "spoof", "author_name": "bob"}})
	if err == nil && !result.IsError {
		t.Fatal("accepted client author identity")
	}
	messages, err := b.Read(ctx, ReadArgs{})
	if err != nil || len(messages.Messages) != 0 {
		t.Fatalf("spoof published: %+v %v", messages, err)
	}
}
func TestLeaseTakeoverFencesOldConsumer(t *testing.T) {
	f := newFixture(t)
	a, b := f.client("alice"), f.client("bob")
	ctx := context.Background()
	old, newID := uuid.NewString(), uuid.NewString()
	if _, err := a.Send(ctx, SendArgs{Content: "work", Recipients: []string{"bob"}}); err != nil {
		t.Fatal(err)
	}
	first, err := b.Poll(ctx, PollArgs{ConsumerID: old})
	if err != nil {
		t.Fatal(err)
	}
	if _, err = f.srv.sql.Exec("UPDATE garden_inbox SET lease_until=0 WHERE principal=?", principalB); err != nil {
		t.Fatal(err)
	}
	second, err := b.Poll(ctx, PollArgs{ConsumerID: newID})
	if err != nil || second.Receipt != first.Receipt {
		t.Fatalf("lease takeover lost message: %+v %v", second, err)
	}
	requireCode(t, b.Acknowledge(ctx, AckArgs{ConsumerID: old, Receipt: first.Receipt}), "invalid_receipt")
	if err = b.Acknowledge(ctx, AckArgs{ConsumerID: newID, Receipt: second.Receipt}); err != nil {
		t.Fatal(err)
	}
}
func TestReadAndPendingDeliveryApplyRedaction(t *testing.T) {
	f := newFixture(t)
	a, b := f.client("alice"), f.client("bob")
	ctx := context.Background()
	owner := uuid.NewString()
	m, err := a.Send(ctx, SendArgs{Content: "remove me", Recipients: []string{"bob"}})
	if err != nil {
		t.Fatal(err)
	}
	if _, err = b.Poll(ctx, PollArgs{ConsumerID: owner}); err != nil {
		t.Fatal(err)
	}
	if _, err = f.srv.sql.Exec("INSERT INTO redactions(message_id,reason,redacted_by) VALUES(?,?,?)", m.ID, "removed", "test"); err != nil {
		t.Fatal(err)
	}
	p, err := b.Poll(ctx, PollArgs{ConsumerID: owner})
	if err != nil || p.Message.Content != "[redacted: removed]" {
		t.Fatalf("pending secret returned: %+v %v", p, err)
	}
	history, err := a.Read(ctx, ReadArgs{})
	if err != nil || history.Messages[0].Content != "[redacted: removed]" {
		t.Fatalf("history secret returned: %+v %v", history, err)
	}
}
func TestNewInboxStartsAtEnrolmentTail(t *testing.T) {
	f := fixtureBeforeStart(t, func(f *fixture) {
		m := model.NewMessage(model.Participant{ID: uuid.NewString(), Name: "local"}, "before enrolment", nil)
		m.Metadata["garden_recipients"] = []string{"bob"}
		if err := f.stream.Publish(context.Background(), m); err != nil {
			t.Fatal(err)
		}
	})
	p, err := f.client("bob").Poll(context.Background(), PollArgs{ConsumerID: uuid.NewString()})
	if err != nil || p.Message != nil {
		t.Fatalf("replayed pre-enrolment history: %+v %v", p, err)
	}
}

func TestGatewayCannotRunTwice(t *testing.T) {
	f := newFixture(t)
	other, err := New(context.Background(), f.cfg)
	if err == nil {
		_ = other.Close()
		t.Fatal("two gateways own same inbox storage")
	}
	if _, err = f.client("alice").Status(context.Background()); err != nil {
		t.Fatalf("second server unlocked first: %v", err)
	}
}
func TestConfigRejectsDuplicateAndAliasedFields(t *testing.T) {
	f := newFixture(t)
	raw, _ := json.Marshal(f.cfg)
	for name, body := range map[string]string{"duplicate": strings.TrimSuffix(string(raw), "}") + `,"listen":"127.0.0.1:1"}`, "case_alias": strings.Replace(string(raw), `"listen"`, `"Listen"`, 1), "unknown": strings.TrimSuffix(string(raw), "}") + `,"extra":true}`, "trailing": string(raw) + `{}`} {
		t.Run(name, func(t *testing.T) {
			path := filepath.Join(t.TempDir(), "config.json")
			if err := os.WriteFile(path, []byte(body), 0600); err != nil {
				t.Fatal(err)
			}
			if _, err := LoadConfig(path); err == nil {
				t.Fatal("accepted ambiguous configuration")
			}
		})
	}
}
func TestRemoteClientRejectsRedirectWithoutLeakingCredential(t *testing.T) {
	reached := false
	dst := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { reached = true; w.WriteHeader(500) }))
	defer dst.Close()
	src := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { http.Redirect(w, r, dst.URL+"/mcp", 307) }))
	defer src.Close()
	_, err := Dial(context.Background(), ClientConfig{Endpoint: src.URL + "/mcp", Token: "secret"})
	requireCode(t, err, "unavailable")
	if reached {
		t.Fatal("redirect received credential")
	}
}

func TestDaemonRestartReconnectsThroughURLFile(t *testing.T) {
	f := newFixture(t)
	a, b := f.client("alice"), f.client("bob")
	ctx := context.Background()
	owner := uuid.NewString()
	sent, err := a.Send(ctx, SendArgs{Content: "survive daemon restart", Recipients: []string{"bob"}})
	if err != nil {
		t.Fatal(err)
	}
	f.ns.Stop()
	f.ns, err = transport.NewServer(f.cfg.DataDir)
	if err != nil {
		t.Fatal(err)
	}
	if err = os.WriteFile(f.cfg.DaemonURLFile, []byte(f.ns.ClientURL()), 0600); err != nil {
		t.Fatal(err)
	}
	p, err := b.Poll(ctx, PollArgs{ConsumerID: owner})
	if err != nil || p.Message == nil || p.Message.ID != sent.ID {
		t.Fatalf("reconnect lost inbox: %+v %v", p, err)
	}
}
func TestUnavailableCairnFailsClosed(t *testing.T) {
	f := newFixture(t)
	a := f.client("alice")
	f.srv.auth, _ = gardenauth.New(gardenauth.Config{Endpoint: "http://127.0.0.1:1/memory/v1/diagnose", InstanceID: instance, Scope: f.cfg.Auth.Scope, Classification: "internal"})
	_, err := a.Read(context.Background(), ReadArgs{})
	requireCode(t, err, "unavailable")
}
func TestPollDoesNotConsumeMalformedUnaddressedRecord(t *testing.T) {
	f := newFixture(t)
	ctx := context.Background()
	js, err := jetstream.New(f.stream.NATSConn())
	if err != nil {
		t.Fatal(err)
	}
	if _, err = js.Publish(ctx, transport.Subject, []byte(`{}`)); err != nil {
		t.Fatal(err)
	}
	_, err = f.client("bob").Poll(ctx, PollArgs{ConsumerID: uuid.NewString()})
	requireCode(t, err, "retention_gap")
}

func TestDaemonStorageCannotBeReboundThroughDifferentDataDir(t *testing.T) {
	f := newFixture(t)
	if _, err := f.client("alice").Send(context.Background(), SendArgs{Content: "internal conversation"}); err != nil {
		t.Fatal(err)
	}
	for _, live := range []bool{true, false} {
		if !live {
			f.http.Close()
			_ = f.srv.Close()
		}
		cfg := f.cfg
		cfg.DataDir = t.TempDir()
		cfg.Auth.Classification = "public"
		other, err := New(context.Background(), cfg)
		if err == nil {
			_ = other.Close()
			t.Errorf("same daemon was rebound through independent storage (live=%v)", live)
		}
		if _, err = os.Stat(filepath.Join(cfg.DataDir, "state.db")); !os.IsNotExist(err) {
			t.Errorf("mismatched storage was opened before validation (live=%v)", live)
		}
	}
	f.start()
}
func TestHistoryPaginatesWithinWireByteLimit(t *testing.T) {
	for _, escaped := range []bool{false, true} {
		t.Run(map[bool]string{false: "plain", true: "escaped"}[escaped], func(t *testing.T) {
			f := newFixture(t)
			c := f.client("alice")
			ctx := context.Background()
			content := strings.Repeat("x", 64*1024)
			count := 70
			if escaped {
				content = strings.Repeat("\x01", 64*1024)
				count = 16
			}
			// Publish via the real local stream: escaped 64KiB text legitimately exceeds
			// the remote 128KiB request bound but remains existing readable room content.
			for i := 0; i < count; i++ {
				m := model.NewMessage(model.Participant{ID: uuid.NewString(), Name: "local"}, content, nil)
				if err := f.stream.Publish(ctx, m); err != nil {
					t.Fatal(err)
				}
			}
			seen := map[string]bool{}
			args := ReadArgs{Limit: 100}
			for pages := 0; pages < count+1; pages++ {
				out, err := c.Read(ctx, args)
				if err != nil {
					t.Fatalf("valid history page failed: %v", err)
				}
				if len(out.Messages) == 0 {
					t.Fatal("byte pagination made no progress")
				}
				for _, m := range out.Messages {
					if seen[m.ID] || m.Content != content {
						t.Fatal("byte pagination duplicated or changed content")
					}
					seen[m.ID] = true
				}
				if out.NextSeq != out.Messages[len(out.Messages)-1].Sequence {
					t.Fatal("pagination cursor skipped messages")
				}
				if !out.More {
					break
				}
				args.AfterSeq = out.NextSeq
				args.Generation = out.Generation
			}
			if len(seen) != count {
				t.Fatalf("pagination returned %d of %d messages", len(seen), count)
			}
		})
	}
}
func TestRemoteClientRejectsEncodedResponsesAtHTTPBoundary(t *testing.T) {
	for _, encodings := range [][]string{{"gzip"}, {"identity", "identity"}, {"identity, gzip"}} {
		t.Run(strings.Join(encodings, "_"), func(t *testing.T) {
			var encoding string
			endpoint := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				encoding = r.Header.Get("Accept-Encoding")
				for _, v := range encodings {
					w.Header().Add("Content-Encoding", v)
				}
				w.Header().Set("Content-Type", "application/json")
				var request struct {
					ID json.RawMessage `json:"id"`
				}
				_ = json.NewDecoder(r.Body).Decode(&request)
				if len(request.ID) == 0 {
					w.WriteHeader(202)
					return
				}
				_ = json.NewEncoder(w).Encode(map[string]any{"jsonrpc": "2.0", "id": request.ID, "result": map[string]any{"protocolVersion": "2025-11-25", "capabilities": map[string]any{}, "serverInfo": map[string]any{"name": "synthetic", "version": "1"}}})
			}))
			defer endpoint.Close()
			ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
			defer cancel()
			c, err := Dial(ctx, ClientConfig{Endpoint: endpoint.URL + "/mcp", Token: "test-token"})
			if c != nil {
				_ = c.Close()
			}
			requireCode(t, err, "unavailable")
			if encoding != "identity" {
				t.Fatalf("client requested unsafe response encoding %q", encoding)
			}
		})
	}
}

func TestReconnectRejectsDifferentDaemonStorage(t *testing.T) {
	f := newFixture(t)
	c := f.client("alice")
	f.ns.Stop()
	var err error
	f.ns, err = transport.NewServer(t.TempDir())
	if err != nil {
		t.Fatal(err)
	}
	if err = os.WriteFile(f.cfg.DaemonURLFile, []byte(f.ns.ClientURL()), 0600); err != nil {
		t.Fatal(err)
	}
	_, err = c.Status(context.Background())
	requireCode(t, err, "unavailable")
}
func TestDaemonStoreSymlinkSharesBindingAndLock(t *testing.T) {
	f := newFixture(t)
	alias := filepath.Join(t.TempDir(), "store")
	if err := os.Symlink(f.cfg.DataDir, alias); err != nil {
		t.Fatal(err)
	}
	cfg := f.cfg
	cfg.DataDir = alias
	other, err := New(context.Background(), cfg)
	if err == nil {
		_ = other.Close()
		t.Fatal("storage alias bypassed gateway ownership")
	}
	f.http.Close()
	_ = f.srv.Close()
	cfg.Auth.Classification = "public"
	other, err = New(context.Background(), cfg)
	if err == nil {
		_ = other.Close()
		t.Fatal("storage alias bypassed persisted authority binding")
	}
	f.start()
}

func TestSelfAddressedMessagesStayInHistoryWithoutWakingSender(t *testing.T) {
	f := newFixture(t)
	a, b := f.client("alice"), f.client("bob")
	ctx := context.Background()
	owner := uuid.NewString()
	self, err := a.Send(ctx, SendArgs{Content: "self note", Recipients: []string{"alice"}})
	if err != nil {
		t.Fatal(err)
	}
	empty, err := a.Poll(ctx, PollArgs{ConsumerID: owner})
	if err != nil || empty.Message != nil {
		t.Fatal("self-addressed message woke its own sender")
	}
	incoming, err := b.Send(ctx, SendArgs{Content: "external request", Recipients: []string{"alice"}})
	if err != nil {
		t.Fatal(err)
	}
	pending, err := a.Poll(ctx, PollArgs{ConsumerID: owner})
	if err != nil || pending.Message == nil || pending.Message.ID != incoming.ID {
		t.Fatal("self suppression skipped another participant's addressed message")
	}
	history, err := a.Read(ctx, ReadArgs{})
	if err != nil || len(history.Messages) != 2 || history.Messages[0].ID != self.ID {
		t.Fatal("self suppression removed room history")
	}
}
