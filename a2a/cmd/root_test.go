package cmd

import (
	"errors"
	"os"
	"path/filepath"
	"testing"

	"github.com/spf13/cobra"
	"github.com/veridian69/cairn/a2a/internal/maintenance"
)

func TestDefaultPathsAndReadDaemonURL(t *testing.T) {
	home := t.TempDir()
	t.Setenv("HOME", home)

	if got, want := DefaultConfigDir(), filepath.Join(home, ".a2a"); got != want {
		t.Fatalf("DefaultConfigDir() = %q, want %q", got, want)
	}
	if got, want := DefaultDataDir(), filepath.Join(home, ".a2a", "data"); got != want {
		t.Fatalf("DefaultDataDir() = %q, want %q", got, want)
	}
	if got, want := DaemonURLPath(), filepath.Join(home, ".a2a", "daemon.url"); got != want {
		t.Fatalf("DaemonURLPath() = %q, want %q", got, want)
	}
	if got, want := DaemonPIDPath(), filepath.Join(home, ".a2a", "daemon.pid"); got != want {
		t.Fatalf("DaemonPIDPath() = %q, want %q", got, want)
	}
	if got, want := DaemonLogPath(), filepath.Join(home, ".a2a", "daemon.log"); got != want {
		t.Fatalf("DaemonLogPath() = %q, want %q", got, want)
	}

	if err := os.MkdirAll(filepath.Dir(DaemonURLPath()), 0755); err != nil {
		t.Fatalf("creating config dir: %v", err)
	}
	if err := os.WriteFile(DaemonURLPath(), []byte("nats://127.0.0.1:4222"), 0600); err != nil {
		t.Fatalf("writing daemon url file: %v", err)
	}
	url, err := ReadDaemonURL()
	if err != nil {
		t.Fatalf("ReadDaemonURL returned error: %v", err)
	}
	if url != "nats://127.0.0.1:4222" {
		t.Fatalf("ReadDaemonURL() = %q", url)
	}
}

func TestReadDaemonURLErrorWhenMissing(t *testing.T) {
	home := t.TempDir()
	t.Setenv("HOME", home)

	_, err := ReadDaemonURL()
	if err == nil {
		t.Fatal("ReadDaemonURL should fail when daemon url file is missing")
	}
	if got, want := err.Error(), "daemon not running (no "+DaemonURLPath()+")"; got != want {
		t.Fatalf("ReadDaemonURL() error = %q, want %q", got, want)
	}
}

func TestResolveDataDirUsesConfiguredPathAndRejectsMalformedConfig(t *testing.T) {
	home := t.TempDir()
	t.Setenv("HOME", home)
	if err := os.MkdirAll(DefaultConfigDir(), 0700); err != nil {
		t.Fatal(err)
	}
	custom := filepath.Join(home, "custom")
	if err := os.WriteFile(filepath.Join(DefaultConfigDir(), "config.yaml"),
		[]byte("stream:\n  data_dir: "+custom+"\n"), 0600); err != nil {
		t.Fatal(err)
	}
	got, err := ResolveDataDir()
	if err != nil {
		t.Fatal(err)
	}
	if got != custom {
		t.Fatalf("ResolveDataDir = %q, want %q", got, custom)
	}
	if err := os.WriteFile(filepath.Join(DefaultConfigDir(), "config.yaml"), []byte(":\n"), 0600); err != nil {
		t.Fatal(err)
	}
	if _, err := ResolveDataDir(); err == nil {
		t.Fatal("malformed config should be reported")
	}
}

func TestOrdinaryCommandLeaseBlocksMaintenance(t *testing.T) {
	t.Setenv("HOME", t.TempDir())
	command := &cobra.Command{Use: "status"}

	if err := acquireCommandLease(command, nil); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(releaseCommandLease)

	_, err := maintenance.Acquire(DefaultConfigDir(), maintenance.Exclusive)
	var inUse *maintenance.InUseError
	if !errors.As(err, &inUse) {
		t.Fatalf("exclusive lease error = %v, want *maintenance.InUseError", err)
	}
}
