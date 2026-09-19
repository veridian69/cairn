package cmd

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestConfigCommandPrintsPathAndCreatesConfigWhenEditorUnset(t *testing.T) {
	home := t.TempDir()
	t.Setenv("HOME", home)
	t.Setenv("EDITOR", "")

	output := captureStdout(t, func() {
		if err := configCmd.RunE(configCmd, nil); err != nil {
			t.Fatalf("configCmd.RunE returned error: %v", err)
		}
	})

	cfgPath := filepath.Join(home, ".a2a", "config.yaml")
	if strings.TrimSpace(output) != cfgPath {
		t.Fatalf("config command output = %q, want %q", strings.TrimSpace(output), cfgPath)
	}
	if _, err := os.Stat(cfgPath); err != nil {
		t.Fatalf("expected config file to be created at %s: %v", cfgPath, err)
	}
}
