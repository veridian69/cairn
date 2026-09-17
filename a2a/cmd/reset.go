package cmd

import (
	"fmt"
	"os"

	"github.com/spf13/cobra"
	"github.com/veridian69/cairn/a2a/internal/snapshot"
	"golang.org/x/term"
)

func newResetCmd() *cobra.Command {
	var yes bool
	command := &cobra.Command{
		Use:   "reset",
		Short: "Delete all stopped runtime state",
		Long: "Deletes the complete message stream, operational state, redactions, and\n" +
			"memory. Configuration, logs, and snapshots are preserved. Every A2A\n" +
			"command must be stopped first.",
		Args: cobra.NoArgs,
		RunE: func(cmd *cobra.Command, args []string) error {
			confirmed, err := confirmRuntimeReset(yes)
			if err != nil || !confirmed {
				return err
			}
			manager := snapshot.NewManager(DefaultConfigDir(), version)
			if err := manager.Reset(cmd.Context()); err != nil {
				return err
			}
			fmt.Fprintln(cmd.OutOrStdout(), "runtime reset")
			return nil
		},
	}
	command.Flags().BoolVar(&yes, "yes", false, "skip confirmation")
	return command
}

func confirmRuntimeReset(yes bool) (bool, error) {
	if yes {
		return true, nil
	}
	if !term.IsTerminal(int(os.Stdin.Fd())) {
		return false, fmt.Errorf("refusing to delete all runtime state without --yes (stdin is not a terminal)")
	}
	fmt.Print("delete all messages, state, redactions, and memory? [y/N] ")
	var answer string
	_, _ = fmt.Scanln(&answer)
	if answer != "y" && answer != "Y" {
		fmt.Println("aborted")
		return false, nil
	}
	return true, nil
}

var resetCmd = newResetCmd()

func init() {
	rootCmd.AddCommand(resetCmd)
}
