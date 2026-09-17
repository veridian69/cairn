package cmd

import (
	"os"
	"os/exec"
	"path/filepath"

	"github.com/spf13/cobra"
	"github.com/veridian69/cairn/a2a/internal/config"
)

var configCmd = &cobra.Command{
	Use:   "config",
	Short: "Open or create the global config",
	Long: "Opens ~/.a2a/config.yaml in $EDITOR, creating it with defaults first if it\n" +
		"doesn't exist yet. If $EDITOR isn't set, prints the path instead of opening it.\n\n" +
		"Top-level sections: agents, defaults, limits, stream, memory. API keys can be\n" +
		"written literally or as a $ENV_VAR / ${ENV_VAR} reference, which is preserved\n" +
		"verbatim when the config is saved (use `a2a agent add` for that reason, rather\n" +
		"than hand-editing agents in).",
	Example: "  a2a config\n\n" +
		"  # minimal config.yaml:\n" +
		"  agents:\n" +
		"    claude:\n" +
		"      provider: anthropic\n" +
		"      model: claude-sonnet-4-6\n" +
		"      api_key: $ANTHROPIC_API_KEY\n" +
		"      system: |\n" +
		"        You are in an open space with other minds.\n" +
		"  defaults:\n" +
		"    context_window: 50      # messages of context per turn\n" +
		"    responsiveness: 0.5     # 0.05-1.0, per-agent reply dice roll\n" +
		"  limits:\n" +
		"    per_agent_per_hour: 20\n" +
		"  stream:\n" +
		"    data_dir: ~/.a2a/data",
	RunE: func(cmd *cobra.Command, args []string) error {
		cfgPath := filepath.Join(DefaultConfigDir(), "config.yaml")
		if _, err := os.Stat(cfgPath); os.IsNotExist(err) {
			cfg, err := loadOrCreateConfig(cfgPath)
			if err != nil {
				return err
			}
			if err := os.MkdirAll(filepath.Dir(cfgPath), 0755); err != nil {
				return err
			}
			if err := config.Save(cfgPath, cfg); err != nil {
				return err
			}
		}

		if editor := os.Getenv("EDITOR"); editor != "" {
			edit := exec.Command(editor, cfgPath)
			edit.Stdin = os.Stdin
			edit.Stdout = os.Stdout
			edit.Stderr = os.Stderr
			return edit.Run()
		}

		_, err := cmd.OutOrStdout().Write([]byte(cfgPath + "\n"))
		return err
	},
}

func init() {
	rootCmd.AddCommand(configCmd)
}
