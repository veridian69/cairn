package cmd

import (
	"context"
	"errors"
	"fmt"
	"os"
	"os/signal"
	"path/filepath"
	"syscall"

	"github.com/spf13/cobra"
	"github.com/veridian69/cairn/a2a/internal/config"
	"github.com/veridian69/cairn/a2a/internal/mcpserver"
)

var mcpName string

var mcpCmd = &cobra.Command{
	Use:   "mcp",
	Short: "Serve the stream to external agents over MCP (stdio)",
	Long: "Runs an MCP server on stdio so external coding agents (Claude Code, Codex)\n" +
		"can join the conversation. Each client spawns its own `a2a mcp` process and\n" +
		"speaks as the participant named by --name. The daemon must be running for\n" +
		"tool calls to succeed, but this command starts without it.\n\n" +
		"--name must not match a configured daemon agent (the command refuses to\n" +
		"start if it does), and must not be an existing human participant's name:\n" +
		"participants are get-or-create by name, so reusing one is rejected on the\n" +
		"first tool call because the kinds disagree.",
	Example: "  claude mcp add a2a -- a2a mcp --name claude\n" +
		"  a2a mcp --name codex",
	Args: cobra.NoArgs,
	RunE: func(cmd *cobra.Command, args []string) error {
		if err := validateMCPName(mcpName); err != nil {
			return err
		}

		srv := mcpserver.New(mcpserver.Options{
			Name:      mcpName,
			Version:   version,
			DaemonURL: ReadDaemonURL,
			DataDir:   ResolveDataDir,
		})
		defer srv.Close()

		ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
		defer stop()

		// Run returns only once in-flight handlers finish, and the stdio
		// transport does not cancel their contexts — so a blocking
		// wait_for_messages must be released explicitly, or Ctrl-C would hang
		// for up to its timeout while holding the runtime lease.
		ran := make(chan struct{})
		go func() {
			select {
			case <-ctx.Done():
				srv.Shutdown()
			case <-ran:
			}
		}()
		err := srv.Run(ctx)
		close(ran)
		if errors.Is(err, context.Canceled) {
			return nil // normal Ctrl-C shutdown, not a failure exit
		}
		return err
	},
}

// validateMCPName refuses names that collide with configured daemon agents:
// RegisterParticipant is get-or-create by name, so a collision would silently
// share the agent's participant ID and make the daemon agent skip the MCP
// client's messages as its own.
func validateMCPName(name string) error {
	if name == "" {
		return errors.New("--name is required (e.g. --name claude)")
	}
	cfg, err := config.Load(filepath.Join(DefaultConfigDir(), "config.yaml"))
	if err != nil {
		if errors.Is(err, os.ErrNotExist) {
			return nil
		}
		return err
	}
	if _, exists := cfg.Agents[name]; exists {
		return fmt.Errorf("--name %q matches a configured daemon agent; pick a different name", name)
	}
	return nil
}

func init() {
	mcpCmd.Flags().StringVar(&mcpName, "name", "", "participant identity for this MCP session (required; must not be a configured agent or an existing human participant)")
	rootCmd.AddCommand(mcpCmd)
}
