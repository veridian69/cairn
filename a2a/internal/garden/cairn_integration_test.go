package garden

import (
	"bufio"
	"context"
	"encoding/json"
	"errors"
	"io"
	"net/http/httptest"
	"os"
	"os/exec"
	"path/filepath"
	"reflect"
	"testing"
	"time"

	"github.com/google/uuid"
	"github.com/modelcontextprotocol/go-sdk/mcp"
	"github.com/veridian69/cairn/a2a/internal/gardenauth"
	"github.com/veridian69/cairn/a2a/internal/transport"
)

// TestRealCairnGardenDelivery exercises the real Python authority boundary, not
// a diagnostic-response imitation. Only explicitly configured runs start it.
func TestRealCairnGardenDelivery(t *testing.T) {
	python := os.Getenv("GARDEN_CAIRN_PYTHON")
	if python == "" {
		t.Skip("set GARDEN_CAIRN_PYTHON to the Cairn test virtualenv Python")
	}
	if !filepath.IsAbs(python) {
		t.Fatal("GARDEN_CAIRN_PYTHON must be an absolute path")
	}
	root, err := filepath.Abs("../../..")
	if err != nil {
		t.Fatal(err)
	}
	ctx, cancel := context.WithTimeout(context.Background(), 45*time.Second)
	defer cancel()
	command := exec.CommandContext(ctx, python, filepath.Join(root, "a2a", "integration", "cairn_fixture.py"))
	command.Dir = root
	// The fixture needs no host credentials or application configuration. Force
	// this worktree's sources and a disposable home even with an editable venv.
	command.Env = []string{"PATH=/usr/bin:/bin", "HOME=" + t.TempDir(), "PYTHONPATH=" + filepath.Join(root, "src"), "PYTHONNOUSERSITE=1", "PYTHONDONTWRITEBYTECODE=1"}
	command.Stderr = io.Discard
	input, err := command.StdinPipe()
	if err != nil {
		t.Fatal(err)
	}
	output, err := command.StdoutPipe()
	if err != nil {
		t.Fatal(err)
	}
	if err = command.Start(); err != nil {
		t.Fatal("could not start disposable Cairn fixture")
	}
	exited := make(chan error, 1)
	go func() { exited <- command.Wait() }()
	defer func() {
		_ = input.Close()
		select {
		case err := <-exited:
			if err != nil && ctx.Err() == nil {
				t.Error("disposable Cairn fixture exited unsuccessfully")
			}
		case <-time.After(5 * time.Second):
			cancel()
			<-exited
			t.Error("disposable Cairn fixture required forced shutdown")
		}
	}()
	lines := make(chan []byte, 2)
	go func() {
		defer close(lines)
		scanner := bufio.NewScanner(output)
		scanner.Buffer(make([]byte, 4096), 64*1024)
		for scanner.Scan() {
			line := append([]byte(nil), scanner.Bytes()...)
			select {
			case lines <- line:
			case <-ctx.Done():
				return
			}
		}
	}()
	next := func() []byte {
		t.Helper()
		select {
		case line, ok := <-lines:
			if !ok {
				t.Fatal("disposable Cairn fixture stopped before replying")
			}
			return line
		case <-ctx.Done():
			t.Fatal("disposable Cairn fixture timed out")
			return nil
		}
	}
	var fixtureConfig struct {
		Endpoint       string           `json:"endpoint"`
		InstanceID     string           `json:"instance_id"`
		Scope          gardenauth.Scope `json:"scope"`
		Classification string           `json:"classification"`
		Actors         map[string]struct {
			ID    string `json:"id"`
			Token string `json:"token"`
		} `json:"actors"`
	}
	if json.Unmarshal(next(), &fixtureConfig) != nil || len(fixtureConfig.Actors) != 3 {
		t.Fatal("disposable Cairn returned invalid configuration")
	}
	// Never format fixtureConfig or tokens into failure output.
	for _, name := range []string{"alice", "bob", "reader"} {
		actor, ok := fixtureConfig.Actors[name]
		if !ok || !canonicalUUID(actor.ID) || actor.Token == "" {
			t.Fatal("disposable Cairn returned invalid actors")
		}
	}
	dir := t.TempDir()
	ns, err := transport.NewServer(dir)
	if err != nil {
		t.Fatal(err)
	}
	defer ns.Stop()
	stream, err := transport.NewStream(ns.ClientURL())
	if err != nil {
		t.Fatal(err)
	}
	defer stream.Close()
	urlfile := filepath.Join(dir, "daemon.url")
	if err = os.WriteFile(urlfile, []byte(ns.ClientURL()), 0600); err != nil {
		t.Fatal(err)
	}
	authConfig := gardenauth.Config{Endpoint: fixtureConfig.Endpoint, InstanceID: fixtureConfig.InstanceID, Scope: fixtureConfig.Scope, Classification: fixtureConfig.Classification}
	cfg := Config{Listen: "127.0.0.1:0", DataDir: dir, DaemonURLFile: urlfile, Auth: authConfig, Principals: map[string]string{}}
	for name, actor := range fixtureConfig.Actors {
		cfg.Principals[actor.ID] = name
	}
	server, err := New(ctx, cfg)
	if err != nil {
		t.Fatal(err)
	}
	defer server.Close()
	endpoint := httptest.NewServer(server.Handler())
	defer endpoint.Close()
	clients := map[string]*Client{}
	for _, name := range []string{"alice", "bob", "reader"} {
		client, err := Dial(ctx, ClientConfig{Endpoint: endpoint.URL + "/mcp", Token: fixtureConfig.Actors[name].Token})
		if err != nil {
			t.Fatalf("%s authentication failed: %v", name, err)
		}
		defer client.Close()
		clients[name] = client
		status, err := client.Status(ctx)
		if err != nil {
			t.Fatal(err)
		}
		if status.Participant != name || status.Binding.InstanceID != fixtureConfig.InstanceID || status.Binding.Classification != "internal" || !reflect.DeepEqual(status.Binding.Scope, fixtureConfig.Scope) {
			t.Fatalf("%s received incorrect fixed binding", name)
		}
	}
	alice, bob, reader := clients["alice"], clients["bob"], clients["reader"]
	_, err = reader.Send(ctx, SendArgs{Content: "reader must not publish"})
	requireCode(t, err, "forbidden")
	sent, err := alice.Send(ctx, SendArgs{Content: "real Cairn authorised delivery", Recipients: []string{"bob"}})
	if err != nil {
		t.Fatal(err)
	}
	owner := uuid.NewString()
	pending, err := bob.Poll(ctx, PollArgs{ConsumerID: owner})
	if err != nil {
		t.Fatal(err)
	}
	if pending.Message == nil || pending.Message.ID != sent.ID || pending.Message.AuthorName != "alice" || !reflect.DeepEqual(pending.Message.Binding.Scope, fixtureConfig.Scope) {
		t.Fatal("real-authority delivery lost message or provenance")
	}
	history, err := reader.Read(ctx, ReadArgs{})
	if err != nil {
		t.Fatal(err)
	}
	if len(history.Messages) != 1 || history.Messages[0].ID != sent.ID {
		t.Fatal("read-only principal could not read shared room history")
	}
	// A caller cannot select a different authority scope in tool arguments.
	result, err := bob.session.CallTool(ctx, &mcp.CallToolParams{Name: "read_messages", Arguments: map[string]any{"scope": map[string]any{"realm": "other", "segments": []any{}}}})
	if err == nil && !result.IsError {
		t.Fatal("caller-selected scope was admitted")
	}
	wrongInstance := authConfig
	wrongInstance.InstanceID = uuid.NewString()
	validator, err := gardenauth.New(wrongInstance)
	if err != nil {
		t.Fatal(err)
	}
	if _, err = validator.Authenticate(ctx, fixtureConfig.Actors["alice"].Token); !errors.Is(err, gardenauth.ErrInvalidResponse) {
		t.Fatal("real Cairn instance mismatch was not rejected")
	}
	wrongScope := authConfig
	wrongScope.Scope = gardenauth.Scope{Realm: "ungranted", Segments: []gardenauth.Segment{}}
	scopeValidator, err := gardenauth.New(wrongScope)
	if err != nil {
		t.Fatal(err)
	}
	outside, err := scopeValidator.Authenticate(ctx, fixtureConfig.Actors["alice"].Token)
	if err != nil || outside.CanRead || outside.CanWrite {
		t.Fatal("real Cairn did not report denied grants for an unrelated realm")
	}
	if err = bob.Acknowledge(ctx, AckArgs{ConsumerID: owner, Receipt: pending.Receipt}); err != nil {
		t.Fatal(err)
	}
	empty, err := bob.Poll(ctx, PollArgs{ConsumerID: owner})
	if err != nil || empty.Message != nil {
		t.Fatal("real-authority acknowledgement did not consume delivery")
	}
	if _, err = io.WriteString(input, "{\"revoke\":\"bob\"}\n"); err != nil {
		t.Fatal("could not request disposable credential revocation")
	}
	var revoked struct {
		Revoked string `json:"revoked"`
	}
	if json.Unmarshal(next(), &revoked) != nil || revoked.Revoked != "bob" {
		t.Fatal("disposable Cairn did not confirm revocation")
	}
	_, err = bob.Read(ctx, ReadArgs{})
	requireCode(t, err, "forbidden")
	_, err = bob.Poll(ctx, PollArgs{ConsumerID: owner})
	requireCode(t, err, "forbidden")
	if _, err = alice.Status(ctx); err != nil {
		t.Fatal("revoking Bob affected Alice")
	}
}
