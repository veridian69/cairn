package cmd

import (
	"fmt"
	"os"
	"text/tabwriter"

	"github.com/spf13/cobra"
	"github.com/veridian69/cairn/a2a/internal/snapshot"
	"golang.org/x/term"
)

func newSnapshotCmd() *cobra.Command {
	command := &cobra.Command{
		Use:   "snapshot",
		Short: "Manage stopped-only runtime snapshots",
		Long: "Creates, lists, and restores complete A2A runtime snapshots. Snapshot\n" +
			"operations refuse while any other A2A command is running.",
	}
	create := &cobra.Command{
		Use:   "create <name>",
		Short: "Create a complete stopped runtime snapshot",
		Args:  cobra.ExactArgs(1),
		RunE: func(cmd *cobra.Command, args []string) error {
			manager := snapshot.NewManager(DefaultConfigDir(), version)
			created, err := manager.Create(cmd.Context(), args[0])
			if err != nil {
				return err
			}
			fmt.Fprintf(cmd.OutOrStdout(), "snapshot created: %s (%s)\n",
				created.ID, formatBytes(created.Size))
			return nil
		},
	}
	list := &cobra.Command{
		Use:   "list",
		Short: "List runtime snapshots",
		Args:  cobra.NoArgs,
		RunE: func(cmd *cobra.Command, args []string) error {
			manager := snapshot.NewManager(DefaultConfigDir(), version)
			snapshots, err := manager.List(cmd.Context())
			if err != nil {
				return err
			}
			if len(snapshots) == 0 {
				fmt.Fprintln(cmd.OutOrStdout(), "no snapshots")
				return nil
			}
			writer := tabwriter.NewWriter(cmd.OutOrStdout(), 0, 0, 2, ' ', 0)
			fmt.Fprintln(writer, "ID\tCREATED\tSIZE")
			for _, saved := range snapshots {
				fmt.Fprintf(writer, "%s\t%s\t%s\n", saved.ID,
					saved.CreatedAt.Local().Format("2006-01-02 15:04:05"),
					formatBytes(saved.Size))
			}
			return writer.Flush()
		},
	}
	var restoreYes bool
	restore := &cobra.Command{
		Use:   "restore <snapshot-id>",
		Short: "Restore a complete stopped runtime snapshot",
		Args:  cobra.ExactArgs(1),
		RunE: func(cmd *cobra.Command, args []string) error {
			confirmed, err := confirmSnapshotRestore(args[0], restoreYes)
			if err != nil || !confirmed {
				return err
			}
			manager := snapshot.NewManager(DefaultConfigDir(), version)
			result, err := manager.Restore(cmd.Context(), args[0])
			if err != nil {
				return err
			}
			fmt.Fprintf(cmd.OutOrStdout(), "snapshot restored: %s (rollback: %s)\n",
				result.RestoredID, result.RollbackID)
			return nil
		},
	}
	restore.Flags().BoolVar(&restoreYes, "yes", false, "skip confirmation")
	command.AddCommand(create, list, restore)
	return command
}

func confirmSnapshotRestore(id string, yes bool) (bool, error) {
	if yes {
		return true, nil
	}
	if !term.IsTerminal(int(os.Stdin.Fd())) {
		return false, fmt.Errorf("refusing to restore snapshot %s without --yes (stdin is not a terminal)", id)
	}
	fmt.Printf("replace all runtime state with snapshot %s? [y/N] ", id)
	var answer string
	_, _ = fmt.Scanln(&answer)
	if answer != "y" && answer != "Y" {
		fmt.Println("aborted")
		return false, nil
	}
	return true, nil
}

func formatBytes(size int64) string {
	const unit = int64(1024)
	if size < unit {
		return fmt.Sprintf("%d B", size)
	}
	value := float64(size)
	suffixes := []string{"KiB", "MiB", "GiB", "TiB"}
	for _, suffix := range suffixes {
		value /= 1024
		if value < 1024 {
			return fmt.Sprintf("%.1f %s", value, suffix)
		}
	}
	return fmt.Sprintf("%.1f PiB", value/1024)
}

var snapshotCmd = newSnapshotCmd()

func init() {
	rootCmd.AddCommand(snapshotCmd)
}
