package cmd

import (
	"context"
	"fmt"
	"log"
	"os"
	"os/exec"
	"os/signal"
	"path/filepath"
	"syscall"
	"time"

	"github.com/spf13/cobra"
	"github.com/veridian69/cairn/a2a/internal/config"
	"github.com/veridian69/cairn/a2a/internal/daemon"
)

var (
	startBackground    bool
	startVerbose       bool
	lockHandoffTimeout = 5 * time.Second
	newDaemonChild     = func(verbose bool) *exec.Cmd {
		return exec.Command(os.Args[0], startChildArgs(verbose)...)
	}
)

func startChildArgs(verbose bool) []string {
	args := []string{"start"}
	if verbose {
		args = append(args, "-v")
	}
	return args
}

var startCmd = &cobra.Command{
	Use:   "start",
	Short: "Start the a2a daemon",
	Long: "Starts the embedded daemon: the NATS/JetStream server, the SQLite state and\n" +
		"memory databases, and one serial worker per agent configured in config.yaml.\n" +
		"Runs in the foreground by default — stop it with Ctrl-C. --daemon forks a\n" +
		"background copy instead, writing daemon.pid and daemon.log. -v adds redacted\n" +
		"provider response bodies capped at 1 KiB to provider error diagnostics,\n" +
		"including in daemon.log when --daemon is used.\n\n" +
		"Config is read once at startup; editing config.yaml or running `a2a agent add`\n" +
		"has no effect on an already-running daemon.",
	Example: "  a2a start\n  a2a start -v\n  a2a start --daemon\n  a2a start --daemon -v",
	RunE: func(cmd *cobra.Command, args []string) error {
		if startBackground && os.Getenv("A2A_DAEMON_CHILD") != "1" {
			logPath := DaemonLogPath()
			if err := os.MkdirAll(filepath.Dir(logPath), 0755); err != nil {
				return err
			}
			logFile, err := os.OpenFile(logPath, os.O_CREATE|os.O_WRONLY|os.O_APPEND, 0600)
			if err != nil {
				return err
			}
			defer logFile.Close()

			child := newDaemonChild(startVerbose)
			child.Stdout = logFile
			child.Stderr = logFile
			readyReader, readyWriter, err := os.Pipe()
			if err != nil {
				return fmt.Errorf("creating daemon lock handoff: %w", err)
			}
			defer readyReader.Close()
			child.ExtraFiles = []*os.File{readyWriter}
			child.Env = append(os.Environ(),
				"A2A_DAEMON_CHILD=1",
				"A2A_LOCK_READY_FD=3",
			)
			child.SysProcAttr = &syscall.SysProcAttr{Setsid: true}
			if err := child.Start(); err != nil {
				_ = readyWriter.Close()
				return err
			}
			_ = readyWriter.Close()
			if err := waitForLockHandoff(readyReader, lockHandoffTimeout); err != nil {
				_ = child.Process.Kill()
				_, _ = child.Process.Wait()
				return fmt.Errorf("daemon lock handoff: %w", err)
			}
			fmt.Printf("a2a daemon starting in background (pid %d)\n", child.Process.Pid)
			return nil
		}

		cfgPath := filepath.Join(DefaultConfigDir(), "config.yaml")
		cfg, err := config.Load(cfgPath)
		if err != nil {
			return fmt.Errorf("config: %w", err)
		}

		dataDir := cfg.Stream.DataDir
		if err := ensureDataDir(dataDir); err != nil {
			return err
		}

		d, err := daemon.New(cfg, dataDir, daemon.WithVerboseProviderErrors(startVerbose))
		if err != nil {
			return err
		}
		defer d.Stop()

		ctx, cancel := context.WithCancel(context.Background())
		defer cancel()

		if err := d.Start(ctx); err != nil {
			return err
		}

		urlPath := DaemonURLPath()
		if err := os.MkdirAll(filepath.Dir(urlPath), 0755); err != nil {
			return fmt.Errorf("creating config dir: %w", err)
		}
		if err := os.WriteFile(urlPath, []byte(d.NATSUrl()), 0600); err != nil {
			return fmt.Errorf("writing daemon URL: %w", err)
		}
		defer os.Remove(urlPath)
		pidPath := DaemonPIDPath()
		if err := os.WriteFile(pidPath, []byte(fmt.Sprintf("%d", os.Getpid())), 0600); err != nil {
			return fmt.Errorf("writing daemon PID: %w", err)
		}
		defer os.Remove(pidPath)

		fmt.Printf("a2a daemon running (%s)\n", d.NATSUrl())
		fmt.Printf("agents: %d configured\n", len(cfg.Agents))

		sigCh := make(chan os.Signal, 1)
		signal.Notify(sigCh, syscall.SIGINT, syscall.SIGTERM)
		<-sigCh
		fmt.Println("\nshutting down...")
		return nil
	},
}

func waitForLockHandoff(reader *os.File, timeout time.Duration) error {
	if err := reader.SetReadDeadline(time.Now().Add(timeout)); err != nil {
		return err
	}
	var signal [1]byte
	if _, err := reader.Read(signal[:]); err != nil {
		return err
	}
	if signal[0] != 1 {
		return fmt.Errorf("unexpected handoff signal %d", signal[0])
	}
	return nil
}

func ensureDataDir(dataDir string) error {
	if err := os.MkdirAll(dataDir, 0700); err != nil {
		return fmt.Errorf("creating data dir: %w", err)
	}
	if err := os.Chmod(dataDir, 0700); err != nil {
		log.Printf("warning: could not tighten %s to 0700: %v", dataDir, err)
	}
	return nil
}

var stopCmd = &cobra.Command{
	Use:   "stop",
	Short: "Stop the a2a daemon",
	Long: "Sends SIGTERM to the daemon process recorded in daemon.pid. Only meaningful\n" +
		"after `a2a start --daemon`; a foreground daemon is stopped with Ctrl-C instead.",
	Example: "  a2a stop",
	RunE: func(cmd *cobra.Command, args []string) error {
		pidData, err := os.ReadFile(DaemonPIDPath())
		if err != nil {
			return fmt.Errorf("daemon not running")
		}
		var pid int
		if _, err := fmt.Sscanf(string(pidData), "%d", &pid); err != nil {
			return fmt.Errorf("parsing daemon pid: %w", err)
		}
		proc, err := os.FindProcess(pid)
		if err != nil {
			return err
		}
		if err := proc.Signal(syscall.SIGTERM); err != nil {
			return err
		}
		fmt.Printf("sent SIGTERM to daemon pid %d\n", pid)
		return nil
	},
}

func init() {
	startCmd.Flags().BoolVar(&startBackground, "daemon", false, "run in background")
	startCmd.Flags().BoolVarP(
		&startVerbose, "verbose", "v", false,
		"include bounded, redacted provider response bodies",
	)
	rootCmd.AddCommand(startCmd)
	rootCmd.AddCommand(stopCmd)
}
