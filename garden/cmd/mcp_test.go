package cmd

import (
	"os"
	"path/filepath"
	"testing"
)

func TestValidateMCPNameRejectsConfiguredAgent(t *testing.T) {
	t.Setenv("HOME", t.TempDir())
	dir := DefaultConfigDir()
	if err := os.MkdirAll(dir, 0o700); err != nil {
		t.Fatal(err)
	}
	cfgYAML := "agents:\n  claude:\n    provider: anthropic\n    model: claude-sonnet-4-6\n"
	if err := os.WriteFile(filepath.Join(dir, "config.yaml"), []byte(cfgYAML), 0o600); err != nil {
		t.Fatal(err)
	}

	if err := validateMCPName("claude"); err == nil {
		t.Fatal("expected error for name matching a configured agent")
	}
	if err := validateMCPName("codex"); err != nil {
		t.Fatalf("unexpected error for unused name: %v", err)
	}
}

func TestValidateMCPNameAllowsMissingConfig(t *testing.T) {
	t.Setenv("HOME", t.TempDir())
	if err := validateMCPName("codex"); err != nil {
		t.Fatalf("missing config must not block: %v", err)
	}
}

func TestValidateMCPNameRequiresName(t *testing.T) {
	t.Setenv("HOME", t.TempDir())
	if err := validateMCPName(""); err == nil {
		t.Fatal("expected error for empty name")
	}
}
