package attention

import (
	"bufio"
	"context"
	"encoding/json"
	"encoding/pem"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/google/uuid"
	"github.com/modelcontextprotocol/go-sdk/mcp"
	"github.com/veridian69/cairn/a2a/internal/garden"
	"github.com/veridian69/cairn/a2a/internal/gardenauth"
	"github.com/veridian69/cairn/a2a/internal/transport"
)

const (
	e2eAlicePrincipal = "11111111-1111-4111-8111-111111111111"
	e2eBobPrincipal   = "22222222-2222-4222-8222-222222222222"
	e2eInstance       = "33333333-3333-4333-8333-333333333333"
)

type cancelAfterAckInbox struct {
	Inbox
	cancel context.CancelFunc
	once   sync.Once
	acked  chan struct{}
}

func (i *cancelAfterAckInbox) Ack(ctx context.Context, consumer, receipt string) error {
	if err := i.Inbox.Ack(ctx, consumer, receipt); err != nil {
		return err
	}
	i.once.Do(func() {
		close(i.acked)
		i.cancel()
	})
	return nil
}

func TestAttentionAdaptersEndToEndWithCentralGarden(t *testing.T) {
	ctx, cancel := context.WithTimeout(context.Background(), 20*time.Second)
	defer cancel()

	scope := gardenauth.Scope{Realm: "test", Segments: []gardenauth.Segment{}}
	auth := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		token := strings.TrimPrefix(r.Header.Get("Authorization"), "Bearer ")
		principal := ""
		switch token {
		case "alice-token":
			principal = e2eAlicePrincipal
		case "bob-token":
			principal = e2eBobPrincipal
		default:
			w.WriteHeader(http.StatusUnauthorized)
			return
		}
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(map[string]any{
			"instance_id": e2eInstance, "product_version": "0.1.0",
			"contract_identity": "cairn.memory/v1", "contract_digest": strings.Repeat("a", 64),
			"mcp_contract_digest": strings.Repeat("b", 64), "principal_id": principal,
			"principal_kind": "workload", "scope": scope, "classification": "internal",
			"permissions":  map[string]bool{"retrieve": true, "ingest": true, "promote": false, "invalidate": false},
			"evaluated_at": "2026-09-17T00:00:00.000000Z", "permission_basis": "current_grants_only",
		})
	}))
	defer auth.Close()

	dir := t.TempDir()
	nats, err := transport.NewServer(dir)
	if err != nil {
		t.Fatal(err)
	}
	defer nats.Stop()
	stream, err := transport.NewStream(nats.ClientURL())
	if err != nil {
		t.Fatal(err)
	}
	defer stream.Close()
	daemonURL := filepath.Join(dir, "daemon.url")
	if err = os.WriteFile(daemonURL, []byte(nats.ClientURL()), 0600); err != nil {
		t.Fatal(err)
	}
	central, err := garden.New(ctx, garden.Config{
		Listen: "127.0.0.1:0", DataDir: dir, DaemonURLFile: daemonURL,
		Auth:       gardenauth.Config{Endpoint: auth.URL + "/memory/v1/diagnose", InstanceID: e2eInstance, Scope: scope, Classification: "internal"},
		Principals: map[string]string{e2eAlicePrincipal: "alice", e2eBobPrincipal: "bob"},
	})
	if err != nil {
		t.Fatal(err)
	}
	defer central.Close()
	centralHTTP := httptest.NewServer(central.Handler())
	defer centralHTTP.Close()

	credentials := t.TempDir()
	aliceCredential := filepath.Join(credentials, "alice.token")
	bobCredential := filepath.Join(credentials, "bob.token")
	if err = os.WriteFile(aliceCredential, []byte("alice-token\n"), 0600); err != nil {
		t.Fatal(err)
	}
	if err = os.WriteFile(bobCredential, []byte("bob-token\n"), 0600); err != nil {
		t.Fatal(err)
	}
	profile := func(participant, credential string) Profile {
		return Profile{GardenEndpoint: centralHTTP.URL + "/mcp", CredentialFile: credential, InstanceID: e2eInstance, Scope: scope, Classification: "internal", Participant: participant}
	}
	diagnosticProfile := profile("bob", bobCredential)
	diagnosticProfile.Adapter = "stdio"
	report, err := Doctor(ctx, diagnosticProfile, true)
	if err != nil || report.Garden != "authenticated_binding_verified" || report.Host != "tools_only" {
		t.Fatalf("doctor: %+v %v", report, err)
	}
	t.Run("tls_doctor", func(t *testing.T) {
		secure := httptest.NewTLSServer(central.Handler())
		defer secure.Close()
		ca := filepath.Join(t.TempDir(), "ca.crt")
		if err := os.WriteFile(ca, pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: secure.Certificate().Raw}), 0600); err != nil {
			t.Fatal(err)
		}
		profile := diagnosticProfile
		profile.GardenEndpoint = secure.URL + "/mcp"
		profile.GardenTLSCAFile = ca
		profile.GardenTLSServerName = "example.com"
		report, err := Doctor(ctx, profile, false)
		if err != nil || report.Garden != "authenticated_binding_verified" {
			t.Fatalf("TLS doctor failed: %v", err)
		}
		profile.GardenTLSServerName = "wrong.invalid"
		if _, err = Doctor(ctx, profile, false); err == nil {
			t.Fatal("doctor ignored TLS server-name verification")
		}
	})
	t.Run("installed_tools", func(t *testing.T) { exerciseInstalledTools(t, ctx, diagnosticProfile) })
	alice, err := Connect(ctx, profile("alice", aliceCredential))
	if err != nil {
		t.Fatal(err)
	}
	defer alice.Close()
	bob, err := Connect(ctx, profile("bob", bobCredential))
	if err != nil {
		t.Fatal(err)
	}
	defer bob.Close()

	messageIDs := make([]string, 0, 3)
	for _, content := range []string{"claude channel", "codex task", "opencode session"} {
		value, err := alice.Call(ctx, "send_message", json.RawMessage(`{"content":`+mustJSON(t, content)+`,"recipients":["bob"]}`))
		if err != nil {
			t.Fatal(err)
		}
		message, ok := value.(garden.Message)
		if !ok || message.ID == "" {
			t.Fatalf("unexpected send result: %#v", value)
		}
		messageIDs = append(messageIDs, message.ID)
	}
	consumer := uuid.NewString()

	// Claude receives the real pending Garden message through the inspected
	// channel JSONL contract. Merely writing the notification does not consume
	// it; the synthetic client must call acknowledge_delivery first.
	t.Run("claude", func(t *testing.T) {
		runCtx, runCancel := context.WithCancel(ctx)
		in, clientWriter := io.Pipe()
		clientReader, out := io.Pipe()
		defer clientWriter.Close()
		defer clientReader.Close()
		server := NewStdio(out, bob, true)
		inbox := &cancelAfterAckInbox{Inbox: bob, cancel: runCancel, acked: make(chan struct{})}
		runner := Runner{Inbox: inbox, Host: server, ConsumerID: consumer, retryAfter: time.Millisecond}
		finished := make(chan error, 1)
		go func() { finished <- server.Run(runCtx, in, runner.Run) }()
		encoder := json.NewEncoder(clientWriter)
		decoder := json.NewDecoder(bufio.NewReader(clientReader))
		if err := encoder.Encode(map[string]any{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": map[string]string{"protocolVersion": "2025-11-25"}}); err != nil {
			t.Fatal(err)
		}
		var frame map[string]any
		if err := decoder.Decode(&frame); err != nil {
			t.Fatal(err)
		}
		if err := encoder.Encode(map[string]any{"jsonrpc": "2.0", "method": "notifications/initialized"}); err != nil {
			t.Fatal(err)
		}
		if err := decoder.Decode(&frame); err != nil {
			t.Fatal(err)
		}
		params, _ := frame["params"].(map[string]any)
		meta, _ := params["meta"].(map[string]any)
		if frame["method"] != "notifications/claude/channel" || meta["message_id"] != messageIDs[0] {
			t.Fatalf("wrong Claude delivery: %#v", frame)
		}
		stillPending, err := bob.Poll(ctx, consumer)
		if err != nil || stillPending == nil || stillPending.Event.ID != messageIDs[0] {
			t.Fatalf("channel write consumed delivery: %#v %v", stillPending, err)
		}
		if err := encoder.Encode(map[string]any{"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": map[string]any{"name": "acknowledge_delivery", "arguments": map[string]string{"message_id": messageIDs[0]}}}); err != nil {
			t.Fatal(err)
		}
		if err := decoder.Decode(&frame); err != nil {
			t.Fatal(err)
		}
		awaitAckAndExit(t, inbox.acked, finished)
	})
	assertPending(t, ctx, bob, consumer, messageIDs[1])

	t.Run("codex", func(t *testing.T) {
		runCtx, runCancel := context.WithCancel(ctx)
		command := exec.CommandContext(runCtx, os.Args[0], "-test.run=TestCodexWireHelper")
		command.Env = append(os.Environ(), "GARDEN_CODEX_TEST_HELPER=accept", "GARDEN_CODEX_MESSAGE_ID="+messageIDs[1])
		host, err := startCodex(runCtx, command, "thread-target")
		if err != nil {
			t.Fatal(err)
		}
		defer host.Close()
		inbox := &cancelAfterAckInbox{Inbox: bob, cancel: runCancel, acked: make(chan struct{})}
		runner := Runner{Inbox: inbox, Host: host, ConsumerID: consumer, retryAfter: time.Millisecond}
		finished := make(chan error, 1)
		go func() { finished <- runner.Run(runCtx) }()
		awaitAckAndExit(t, inbox.acked, finished)
	})
	assertPending(t, ctx, bob, consumer, messageIDs[2])

	t.Run("opencode", func(t *testing.T) {
		var mu sync.Mutex
		var stored map[string]any
		hostHTTP := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			switch {
			case strings.Contains(r.URL.Path, "/message/"):
				mu.Lock()
				defer mu.Unlock()
				if stored == nil {
					w.WriteHeader(http.StatusNotFound)
					return
				}
				_ = json.NewEncoder(w).Encode(map[string]any{"info": map[string]string{"id": stored["messageID"].(string), "role": "user", "sessionID": "ses_target"}, "parts": stored["parts"]})
			case r.URL.Path == "/session/status":
				_, _ = io.WriteString(w, `{}`)
			case r.URL.Path == "/session/ses_target/prompt_async" && r.Method == http.MethodPost:
				var submitted map[string]any
				if err := json.NewDecoder(r.Body).Decode(&submitted); err != nil {
					t.Error(err)
					w.WriteHeader(http.StatusBadRequest)
					return
				}
				encoded, _ := json.Marshal(submitted)
				if !strings.Contains(string(encoded), messageIDs[2]) || !strings.Contains(string(encoded), "external_untrusted") {
					t.Error("OpenCode submission lost Garden provenance")
				}
				mu.Lock()
				stored = submitted
				mu.Unlock()
				w.WriteHeader(http.StatusNoContent)
			default:
				w.WriteHeader(http.StatusNotFound)
			}
		}))
		defer hostHTTP.Close()
		host, err := NewOpenCode(hostHTTP.URL, "ses_target", "", "")
		if err != nil {
			t.Fatal(err)
		}
		runCtx, runCancel := context.WithCancel(ctx)
		inbox := &cancelAfterAckInbox{Inbox: bob, cancel: runCancel, acked: make(chan struct{})}
		runner := Runner{Inbox: inbox, Host: host, ConsumerID: consumer, retryAfter: time.Millisecond}
		finished := make(chan error, 1)
		go func() { finished <- runner.Run(runCtx) }()
		awaitAckAndExit(t, inbox.acked, finished)
	})
	empty, err := bob.api.Poll(ctx, garden.PollArgs{ConsumerID: consumer})
	if err != nil || empty.Message != nil {
		t.Fatalf("accepted deliveries remain pending: %#v %v", empty.Message, err)
	}
}

func exerciseInstalledTools(t *testing.T, ctx context.Context, profile Profile) {
	t.Helper()
	binary := os.Getenv("GARDEN_TEST_BINARY")
	if binary == "" {
		t.Skip("set GARDEN_TEST_BINARY to exercise a clean installed toolchain")
	}
	if !filepath.IsAbs(binary) {
		t.Fatal("GARDEN_TEST_BINARY must be absolute")
	}
	root := t.TempDir()
	prefix := filepath.Join(root, "install")
	run := func(program string, args ...string) {
		t.Helper()
		command := exec.CommandContext(ctx, program, args...)
		if output, err := command.CombinedOutput(); err != nil {
			t.Fatalf("installed tooling command failed: %v (%s)", err, output)
		}
	}
	installer, err := filepath.Abs("../../scripts/install-user")
	if err != nil {
		t.Fatal(err)
	}
	run(installer, "--prefix", prefix, "--binary", binary)
	run(installer, "--prefix", prefix, "--binary", binary)
	profilePath := filepath.Join(root, "profile.json")
	data, _ := json.Marshal(profile)
	if err = os.WriteFile(profilePath, data, 0600); err != nil {
		t.Fatal(err)
	}
	installed := filepath.Join(prefix, "bin", "a2a")
	helper := filepath.Join(prefix, "bin", "garden-config")
	run(installed, "doctor", "--profile", profilePath)
	for _, host := range []string{"claude", "codex", "opencode"} {
		config := filepath.Join(root, host, "config.json")
		if host == "codex" {
			config = filepath.Join(root, host, "config.toml")
		}
		args := []string{"--host", host, "--profile", profilePath, "--binary", installed, "--config", config}
		run(helper, args...)
		run(helper, args...)
	}
	client := mcp.NewClient(&mcp.Implementation{Name: "installed-tooling-test", Version: "1"}, nil)
	session, err := client.Connect(ctx, &mcp.CommandTransport{Command: exec.CommandContext(ctx, installed, "connect", "--profile", profilePath)}, nil)
	if err != nil {
		t.Fatal(err)
	}
	defer session.Close()
	tools, err := session.ListTools(ctx, nil)
	if err != nil || len(tools.Tools) != 3 {
		t.Fatalf("installed MCP tools: %v", err)
	}
	result, err := session.CallTool(ctx, &mcp.CallToolParams{Name: "status", Arguments: map[string]any{}})
	if err != nil || result.IsError {
		t.Fatalf("installed MCP status: %v", err)
	}
}

func mustJSON(t *testing.T, value string) string {
	t.Helper()
	encoded, err := json.Marshal(value)
	if err != nil {
		t.Fatal(err)
	}
	return string(encoded)
}

func awaitAckAndExit(t *testing.T, acked <-chan struct{}, finished <-chan error) {
	t.Helper()
	select {
	case <-acked:
	case <-time.After(5 * time.Second):
		t.Fatal("central Garden acknowledgement timed out")
	}
	select {
	case err := <-finished:
		if err != nil {
			t.Fatal(err)
		}
	case <-time.After(5 * time.Second):
		t.Fatal("attention runner did not stop after acknowledgement")
	}
}

func assertPending(t *testing.T, ctx context.Context, inbox *Remote, consumer, messageID string) {
	t.Helper()
	pending, err := inbox.Poll(ctx, consumer)
	if err != nil || pending == nil || pending.Event.ID != messageID {
		t.Fatalf("next central delivery = %#v, %v; want %s", pending, err, messageID)
	}
}
