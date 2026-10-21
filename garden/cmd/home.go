package cmd

import (
	"fmt"

	"github.com/spf13/cobra"
	"github.com/veridian69/cairn/garden/internal/attention"
)

func init() { rootCmd.AddCommand(newHomeCommand()) }

func newHomeCommand() *cobra.Command {
	home := &cobra.Command{Use: "home", Short: "Manage the Garden harness home", PersistentPreRunE: func(*cobra.Command, []string) error { return nil }}
	home.AddCommand(newHomeImportCommand(), newHomeAddIdentityCommand())
	return home
}

// newHomeAddIdentityCommand installs one token issued on the Garden server into the Garden
// home. It never prints the token; output names the identity, the harness and paths only.
func newHomeAddIdentityCommand() *cobra.Command {
	var tokenFile, harness string
	command := &cobra.Command{Use: "add-identity <name>", Short: "Install one issued identity token into the Garden home", Args: cobra.ExactArgs(1), PersistentPreRunE: func(*cobra.Command, []string) error { return nil }}
	command.Flags().StringVar(&tokenFile, "token-file", "", "Private token file issued on the Garden server (mode 0600, owned by you)")
	command.Flags().StringVar(&harness, "default", "", "Make this identity the default for claude, codex or opencode")
	_ = command.MarkFlagRequired("token-file")
	command.RunE = func(cmd *cobra.Command, args []string) error {
		dir, err := attention.ResolveHomeDir()
		if err != nil {
			return err
		}
		result, err := attention.AddIdentity(dir, args[0], tokenFile, harness)
		if err != nil {
			return err
		}
		out := cmd.OutOrStdout()
		if result.Present {
			fmt.Fprintf(out, "already present: identity %s in %s\n", result.Name, result.Home)
		} else {
			fmt.Fprintf(out, "added identity %s to %s\n", result.Name, result.Home)
		}
		for _, path := range result.Written {
			fmt.Fprintf(out, "  wrote %s\n", path)
		}
		if result.DefaultSet {
			fmt.Fprintf(out, "  default for %s is now %s (garden.json updated)\n", result.Harness, result.Name)
		} else if result.Harness != "" {
			fmt.Fprintf(out, "  default for %s already %s; garden.json unchanged\n", result.Harness, result.Name)
		}
		return nil
	}
	return command
}

// newHomeImportCommand migrates the old per-profile layout into $GARDEN_HOME. It prints
// identity names and paths only; token contents never leave the protected files.
func newHomeImportCommand() *cobra.Command {
	var from string
	var defaults map[string]string
	command := &cobra.Command{Use: "import", Short: "Import an old per-profile Garden layout into the Garden home once", Args: cobra.NoArgs, PersistentPreRunE: func(*cobra.Command, []string) error { return nil }}
	command.Flags().StringVar(&from, "from", "", "Old per-profile directory, for example ~/.config/legacy-garden")
	command.Flags().StringToStringVar(&defaults, "default", nil, "Harness default when two profiles share an adapter, as harness=name (repeatable)")
	_ = command.MarkFlagRequired("from")
	command.RunE = func(cmd *cobra.Command, _ []string) error {
		dir, err := attention.ResolveHomeDir()
		if err != nil {
			return err
		}
		result, err := attention.ImportLegacyHome(from, dir, defaults)
		if err != nil {
			return err
		}
		out := cmd.OutOrStdout()
		if result.Already {
			fmt.Fprintf(out, "already imported: %s\n", result.Home)
		} else {
			fmt.Fprintf(out, "imported into %s\n", result.Home)
			for _, path := range result.Written {
				fmt.Fprintf(out, "  wrote %s\n", path)
			}
		}
		for _, id := range result.Identities {
			if id.Default {
				fmt.Fprintf(out, "  identity %s (default for %s)\n", id.Name, id.Harness)
			} else {
				fmt.Fprintf(out, "  identity %s\n", id.Name)
			}
		}
		for _, name := range result.Ignored {
			fmt.Fprintf(out, "  ignored %s\n", name)
		}
		for _, name := range result.NotMigrated {
			fmt.Fprintf(out, "  not migrated: %s\n", name)
		}
		fmt.Fprintln(out, "the source directory was left unchanged")
		return nil
	}
	return command
}
