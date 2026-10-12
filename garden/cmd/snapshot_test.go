package cmd

import (
	"bytes"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestSnapshotCreateAndListCommands(t *testing.T) {
	home := t.TempDir()
	t.Setenv("HOME", home)
	dataDir := filepath.Join(home, "runtime")
	if err := os.MkdirAll(dataDir, 0700); err != nil {
		t.Fatal(err)
	}
	if err := os.MkdirAll(DefaultConfigDir(), 0700); err != nil {
		t.Fatal(err)
	}
	const literalSecret = "must-not-appear-in-list-output"
	configBytes := []byte(fmt.Sprintf(
		"agents: {}\nstream:\n  data_dir: %s\noperator_note: %s\n",
		dataDir, literalSecret,
	))
	if err := os.WriteFile(filepath.Join(DefaultConfigDir(), "config.yaml"), configBytes, 0600); err != nil {
		t.Fatal(err)
	}

	command := newSnapshotCmd()
	var output bytes.Buffer
	command.SetOut(&output)
	command.SetErr(&output)
	command.SetArgs([]string{"create", "before-test"})
	if err := command.Execute(); err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(output.String(), "snapshot created: ") {
		t.Fatalf("create output = %q", output.String())
	}
	fields := strings.Fields(output.String())
	if len(fields) < 3 {
		t.Fatalf("create output fields = %#v", fields)
	}
	snapshotID := fields[2]

	output.Reset()
	command = newSnapshotCmd()
	command.SetOut(&output)
	command.SetErr(&output)
	command.SetArgs([]string{"list"})
	if err := command.Execute(); err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(output.String(), "before-test") {
		t.Fatalf("list output = %q", output.String())
	}
	if strings.Contains(output.String(), literalSecret) {
		t.Fatalf("list output leaked literal config content: %q", output.String())
	}

	output.Reset()
	command = newSnapshotCmd()
	command.SetOut(&output)
	command.SetErr(&output)
	command.SetArgs([]string{"restore", snapshotID, "--yes"})
	if err := command.Execute(); err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(output.String(), "snapshot restored: "+snapshotID) ||
		!strings.Contains(output.String(), "rollback: ") {
		t.Fatalf("restore output = %q", output.String())
	}
}

func TestResetCommandRequiresYesAndClearsRuntime(t *testing.T) {
	home := t.TempDir()
	t.Setenv("HOME", home)
	dataDir := filepath.Join(home, "runtime")
	if err := os.MkdirAll(dataDir, 0700); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(dataDir, "state.db"), []byte("discard"), 0600); err != nil {
		t.Fatal(err)
	}
	if err := os.MkdirAll(DefaultConfigDir(), 0700); err != nil {
		t.Fatal(err)
	}
	configBytes := []byte(fmt.Sprintf("agents: {}\nstream:\n  data_dir: %s\n", dataDir))
	if err := os.WriteFile(filepath.Join(DefaultConfigDir(), "config.yaml"), configBytes, 0600); err != nil {
		t.Fatal(err)
	}

	command := newResetCmd()
	command.SetArgs(nil)
	if err := command.Execute(); err == nil {
		t.Fatal("non-interactive reset without --yes succeeded")
	}

	command = newResetCmd()
	var output bytes.Buffer
	command.SetOut(&output)
	command.SetErr(&output)
	command.SetArgs([]string{"--yes"})
	if err := command.Execute(); err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(output.String(), "runtime reset") {
		t.Fatalf("reset output = %q", output.String())
	}
	if _, err := os.Stat(filepath.Join(dataDir, "state.db")); !os.IsNotExist(err) {
		t.Fatalf("state.db survived reset: %v", err)
	}
}
