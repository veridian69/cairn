package cmd

import (
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/veridian69/cairn/a2a/internal/state"
)

func TestRedactCommandStoresRedactionAndPrintsConfirmation(t *testing.T) {
	home := t.TempDir()
	t.Setenv("HOME", home)
	if err := os.MkdirAll(DefaultDataDir(), 0755); err != nil {
		t.Fatalf("creating data dir: %v", err)
	}

	db, err := state.Open(filepath.Join(DefaultDataDir(), "state.db"))
	if err != nil {
		t.Fatalf("opening state db: %v", err)
	}
	defer db.Close()

	originalReason := redactReason
	redactReason = "safety"
	defer func() { redactReason = originalReason }()

	output := captureStdout(t, func() {
		if err := redactCmd.RunE(redactCmd, []string{"msg-123"}); err != nil {
			t.Fatalf("redactCmd.RunE returned error: %v", err)
		}
	})

	if !strings.Contains(output, "redacted msg-123 (safety)") {
		t.Fatalf("unexpected redact output: %s", output)
	}
	reason, ok, err := db.RedactionReason("msg-123")
	if err != nil {
		t.Fatalf("looking up redaction reason: %v", err)
	}
	if !ok || reason != "safety" {
		t.Fatalf("redaction reason = (%q, %v), want (%q, true)", reason, ok, "safety")
	}
}
