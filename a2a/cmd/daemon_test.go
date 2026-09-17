package cmd

import (
	"errors"
	"fmt"
	"os"
	"os/exec"
	"os/signal"
	"path/filepath"
	"reflect"
	"strings"
	"syscall"
	"testing"
	"time"

	"github.com/spf13/cobra"
	"github.com/veridian69/cairn/a2a/internal/maintenance"
)

func TestStartChildArgs(t *testing.T) {
	if got, want := startChildArgs(false), []string{"start"}; !reflect.DeepEqual(got, want) {
		t.Fatalf("startChildArgs(false) = %q, want %q", got, want)
	}
	if got, want := startChildArgs(true), []string{"start", "-v"}; !reflect.DeepEqual(got, want) {
		t.Fatalf("startChildArgs(true) = %q, want %q", got, want)
	}
}

func TestStartVerboseFlag(t *testing.T) {
	flag := startCmd.Flags().Lookup("verbose")
	if flag == nil || flag.Shorthand != "v" {
		t.Fatal("start -v flag missing")
	}
}

func TestStartCommandFailsWhenConfigMissing(t *testing.T) {
	t.Setenv("HOME", t.TempDir())

	originalBackground := startBackground
	startBackground = false
	defer func() { startBackground = originalBackground }()

	err := startCmd.RunE(startCmd, nil)
	if err == nil {
		t.Fatal("startCmd.RunE should fail when config is missing")
	}
	if !strings.Contains(err.Error(), "config:") {
		t.Fatalf("unexpected start error: %v", err)
	}
}

func TestStopCommandFailsWhenPIDFileMissing(t *testing.T) {
	t.Setenv("HOME", t.TempDir())

	err := stopCmd.RunE(stopCmd, nil)
	if err == nil {
		t.Fatal("stopCmd.RunE should fail when pid file is missing")
	}
	if err.Error() != "daemon not running" {
		t.Fatalf("unexpected stop error: %v", err)
	}
}

func TestStopCommandFailsForMalformedPID(t *testing.T) {
	t.Setenv("HOME", t.TempDir())
	if err := os.MkdirAll(DefaultConfigDir(), 0755); err != nil {
		t.Fatalf("creating config dir: %v", err)
	}
	if err := os.WriteFile(DaemonPIDPath(), []byte("not-a-pid"), 0600); err != nil {
		t.Fatalf("writing pid file: %v", err)
	}

	err := stopCmd.RunE(stopCmd, nil)
	if err == nil {
		t.Fatal("stopCmd.RunE should fail for malformed pid data")
	}
	if !strings.Contains(err.Error(), "parsing daemon pid") {
		t.Fatalf("unexpected stop error: %v", err)
	}
}

func TestWaitForLockHandoffRequiresChildSignal(t *testing.T) {
	reader, writer, err := os.Pipe()
	if err != nil {
		t.Fatal(err)
	}
	defer reader.Close()
	go func() {
		time.Sleep(20 * time.Millisecond)
		_, _ = writer.Write([]byte{1})
		_ = writer.Close()
	}()

	started := time.Now()
	if err := waitForLockHandoff(reader, time.Second); err != nil {
		t.Fatal(err)
	}
	if time.Since(started) < 15*time.Millisecond {
		t.Fatal("lock handoff returned before the child signal")
	}
}

func TestBackgroundStartTransfersLeaseToChild(t *testing.T) {
	if os.Getenv("A2A_TEST_LOCK_HANDOFF_CHILD") == "1" {
		runLockHandoffChild(t)
		return
	}

	home := t.TempDir()
	t.Setenv("HOME", home)
	t.Setenv("A2A_TEST_LOCK_HANDOFF_CHILD", "1")
	originalBackground := startBackground
	originalNewChild := newDaemonChild
	startBackground = true
	newDaemonChild = func(bool) *exec.Cmd {
		return exec.Command(
			os.Args[0],
			"-test.run=^TestBackgroundStartTransfersLeaseToChild$",
		)
	}
	defer func() {
		startBackground = originalBackground
		newDaemonChild = originalNewChild
		releaseCommandLease()
	}()

	parentCommand := &cobra.Command{Use: "start"}
	if err := acquireCommandLease(parentCommand, nil); err != nil {
		t.Fatal(err)
	}
	if err := startCmd.RunE(startCmd, nil); err != nil {
		t.Fatal(err)
	}
	releaseCommandLease()

	_, err := maintenance.Acquire(DefaultConfigDir(), maintenance.Exclusive)
	var inUse *maintenance.InUseError
	if !errors.As(err, &inUse) {
		t.Fatalf("exclusive lease after handoff = %v, want *maintenance.InUseError", err)
	}

	pidBytes, err := os.ReadFile(filepath.Join(DefaultConfigDir(), "handoff-child.pid"))
	if err != nil {
		t.Fatal(err)
	}
	var pid int
	if _, err := fmt.Sscanf(string(pidBytes), "%d", &pid); err != nil {
		t.Fatal(err)
	}
	process, err := os.FindProcess(pid)
	if err != nil {
		t.Fatal(err)
	}
	if err := process.Signal(syscall.SIGTERM); err != nil {
		t.Fatal(err)
	}
	if _, err := process.Wait(); err != nil {
		t.Fatal(err)
	}
}

func TestBackgroundStartKillsChildWhenHandoffFails(t *testing.T) {
	if os.Getenv("A2A_TEST_LOCK_HANDOFF_STALL") == "1" {
		runStalledLockHandoffChild(t)
		return
	}

	home := t.TempDir()
	t.Setenv("HOME", home)
	t.Setenv("A2A_TEST_LOCK_HANDOFF_STALL", "1")
	originalBackground := startBackground
	originalTimeout := lockHandoffTimeout
	originalNewChild := newDaemonChild
	startBackground = true
	lockHandoffTimeout = 500 * time.Millisecond
	newDaemonChild = func(bool) *exec.Cmd {
		return exec.Command(
			os.Args[0],
			"-test.run=^TestBackgroundStartKillsChildWhenHandoffFails$",
		)
	}
	defer func() {
		startBackground = originalBackground
		lockHandoffTimeout = originalTimeout
		newDaemonChild = originalNewChild
		releaseCommandLease()
	}()

	parentCommand := &cobra.Command{Use: "start"}
	if err := acquireCommandLease(parentCommand, nil); err != nil {
		t.Fatal(err)
	}
	err := startCmd.RunE(startCmd, nil)
	if err == nil || !strings.Contains(err.Error(), "daemon lock handoff") {
		t.Fatalf("start error = %v, want daemon lock handoff failure", err)
	}

	pidBytes, err := os.ReadFile(filepath.Join(DefaultConfigDir(), "stalled-child.pid"))
	if err != nil {
		t.Fatal(err)
	}
	var pid int
	if _, err := fmt.Sscanf(string(pidBytes), "%d", &pid); err != nil {
		t.Fatal(err)
	}
	if err := syscall.Kill(pid, 0); !errors.Is(err, syscall.ESRCH) {
		t.Fatalf("stalled child %d was not killed and reaped: %v", pid, err)
	}
}

func runLockHandoffChild(t *testing.T) {
	if err := os.MkdirAll(DefaultConfigDir(), 0700); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(
		filepath.Join(DefaultConfigDir(), "handoff-child.pid"),
		[]byte(fmt.Sprintf("%d\n", os.Getpid())),
		0600,
	); err != nil {
		t.Fatal(err)
	}
	command := &cobra.Command{Use: "start"}
	if err := acquireCommandLease(command, nil); err != nil {
		t.Fatal(err)
	}
	defer releaseCommandLease()
	signals := make(chan os.Signal, 1)
	signal.Notify(signals, syscall.SIGTERM)
	defer signal.Stop(signals)
	<-signals
}

func runStalledLockHandoffChild(t *testing.T) {
	if err := os.MkdirAll(DefaultConfigDir(), 0700); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(
		filepath.Join(DefaultConfigDir(), "stalled-child.pid"),
		[]byte(fmt.Sprintf("%d\n", os.Getpid())),
		0600,
	); err != nil {
		t.Fatal(err)
	}
	select {}
}
