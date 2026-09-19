package cmd

import (
	"os"
	"os/signal"
	"syscall"

	"github.com/spf13/cobra"
	"github.com/veridian69/cairn/a2a/internal/garden"
)

func newHostCommand() *cobra.Command {
	var path string
	command := &cobra.Command{Use: "host --config FILE", Short: "Run a complete scoped Garden in the foreground", Args: cobra.NoArgs, PersistentPreRunE: func(*cobra.Command, []string) error { return nil }}
	command.Flags().StringVar(&path, "config", "", "strict Garden host JSON configuration")
	_ = command.MarkFlagRequired("config")
	command.RunE = func(cmd *cobra.Command, _ []string) error {
		cfg, err := garden.LoadHostConfig(path)
		if err != nil {
			return err
		}
		ctx, stop := signal.NotifyContext(cmd.Context(), os.Interrupt, syscall.SIGTERM)
		defer stop()
		return garden.RunHost(ctx, cfg)
	}
	return command
}
func init() { rootCmd.AddCommand(newHostCommand()) }
