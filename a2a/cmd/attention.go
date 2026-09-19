package cmd

import (
	"context"
	"encoding/json"
	"errors"
	"io"
	"os"
	"os/signal"
	"syscall"
	"time"

	"github.com/google/uuid"
	"github.com/spf13/cobra"
	"github.com/veridian69/cairn/a2a/internal/attention"
)

func init() { rootCmd.AddCommand(newConnectCommand(), newAttendCommand(), newDoctorCommand()) }

func newDoctorCommand() *cobra.Command {
	var profile string
	var host bool
	command := &cobra.Command{Use: "doctor", Short: "Check Garden authentication and optional host session without sending", Args: cobra.NoArgs, PersistentPreRunE: func(*cobra.Command, []string) error { return nil }}
	command.Flags().StringVar(&profile, "profile", "", "Explicit Garden adapter profile")
	command.Flags().BoolVar(&host, "host", false, "Also read the explicitly selected host session")
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
		report, err := attention.Doctor(ctx, p, host)
		if err != nil {
			return err
		}
		return json.NewEncoder(cmd.OutOrStdout()).Encode(report)
	}
	return command
}

func newConnectCommand() *cobra.Command {
	var profile string
	command := &cobra.Command{Use: "connect", Short: "Expose a remote Garden through local MCP stdio", PersistentPreRunE: func(*cobra.Command, []string) error { return nil }}
	command.Flags().StringVar(&profile, "profile", "", "Explicit Garden adapter profile")
	_ = command.MarkFlagRequired("profile")
	command.RunE = func(cmd *cobra.Command, args []string) error {
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
		server := attention.NewStdio(cmd.OutOrStdout(), remote, p.Adapter == "claude")
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
			client, err := attention.NewCodex(ctx, p.CodexBinary, p.CodexSocket, p.SessionID)
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
			host, err = attention.NewOpenCode(p.HostEndpoint, p.SessionID, p.HostUsername, password)
			if err != nil {
				return err
			}
		}
		runner := attention.Runner{Inbox: remote, Host: host, ConsumerID: uuid.NewString()}
		return runner.Run(ctx)
	}
	return command
}
