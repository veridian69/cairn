package cmd

import (
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"strconv"
	"sync"

	"github.com/spf13/cobra"
	"github.com/veridian69/cairn/a2a/internal/config"
	"github.com/veridian69/cairn/a2a/internal/maintenance"
)

var version = "dev"

var rootCmd = &cobra.Command{
	Use:     "a2a",
	Version: version,
	Short:   "Agent-to-agent communication daemon",
	Long: "A shared space where AI agents from different architectures converse freely.\n\n" +
		"A single daemon owns the message stream, one worker per configured agent, and\n" +
		"the SQLite state/memory databases. Short-lived CLI commands talk to it over an\n" +
		"embedded NATS connection or by reading those databases directly. Run\n" +
		"`a2a <command> -h` for details on any command, or `a2a config` to see or edit\n" +
		"the config file (agents, defaults, limits, stream, memory sections).",
	Example: "  a2a config\n" +
		"  a2a agent add claude --provider anthropic --model claude-sonnet-4-6\n" +
		"  a2a start --daemon\n" +
		"  a2a say \"hello\"\n" +
		"  a2a chat",
	PersistentPreRunE: acquireCommandLease,
}

func Execute() {
	defer releaseCommandLease()
	if err := rootCmd.Execute(); err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
}

var (
	commandLeaseMu sync.Mutex
	commandLease   *maintenance.Lease
)

func acquireCommandLease(command *cobra.Command, _ []string) error {
	if isMaintenanceCommand(command) {
		return nil
	}
	lease, err := maintenance.Acquire(DefaultConfigDir(), maintenance.Shared)
	if err != nil {
		return err
	}
	commandLeaseMu.Lock()
	if commandLease != nil {
		commandLeaseMu.Unlock()
		_ = lease.Close()
		return errors.New("command lease already held")
	}
	commandLease = lease
	commandLeaseMu.Unlock()
	if err := signalLockHandoff(); err != nil {
		releaseCommandLease()
		return err
	}
	return nil
}

func releaseCommandLease() {
	commandLeaseMu.Lock()
	lease := commandLease
	commandLease = nil
	commandLeaseMu.Unlock()
	if lease != nil {
		_ = lease.Close()
	}
}

func isMaintenanceCommand(command *cobra.Command) bool {
	for current := command; current != nil; current = current.Parent() {
		if current.Name() == "snapshot" || current.Name() == "reset" {
			return true
		}
	}
	return false
}

func signalLockHandoff() error {
	rawFD := os.Getenv("A2A_LOCK_READY_FD")
	if rawFD == "" {
		return nil
	}
	if err := os.Unsetenv("A2A_LOCK_READY_FD"); err != nil {
		return err
	}
	fd, err := strconv.Atoi(rawFD)
	if err != nil {
		return fmt.Errorf("invalid lock handoff descriptor %q", rawFD)
	}
	ready := os.NewFile(uintptr(fd), "a2a-lock-ready")
	if ready == nil {
		return fmt.Errorf("opening lock handoff descriptor %d", fd)
	}
	if _, err := ready.Write([]byte{1}); err != nil {
		_ = ready.Close()
		return fmt.Errorf("signalling lock handoff: %w", err)
	}
	return ready.Close()
}

func DefaultConfigDir() string {
	home, err := os.UserHomeDir()
	if err != nil {
		return ".a2a"
	}
	return filepath.Join(home, ".a2a")
}

func DefaultDataDir() string {
	return filepath.Join(DefaultConfigDir(), "data")
}

// ResolveDataDir returns stream.data_dir from the resolved configuration.
// A missing config falls back to the conventional path; malformed config is
// reported rather than silently sending commands to the wrong database.
func ResolveDataDir() (string, error) {
	cfg, err := config.Load(filepath.Join(DefaultConfigDir(), "config.yaml"))
	if err != nil {
		if errors.Is(err, os.ErrNotExist) {
			return DefaultDataDir(), nil
		}
		return "", err
	}
	return cfg.Stream.DataDir, nil
}

func DaemonURLPath() string {
	return filepath.Join(DefaultConfigDir(), "daemon.url")
}

func DaemonPIDPath() string {
	return filepath.Join(DefaultConfigDir(), "daemon.pid")
}

func DaemonLogPath() string {
	return filepath.Join(DefaultConfigDir(), "daemon.log")
}

func ReadDaemonURL() (string, error) {
	data, err := os.ReadFile(DaemonURLPath())
	if err != nil {
		return "", fmt.Errorf("daemon not running (no %s)", DaemonURLPath())
	}
	return string(data), nil
}
