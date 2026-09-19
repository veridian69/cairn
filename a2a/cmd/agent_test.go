package cmd

import (
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/veridian69/cairn/a2a/internal/config"
	daemonpkg "github.com/veridian69/cairn/a2a/internal/daemon"
)

func TestLoadOrCreateConfigCreatesEmptyConfigForMissingFile(t *testing.T) {
	path := filepath.Join(t.TempDir(), "nested", "config.yaml")

	cfg, err := loadOrCreateConfig(path)
	if err != nil {
		t.Fatalf("loadOrCreateConfig returned error: %v", err)
	}
	if cfg == nil {
		t.Fatal("loadOrCreateConfig returned nil config")
	}
	if cfg.Agents == nil {
		t.Fatal("loadOrCreateConfig should initialize Agents map")
	}
	if len(cfg.Agents) != 0 {
		t.Fatalf("expected empty Agents map, got %d entries", len(cfg.Agents))
	}
	if _, err := os.Stat(filepath.Dir(path)); err != nil {
		t.Fatalf("expected config directory to be created: %v", err)
	}
}

func TestAgentListShowsDaemonStateWhenAvailable(t *testing.T) {
	home := t.TempDir()
	t.Setenv("HOME", home)
	writeConfigFile(t, home, &config.Config{
		Agents: map[string]config.AgentConfig{
			"claude": {Provider: "anthropic", Model: "claude-4"},
			"gpt":    {Provider: "openai", Model: "gpt-5.4"},
		},
	})
	url := startStatusResponder(t, []daemonpkg.AgentStatus{
		{Name: "claude", State: "paused", Active: false, Provider: "anthropic", Model: "claude-4"},
	})
	if err := os.WriteFile(DaemonURLPath(), []byte(url), 0600); err != nil {
		t.Fatalf("writing daemon url: %v", err)
	}

	output := captureStdout(t, func() {
		if err := agentListCmd.RunE(agentListCmd, nil); err != nil {
			t.Fatalf("agentListCmd.RunE returned error: %v", err)
		}
	})
	if !strings.Contains(output, "claude") || !strings.Contains(output, "paused") {
		t.Fatalf("agent list output missing daemon-backed paused state: %s", output)
	}
	if !strings.Contains(output, "gpt") || !strings.Contains(output, "offline") {
		t.Fatalf("agent list output missing offline agent row: %s", output)
	}
}

func TestResolveAgentAPIKeyPrefersExplicitFlag(t *testing.T) {
	home := t.TempDir()
	t.Setenv("HOME", home)
	t.Setenv("OPENAI_API_KEY", "environment-key")
	writeSetFile(t, home, "set-api.sh", "OPENAI_API_KEY=file-key\n")

	got, err := resolveAgentAPIKey("openai", "explicit-key", true)
	if err != nil {
		t.Fatalf("resolveAgentAPIKey returned error: %v", err)
	}
	if got != "explicit-key" {
		t.Fatalf("API key = %q, want explicit flag value", got)
	}
}

func TestResolveAgentAPIKeyUsesProviderEnvironmentReference(t *testing.T) {
	for _, tc := range []struct {
		provider string
		envName  string
	}{
		{provider: "anthropic", envName: "ANTHROPIC_API_KEY"},
		{provider: "openai", envName: "OPENAI_API_KEY"},
		{provider: "google", envName: "GOOGLE_API_KEY"},
		{provider: "deepseek", envName: "DEEPSEEK_API_KEY"},
	} {
		t.Run(tc.provider, func(t *testing.T) {
			clearAgentAPIKeyEnv(t)
			t.Setenv(tc.envName, "environment-key")

			got, err := resolveAgentAPIKey(tc.provider, "", false)
			if err != nil {
				t.Fatalf("resolveAgentAPIKey returned error: %v", err)
			}
			if want := "$" + tc.envName; got != want {
				t.Fatalf("API key = %q, want %q", got, want)
			}
		})
	}
}

func TestResolveGoogleAPIKeyUsesGeminiEnvironmentAlias(t *testing.T) {
	home := t.TempDir()
	t.Setenv("HOME", home)
	clearAgentAPIKeyEnv(t)
	t.Setenv("GEMINI_API_KEY", "gemini-environment-key")

	got, err := resolveAgentAPIKey("google", "", false)
	if err != nil {
		t.Fatalf("resolveAgentAPIKey returned error: %v", err)
	}
	if got != "$GEMINI_API_KEY" {
		t.Fatalf("API key = %q, want $GEMINI_API_KEY", got)
	}
}

func TestResolveGoogleAPIKeyRejectsDistinctEnvironmentAliases(t *testing.T) {
	home := t.TempDir()
	t.Setenv("HOME", home)
	clearAgentAPIKeyEnv(t)
	t.Setenv("GOOGLE_API_KEY", "google-secret")
	t.Setenv("GEMINI_API_KEY", "gemini-secret")

	_, err := resolveAgentAPIKey("google", "", false)
	if err == nil {
		t.Fatal("resolveAgentAPIKey returned nil error for distinct aliases")
	}
	if strings.Contains(err.Error(), "google-secret") ||
		strings.Contains(err.Error(), "gemini-secret") {
		t.Fatalf("error leaked API key value: %v", err)
	}
	for _, name := range []string{"GOOGLE_API_KEY", "GEMINI_API_KEY"} {
		if !strings.Contains(err.Error(), name) {
			t.Fatalf("error %q does not identify %s", err, name)
		}
	}
}

func TestResolveAgentAPIKeyUsesMatchingLiteralFromSetFiles(t *testing.T) {
	home := t.TempDir()
	t.Setenv("HOME", home)
	clearAgentAPIKeyEnv(t)
	writeSetFile(t, home, "set-api.sh", "OPENAI_API_KEY=file-key\n")
	writeSetFile(t, home, "set-export.sh", "export OPENAI_API_KEY=file-key\n")
	writeSetFile(t, home, "set-double.sh", "export OPENAI_API_KEY=\"file-key\"\n")
	writeSetFile(t, home, "set-local.sh", "export OPENAI_API_KEY='file-key'\n")

	got, err := resolveAgentAPIKey("openai", "", false)
	if err != nil {
		t.Fatalf("resolveAgentAPIKey returned error: %v", err)
	}
	if got != "file-key" {
		t.Fatalf("API key = %q, want file literal", got)
	}
}

func TestResolveGoogleAPIKeyUsesGeminiLiteralFromSetFile(t *testing.T) {
	home := t.TempDir()
	t.Setenv("HOME", home)
	clearAgentAPIKeyEnv(t)
	writeSetFile(t, home, "set-google.sh", "export GEMINI_API_KEY='gemini-file-key'\n")

	got, err := resolveAgentAPIKey("google", "", false)
	if err != nil {
		t.Fatalf("resolveAgentAPIKey returned error: %v", err)
	}
	if got != "gemini-file-key" {
		t.Fatalf("API key = %q, want Gemini file literal", got)
	}
}

func TestAgentAddWritesEnvironmentAPIKeyReference(t *testing.T) {
	home := t.TempDir()
	t.Setenv("HOME", home)
	clearAgentAPIKeyEnv(t)
	t.Setenv("OPENAI_API_KEY", "environment-key")
	resetAgentAddFlags(t)
	agentProvider = "openai"
	agentModel = "test-model"

	if err := agentAddCmd.RunE(agentAddCmd, []string{"test-agent"}); err != nil {
		t.Fatalf("agent add returned error: %v", err)
	}
	cfg, err := config.LoadRaw(filepath.Join(DefaultConfigDir(), "config.yaml"))
	if err != nil {
		t.Fatalf("loading generated config: %v", err)
	}
	if got := cfg.Agents["test-agent"].APIKey; got != "$OPENAI_API_KEY" {
		t.Fatalf("written API key = %q, want environment reference", got)
	}
}

func TestAgentAddWritesLiteralDiscoveredFromSetFile(t *testing.T) {
	home := t.TempDir()
	t.Setenv("HOME", home)
	clearAgentAPIKeyEnv(t)
	writeSetFile(t, home, "set-api.sh", "export OPENAI_API_KEY='file-key'\n")
	resetAgentAddFlags(t)
	agentProvider = "openai"
	agentModel = "test-model"

	if err := agentAddCmd.RunE(agentAddCmd, []string{"test-agent"}); err != nil {
		t.Fatalf("agent add returned error: %v", err)
	}
	cfg, err := config.LoadRaw(filepath.Join(DefaultConfigDir(), "config.yaml"))
	if err != nil {
		t.Fatalf("loading generated config: %v", err)
	}
	if got := cfg.Agents["test-agent"].APIKey; got != "file-key" {
		t.Fatalf("written API key = %q, want discovered literal", got)
	}
}

func TestResolveAgentAPIKeyRejectsNonLiteralSetFileAssignments(t *testing.T) {
	home := t.TempDir()
	t.Setenv("HOME", home)
	clearAgentAPIKeyEnv(t)
	marker := filepath.Join(home, "must-not-exist")
	writeSetFile(t, home, "set-api.sh", strings.Join([]string{
		"OPENAI_API_KEY=$OTHER_KEY",
		"OPENAI_API_KEY='$AWS_SECRET_ACCESS_KEY'",
		"export OPENAI_API_KEY=$(touch " + marker + ")",
		"export OPENAI_API_KEY=literal-key; command-not-run",
	}, "\n"))

	_, err := resolveAgentAPIKey("openai", "", false)
	if err == nil {
		t.Fatal("resolveAgentAPIKey returned nil error for non-literal assignments")
	}
	if strings.Contains(err.Error(), "command-not-run") || strings.Contains(err.Error(), "literal-key") {
		t.Fatalf("error leaked set-file content: %v", err)
	}
	if _, statErr := os.Stat(marker); !os.IsNotExist(statErr) {
		t.Fatalf("set file appears to have been executed: stat error %v", statErr)
	}
}

func TestResolveAgentAPIKeyRejectsDistinctSetFileValuesWithoutLeakingThem(t *testing.T) {
	home := t.TempDir()
	t.Setenv("HOME", home)
	clearAgentAPIKeyEnv(t)
	first := writeSetFile(t, home, "set-api.sh", "OPENAI_API_KEY=first-secret\n")
	second := writeSetFile(t, home, "set-local.sh", "export OPENAI_API_KEY=second-secret\n")

	_, err := resolveAgentAPIKey("openai", "", false)
	if err == nil {
		t.Fatal("resolveAgentAPIKey returned nil error for distinct file values")
	}
	for _, path := range []string{first, second} {
		if !strings.Contains(err.Error(), path) {
			t.Fatalf("error %q does not identify source %q", err, path)
		}
	}
	if strings.Contains(err.Error(), "first-secret") || strings.Contains(err.Error(), "second-secret") {
		t.Fatalf("error leaked API key value: %v", err)
	}
}

func TestResolveAgentAPIKeyErrorsWhenNoKeyIsAvailable(t *testing.T) {
	home := t.TempDir()
	t.Setenv("HOME", home)
	clearAgentAPIKeyEnv(t)

	_, err := resolveAgentAPIKey("openai", "", false)
	if err == nil {
		t.Fatal("resolveAgentAPIKey returned nil error without an API key")
	}
	if strings.Contains(err.Error(), "OPENAI_API_KEY") {
		t.Fatalf("error should not imply an API key was found: %v", err)
	}
}

func clearAgentAPIKeyEnv(t *testing.T) {
	t.Helper()
	for _, name := range []string{
		"ANTHROPIC_API_KEY",
		"OPENAI_API_KEY",
		"GOOGLE_API_KEY",
		"GEMINI_API_KEY",
		"DEEPSEEK_API_KEY",
	} {
		t.Setenv(name, "")
	}
}

func writeSetFile(t *testing.T, home, name, contents string) string {
	t.Helper()
	path := filepath.Join(home, name)
	if err := os.WriteFile(path, []byte(contents), 0600); err != nil {
		t.Fatalf("writing %s: %v", path, err)
	}
	return path
}

func resetAgentAddFlags(t *testing.T) {
	t.Helper()
	oldProvider, oldModel, oldSystem, oldAPIKey := agentProvider, agentModel, agentSystem, agentAPIKey
	oldTemp, oldResp := agentTemp, agentResp
	changed := make(map[string]bool)
	for _, name := range []string{"provider", "model", "api-key", "system", "temperature", "responsiveness"} {
		flag := agentAddCmd.Flags().Lookup(name)
		changed[name] = flag.Changed
		flag.Changed = false
	}
	agentProvider, agentModel, agentSystem, agentAPIKey = "", "", "", ""
	agentTemp, agentResp = 0, 0
	t.Cleanup(func() {
		agentProvider, agentModel, agentSystem, agentAPIKey = oldProvider, oldModel, oldSystem, oldAPIKey
		agentTemp, agentResp = oldTemp, oldResp
		for name, wasChanged := range changed {
			agentAddCmd.Flags().Lookup(name).Changed = wasChanged
		}
	})
}
