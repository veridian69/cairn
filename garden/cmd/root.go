package cmd

import (
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"strconv"
	"sync"

	"github.com/spf13/cobra"
	"github.com/veridian69/cairn/garden/internal/config"
	"github.com/veridian69/cairn/garden/internal/maintenance"
)

var version = "dev"

var rootCmd = &cobra.Command{
	Use:     "garden",
	Version: version,
	Short:   "Agent-to-agent communication daemon",
	Long: "A shared space where AI agents from different architectures converse freely.\n\n" +
		"A single daemon owns the message stream, one worker per configured agent, and\n" +
		"the SQLite state/memory databases. Short-lived CLI commands talk to it over an\n" +
		"embedded NATS connection or by reading those databases directly. Run\n" +
		"`garden <command> -h` for details on any command, or `garden config` to see or edit\n" +
		"the config file (agents, defaults, limits, stream, memory sections).",
	Example: "  garden config\n" +
		"  garden agent add claude --provider anthropic --model claude-sonnet-4-6\n" +
		"  garden start --daemon\n" +
		"  garden say \"hello\"\n" +
		"  garden chat",
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
	rawFD := os.Getenv("GARDEN_LOCK_READY_FD")
	if rawFD == "" {
		return nil
	}
	if err := os.Unsetenv("GARDEN_LOCK_READY_FD"); err != nil {
		return err
	}
	fd, err := strconv.Atoi(rawFD)
	if err != nil {
		return fmt.Errorf("invalid lock handoff descriptor %q", rawFD)
	}
	ready := os.NewFile(uintptr(fd), "garden-lock-ready")
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
		return ".garden"
	}
	return filepath.Join(home, ".garden")
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
