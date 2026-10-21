package cmd

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"
	"os/signal"
	"syscall"
	"time"

	"github.com/google/uuid"
	"github.com/spf13/cobra"
	"github.com/veridian69/cairn/garden/internal/attention"
)

func init() { rootCmd.AddCommand(newConnectCommand(), newAttendCommand(), newDoctorCommand()) }

func newDoctorCommand() *cobra.Command {
	var profile string
	var host bool
	var readiness bool
	command := &cobra.Command{Use: "doctor", Short: "Check Garden authentication and optional host session without sending", Args: cobra.NoArgs, PersistentPreRunE: func(*cobra.Command, []string) error { return nil }}
	command.Flags().StringVar(&profile, "profile", "", "Explicit Garden adapter profile")
	command.Flags().BoolVar(&host, "host", false, "Also read the explicitly selected host session")
	command.Flags().BoolVar(&readiness, "readiness", false, "Emit one machine-readable Garden readiness observation without checking a host")
	command.MarkFlagsMutuallyExclusive("host", "readiness")
	_ = command.MarkFlagRequired("profile")
	command.RunE = func(cmd *cobra.Command, _ []string) error {
		p, err := attention.LoadProfile(profile)
		if err != nil {
			return err
		}
		signalCtx, stop := signal.NotifyContext(cmd.Context(), os.Interrupt, syscall.SIGTERM)
		defer stop()
		ctx, cancel := context.WithTimeout(signalCtx, 30*time.Second)
		defer cancel()
		if readiness {
			return json.NewEncoder(cmd.OutOrStdout()).Encode(attention.Readiness(ctx, p))
		}
		report, err := attention.Doctor(ctx, p, host)
		if err != nil {
			return err
		}
		return json.NewEncoder(cmd.OutOrStdout()).Encode(report)
	}
	return command
}

func newConnectCommand() *cobra.Command {
	var profile, harness string
	command := &cobra.Command{Use: "connect", Short: "Expose a remote Garden through local MCP stdio", PersistentPreRunE: func(*cobra.Command, []string) error { return nil }}
	command.Flags().StringVar(&profile, "profile", "", "Explicit Garden adapter profile")
	command.Flags().StringVar(&harness, "harness", "", "Connect as this harness's default identity from the Garden home (claude, opencode, codex)")
	command.MarkFlagsMutuallyExclusive("profile", "harness")
	command.MarkFlagsOneRequired("profile", "harness")
	command.RunE = func(cmd *cobra.Command, args []string) error {
		if harness != "" {
			return connectHarness(cmd, harness)
		}
		p, err := attention.LoadProfile(profile)
		if err != nil {
			return err
		}
		ctx, cancel := signal.NotifyContext(cmd.Context(), os.Interrupt, syscall.SIGTERM)
		defer cancel()
		remote, err := attention.Connect(ctx, p)
		if err != nil {
			return err
		}
		defer remote.Close()
		instructions := attention.ProfileInstructions
		if p.Adapter == "claude" {
			instructions = attention.ProfileChannelInstructions
		}
		server := attention.NewStdio(cmd.OutOrStdout(), remote, p.Adapter == "claude", instructions)
		in, ok := cmd.InOrStdin().(io.ReadCloser)
		if !ok {
			in = io.NopCloser(cmd.InOrStdin())
		}
		if p.Adapter == "claude" {
			runner := attention.Runner{Inbox: remote, Host: server, ConsumerID: uuid.NewString()}
			return server.Run(ctx, in, runner.Run)
		}
		return server.Run(ctx, in, nil)
	}
	return command
}

// connectHarness serves the harness's default (or GARDEN_IDENTITY) Garden identity.
// Connection failures stay up as a status-only server with the reason.
func connectHarness(cmd *cobra.Command, harness string) error {
	if harness != "claude" && harness != "opencode" && harness != "codex" {
		return fmt.Errorf("harness %s is not supported yet", harness)
	}
	ctx, cancel := signal.NotifyContext(cmd.Context(), os.Interrupt, syscall.SIGTERM)
	defer cancel()
	if harness == "codex" {
		return connectCodex(ctx, cmd)
	}
	session := attention.OpenHarness(ctx, harness, os.Getenv("GARDEN_IDENTITY"))
	defer session.Close()
	// Codex has no channel: its messages reach the thread through `codex queue`, not the Stdio server.
	server := attention.NewStdio(cmd.OutOrStdout(), session.Tools, session.Remote != nil && harness != "codex", session.Instructions)
	if harness == "opencode" && session.Remote != nil {
		server.UseOpenCode(session.Remote.SessionReported, session.Remote.DeliveryFailed)
		session.Remote.OnInstructions(server.SetInstructions)
	}
	in, ok := cmd.InOrStdin().(io.ReadCloser)
	if !ok {
		in = io.NopCloser(cmd.InOrStdin())
	}
	if session.Remote != nil {
		host := session.Host
		if host == nil {
			host = server
		}
		runner := attention.Runner{Inbox: session.Remote, Host: host, ConsumerID: session.ConsumerID}
		return server.Run(ctx, in, func(ctx context.Context) error {
			if err := session.Remote.StartDelivery(ctx, server); err != nil {
				return err
			}
			return runner.Run(ctx)
		})
	}
	return server.Run(ctx, in, nil)
}

// connectCodex serves one embedded Codex thread. A sibling core of the same Codex process asks
// for the identity with SIGUSR1, so the handler is registered before the identity is claimed.
func connectCodex(ctx context.Context, cmd *cobra.Command) error {
	yield := make(chan os.Signal, 1)
	signal.Notify(yield, syscall.SIGUSR1)
	defer signal.Stop(yield)
	session := attention.OpenHarness(ctx, "codex", os.Getenv("GARDEN_IDENTITY"))
	defer session.Close()
	core := attention.NewCodexCore(session)
	// Codex has no channel: its messages reach the thread through `codex queue`, not the Stdio server.
	server := attention.NewStdio(cmd.OutOrStdout(), core, false, core.Instructions())
	server.UseCodex(core.Learn)
	go func() {
		for {
			select {
			case <-yield:
				core.Yield()
			case <-ctx.Done():
				return
			}
		}
	}()
	in, ok := cmd.InOrStdin().(io.ReadCloser)
	if !ok {
		in = io.NopCloser(cmd.InOrStdin())
	}
	return server.Run(ctx, in, func(ctx context.Context) error { return core.Run(ctx, server) })
}

func newAttendCommand() *cobra.Command {
	var profile string
	command := &cobra.Command{Use: "attend", Short: "Deliver addressed Garden messages to an existing agent session", PersistentPreRunE: func(*cobra.Command, []string) error { return nil }}
	command.Flags().StringVar(&profile, "profile", "", "Explicit Garden adapter profile")
	_ = command.MarkFlagRequired("profile")
	command.RunE = func(cmd *cobra.Command, args []string) error {
		p, err := attention.LoadProfile(profile)
		if err != nil {
			return err
		}
		if p.Adapter != "codex" && p.Adapter != "opencode" {
			return errors.New("attend requires a codex or opencode profile")
		}
		ctx, cancel := signal.NotifyContext(cmd.Context(), os.Interrupt, syscall.SIGTERM)
		defer cancel()
		remote, err := attention.Connect(ctx, p)
		if err != nil {
			return err
		}
		defer remote.Close()
		var host attention.Host
		if p.Adapter == "codex" {
			client, err := attention.NewCodex(ctx, p.CodexSocket, p.SessionID)
			if err != nil {
				return err
			}
			defer client.Close()
			host = client
		} else {
			password := ""
			if p.HostCredentialFile != "" {
				password, err = attention.ReadCredential(p.HostCredentialFile)
				if err != nil {
					return err
				}
			}
			host, err = attention.NewOpenCode(p.HostEndpoint, p.SessionID, p.HostUsername, password, p.HostAPIVersion)
			if err != nil {
				return err
			}
		}
		runner := attention.Runner{Inbox: remote, Host: host, ConsumerID: uuid.NewString()}
		return runner.Run(ctx)
	}
	return command
}
