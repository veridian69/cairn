package cmd

import (
	"fmt"
	"path/filepath"
	"text/tabwriter"

	"github.com/spf13/cobra"
	"github.com/veridian69/cairn/a2a/internal/config"
)

var statusCmd = &cobra.Command{
	Use:   "status",
	Short: "Show daemon and agent status",
	Long: "Reports whether the daemon is running and, for each configured agent: state,\n" +
		"responsiveness, inbox queue depth, last seen stream sequence, hourly message\n" +
		"count, provider, and model. Falls back to `a2a agent list` if no daemon is running.",
	Example: "  a2a status",
	RunE: func(cmd *cobra.Command, args []string) error {
		cfg, err := config.Load(filepath.Join(DefaultConfigDir(), "config.yaml"))
		if err != nil {
			return err
		}

		url, err := ReadDaemonURL()
		if err != nil {
			fmt.Println("daemon: stopped")
			return agentListCmd.RunE(cmd, args)
		}

		statuses, err := queryStatuses(url)
		if err != nil {
			return err
		}

		fmt.Println("daemon: running")
		w := tabwriter.NewWriter(cmd.OutOrStdout(), 0, 0, 2, ' ', 0)
		fmt.Fprintln(w, "NAME\tSTATE\tRESP\tQUEUE\tLASTSEQ\tHOUR\tPROVIDER\tMODEL")
		statusByName := make(map[string]bool, len(statuses))
		for _, st := range statuses {
			state := st.State
			if state == "" {
				state = "paused"
				if st.Active {
					state = "active"
				}
			}
			fmt.Fprintf(w, "%s\t%s\t%.2f\t%d\t%d\t%d\t%s\t%s\n",
				st.Name, state, st.Responsiveness, st.QueueDepth, st.LastSeenSeq, st.HourlyCount, st.Provider, st.Model)
			statusByName[st.Name] = true
		}
		for name, ac := range cfg.Agents {
			if statusByName[name] {
				continue
			}
			fmt.Fprintf(w, "%s\toffline\t%.2f\t0\t0\t0\t%s\t%s\n",
				name, ac.EffectiveResponsiveness(cfg.DefaultResponsiveness()), ac.Provider, ac.Model)
		}
		w.Flush()
		return nil
	},
}

func init() {
	rootCmd.AddCommand(statusCmd)
}
