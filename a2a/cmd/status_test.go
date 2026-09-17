package cmd

import (
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/nats-io/nats.go"
	"github.com/veridian69/cairn/a2a/internal/config"
	daemonpkg "github.com/veridian69/cairn/a2a/internal/daemon"
	"github.com/veridian69/cairn/a2a/internal/transport"
)

func TestQueryStatusesReturnsDecodedAgentStatuses(t *testing.T) {
	url := startStatusResponder(t, []daemonpkg.AgentStatus{
		{Name: "claude", State: "thinking", Active: true, Provider: "anthropic", Model: "claude-4", Responsiveness: 0.7},
	})

	statuses, err := queryStatuses(url)
	if err != nil {
		t.Fatalf("queryStatuses returned error: %v", err)
	}
	if len(statuses) != 1 {
		t.Fatalf("expected 1 status, got %d", len(statuses))
	}
	if statuses[0].Name != "claude" || statuses[0].State != "thinking" {
		t.Fatalf("unexpected statuses: %+v", statuses)
	}
}

func TestStatusCommandPrintsDaemonAndOfflineAgents(t *testing.T) {
	home := t.TempDir()
	t.Setenv("HOME", home)
	writeConfigFile(t, home, &config.Config{
		Agents: map[string]config.AgentConfig{
			"claude": {Provider: "anthropic", Model: "claude-4"},
			"gpt":    {Provider: "openai", Model: "gpt-5.4"},
		},
	})
	url := startStatusResponder(t, []daemonpkg.AgentStatus{
		{Name: "claude", State: "thinking", Active: true, Provider: "anthropic", Model: "claude-4", Responsiveness: 0.7, QueueDepth: 2, LastSeenSeq: 11, HourlyCount: 3},
	})
	if err := os.WriteFile(DaemonURLPath(), []byte(url), 0600); err != nil {
		t.Fatalf("writing daemon url: %v", err)
	}

	output := captureStdout(t, func() {
		if err := statusCmd.RunE(statusCmd, nil); err != nil {
			t.Fatalf("statusCmd.RunE returned error: %v", err)
		}
	})
	if !strings.Contains(output, "daemon: running") {
		t.Fatalf("status output missing running banner: %s", output)
	}
	if !strings.Contains(output, "claude") || !strings.Contains(output, "thinking") {
		t.Fatalf("status output missing online status row: %s", output)
	}
	if !strings.Contains(output, "gpt") || !strings.Contains(output, "offline") {
		t.Fatalf("status output missing offline config-backed row: %s", output)
	}
}

func TestStatusCommandFallsBackToAgentListWhenDaemonMissing(t *testing.T) {
	home := t.TempDir()
	t.Setenv("HOME", home)
	writeConfigFile(t, home, &config.Config{
		Agents: map[string]config.AgentConfig{
			"claude": {Provider: "anthropic", Model: "claude-4"},
		},
	})

	output := captureStdout(t, func() {
		if err := statusCmd.RunE(statusCmd, nil); err != nil {
			t.Fatalf("statusCmd.RunE returned error: %v", err)
		}
	})
	if !strings.Contains(output, "daemon: stopped") {
		t.Fatalf("status output missing stopped banner: %s", output)
	}
	if !strings.Contains(output, "claude") || !strings.Contains(output, "offline") {
		t.Fatalf("status fallback output missing offline agent list: %s", output)
	}
}

func startStatusResponder(t *testing.T, statuses []daemonpkg.AgentStatus) string {
	t.Helper()
	server, err := transport.NewServer(t.TempDir())
	if err != nil {
		t.Fatalf("starting transport server: %v", err)
	}
	t.Cleanup(server.Stop)

	nc, err := nats.Connect(server.ClientURL())
	if err != nil {
		t.Fatalf("connecting nats: %v", err)
	}
	t.Cleanup(nc.Close)

	if _, err := nc.Subscribe("a2a.status", func(msg *nats.Msg) {
		data, err := json.Marshal(statuses)
		if err != nil {
			t.Errorf("marshal statuses: %v", err)
			return
		}
		if err := msg.Respond(data); err != nil {
			t.Errorf("responding to status request: %v", err)
		}
	}); err != nil {
		t.Fatalf("subscribing status responder: %v", err)
	}
	nc.Flush()
	if err := nc.LastError(); err != nil {
		t.Fatalf("nats flush error: %v", err)
	}

	return server.ClientURL()
}

func writeConfigFile(t *testing.T, home string, cfg *config.Config) {
	t.Helper()
	cfgDir := filepath.Join(home, ".a2a")
	if err := os.MkdirAll(cfgDir, 0755); err != nil {
		t.Fatalf("creating config dir: %v", err)
	}
	if err := config.Save(filepath.Join(cfgDir, "config.yaml"), cfg); err != nil {
		t.Fatalf("writing config file: %v", err)
	}
}
