package garden

import (
	"context"
	"encoding/json"
	"net"
	"os"
	"path/filepath"
	"testing"
	"time"

	"github.com/google/uuid"
	"github.com/nats-io/nats.go/jetstream"
	"github.com/veridian69/cairn/a2a/internal/model"
	"github.com/veridian69/cairn/a2a/internal/transport"
)

func hostConfigForTest(t *testing.T, f *fixture) HostConfig {
	t.Helper()
	cfg := f.cfg
	cfg.DataDir = t.TempDir()
	cfg.DaemonURLFile = filepath.Join(cfg.DataDir, "run", "daemon.url")
	cfg.Listen = "127.0.0.1:0"
	return HostConfig{Gateway: cfg, Stream: StreamConfig{MaxAge: "24h", MaxBytes: 1024 * 1024}}
}
func TestHostRunsAndRestartsWithoutMemoryOrWorkers(t *testing.T) {
	f := newFixture(t)
	cfg := hostConfigForTest(t, f)
	var receipt, messageID string
	for round := 0; round < 3; round++ {
		ctx, cancel := context.WithCancel(context.Background())
		runtime, err := startHost(ctx, cfg)
		if err != nil {
			cancel()
			t.Fatal(err)
		}
		done := make(chan error, 1)
		go func() { done <- runtime.run(ctx) }()
		client, err := Dial(ctx, ClientConfig{Endpoint: "http://" + runtime.listener.Addr().String() + "/mcp", Token: "bob"})
		if err != nil {
			cancel()
			<-done
			t.Fatal(err)
		}
		status, err := client.Status(ctx)
		if err != nil || status.Participant != "bob" {
			t.Fatal("host not ready for authenticated MCP")
		}
		owner := "44444444-4444-4444-8444-444444444444"
		if round == 0 {
			sender, e := Dial(ctx, ClientConfig{Endpoint: "http://" + runtime.listener.Addr().String() + "/mcp", Token: "alice"})
			if e != nil {
				t.Fatal(e)
			}
			sent, e := sender.Send(ctx, SendArgs{Content: "survives host restart", Recipients: []string{"bob"}})
			_ = sender.Close()
			if e != nil {
				t.Fatal(e)
			}
			messageID = sent.ID
		}
		pending, err := client.Poll(ctx, PollArgs{ConsumerID: owner})
		if err != nil || pending.Message == nil || pending.Message.ID != messageID {
			t.Fatal("host lost retained delivery")
		}
		if round == 0 {
			receipt = pending.Receipt
		} else {
			if receipt != pending.Receipt {
				t.Fatal("host restart changed receipt")
			}
			if round == 2 {
				if err = client.Acknowledge(ctx, AckArgs{ConsumerID: owner, Receipt: receipt}); err != nil {
					t.Fatal(err)
				}
			}
		}
		_ = client.Close()
		cancel()
		select {
		case err := <-done:
			if err != nil {
				t.Fatal(err)
			}
		case <-time.After(5 * time.Second):
			t.Fatal("host did not stop promptly")
		}
		if _, err = os.Stat(cfg.Gateway.DaemonURLFile); !os.IsNotExist(err) {
			t.Fatal("host left its daemon URL after shutdown")
		}
		if _, err = os.Stat(filepath.Join(cfg.Gateway.DataDir, "memory.db")); !os.IsNotExist(err) {
			t.Fatal("host created a local memory store")
		}
	}
}
func TestHostStartupFailureReleasesResources(t *testing.T) {
	f := newFixture(t)
	cfg := hostConfigForTest(t, f)
	occupied, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	cfg.Gateway.Listen = occupied.Addr().String()
	if err = RunHost(context.Background(), cfg); err == nil {
		t.Fatal("host ignored an occupied port")
	}
	_ = occupied.Close()
	if _, err = os.Stat(cfg.Gateway.DaemonURLFile); !os.IsNotExist(err) {
		t.Fatal("failed startup published a daemon URL")
	}
	runtime, err := startHost(context.Background(), cfg)
	if err != nil {
		t.Fatal("failed startup retained runtime ownership")
	}
	_ = runtime.close()
	cfg.Gateway.TLSCertFile = filepath.Join(t.TempDir(), "missing.crt")
	cfg.Gateway.TLSKeyFile = filepath.Join(t.TempDir(), "missing.key")
	if err = RunHost(context.Background(), cfg); err == nil {
		t.Fatal("missing TLS files accepted")
	}
	if _, err = os.Stat(cfg.Gateway.DaemonURLFile); !os.IsNotExist(err) {
		t.Fatal("invalid TLS startup published a daemon URL")
	}
}
func TestHostRefusesForeignURLAndConcurrentRuntime(t *testing.T) {
	f := newFixture(t)
	cfg := hostConfigForTest(t, f)
	if err := os.MkdirAll(filepath.Dir(cfg.Gateway.DaemonURLFile), 0700); err != nil {
		t.Fatal(err)
	}
	target := filepath.Join(t.TempDir(), "unrelated")
	if err := os.WriteFile(target, []byte("preserve"), 0600); err != nil {
		t.Fatal(err)
	}
	if err := os.Symlink(target, cfg.Gateway.DaemonURLFile); err != nil {
		t.Fatal(err)
	}
	if _, err := startHost(context.Background(), cfg); err == nil {
		t.Fatal("host overwrote a URL symlink")
	}
	raw, _ := os.ReadFile(target)
	if string(raw) != "preserve" {
		t.Fatal("foreign URL target changed")
	}
	_ = os.Remove(cfg.Gateway.DaemonURLFile)
	runtime, err := startHost(context.Background(), cfg)
	if err != nil {
		t.Fatal(err)
	}
	defer runtime.close()
	other := cfg
	other.Gateway.DaemonURLFile = filepath.Join(t.TempDir(), "daemon.url")
	if duplicate, err := startHost(context.Background(), other); err == nil {
		_ = duplicate.close()
		t.Fatal("host allowed a second runtime on same store")
	}
}
func TestHostConfigIsStrictAndRetentionIsValidated(t *testing.T) {
	f := newFixture(t)
	cfg := hostConfigForTest(t, f)
	path := filepath.Join(t.TempDir(), "host.json")
	raw, _ := json.Marshal(cfg)
	if err := os.WriteFile(path, raw, 0600); err != nil {
		t.Fatal(err)
	}
	if _, err := LoadHostConfig(path); err != nil {
		t.Fatal(err)
	}
	for _, age := range []string{"-1h", "nonsense"} {
		cfg.Stream.MaxAge = age
		raw, _ = json.Marshal(cfg)
		_ = os.WriteFile(path, raw, 0600)
		if _, err := LoadHostConfig(path); err == nil {
			t.Fatal("invalid retention accepted")
		}
	}
	cfg.Stream.MaxAge = "0"
	cfg.Stream.MaxBytes = -1
	raw, _ = json.Marshal(cfg)
	_ = os.WriteFile(path, raw, 0600)
	if _, err := LoadHostConfig(path); err == nil {
		t.Fatal("negative retention accepted")
	}
	cfg.Stream.MaxBytes = 0
	cfg.Gateway.Principals[uuid.NewString()] = "charlie"
	raw, _ = json.Marshal(cfg)
	raw = append(raw[:len(raw)-1], []byte(`,"Gateway":{}}`)...)
	_ = os.WriteFile(path, raw, 0600)
	if _, err := LoadHostConfig(path); err == nil {
		t.Fatal("aliased runtime configuration accepted")
	}
}

func TestHostRejectedBindingDoesNotChangeRetention(t *testing.T) {
	f := newFixture(t)
	cfg := hostConfigForTest(t, f)
	runtime, err := startHost(context.Background(), cfg)
	if err != nil {
		t.Fatal(err)
	}
	local, err := transport.NewStream(runtime.nats.ClientURL())
	if err != nil {
		t.Fatal(err)
	}
	m := model.NewMessage(model.Participant{ID: uuid.NewString(), Name: "local"}, "keep this history", nil)
	if err = local.Publish(context.Background(), m); err != nil {
		t.Fatal(err)
	}
	local.Close()
	_ = runtime.close()
	wrong := cfg
	wrong.Gateway.Auth.Classification = "public"
	wrong.Stream.MaxAge = "1h"
	rejected, err := startHost(context.Background(), wrong)
	if err == nil {
		_ = rejected.close()
		t.Fatal("host accepted a different authority binding")
	}
	if _, err = os.Stat(cfg.Gateway.DaemonURLFile); !os.IsNotExist(err) {
		t.Fatal("rejected binding left a daemon URL")
	}
	// Inspect the existing stream without installing a fresh retention policy.
	ns, err := transport.NewServer(cfg.Gateway.DataDir)
	if err != nil {
		t.Fatal("failed host retained its local daemon")
	}
	defer ns.Stop()
	stream, err := transport.NewStream(ns.ClientURL())
	if err != nil {
		t.Fatal(err)
	}
	defer stream.Close()
	js, err := jetstream.New(stream.NATSConn())
	if err != nil {
		t.Fatal(err)
	}
	persisted, err := js.Stream(context.Background(), transport.StreamName)
	if err != nil {
		t.Fatal(err)
	}
	info, err := persisted.Info(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	if info.Config.MaxAge != 24*time.Hour {
		t.Fatal("rejected binding changed the persisted retention policy")
	}
	entries, err := stream.Tail(context.Background(), 1)
	if err != nil || len(entries) != 1 || entries[0].ID != m.ID {
		t.Fatal("rejected host binding changed retention and deleted history")
	}
}
