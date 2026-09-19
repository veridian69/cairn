package cmd

import (
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"text/tabwriter"
	"time"

	"github.com/nats-io/nats.go"
	"github.com/spf13/cobra"
	"github.com/veridian69/cairn/a2a/internal/config"
	daemonpkg "github.com/veridian69/cairn/a2a/internal/daemon"
)

var agentCmd = &cobra.Command{
	Use:   "agent",
	Short: "Manage agents",
	Long: "Add, list, pause, resume, and remove agents defined in config.yaml's `agents`\n" +
		"section. The daemon reads agent config only at startup: `add` and `remove` need\n" +
		"a daemon restart (`a2a stop && a2a start`) to take effect. `pause` and `resume`\n" +
		"act on the live daemon instead and need no restart.",
}

var (
	agentProvider string
	agentModel    string
	agentSystem   string
	agentTemp     float64
	agentResp     float64
	agentAPIKey   string
)

var agentProviderAPIKeyEnv = map[string][]string{
	"anthropic": {"ANTHROPIC_API_KEY"},
	"openai":    {"OPENAI_API_KEY"},
	"google":    {"GOOGLE_API_KEY", "GEMINI_API_KEY"},
	"deepseek":  {"DEEPSEEK_API_KEY"},
}

var agentAddCmd = &cobra.Command{
	Use:   "add [name]",
	Args:  cobra.ExactArgs(1),
	Short: "Register a new agent in config.yaml",
	Long: "Adds an agent under agents.<name> in config.yaml. --provider and --model are\n" +
		"required; --api-key is optional and falls back to the provider's environment\n" +
		"variable or a literal assignment in ~/set*sh. --system, --temperature, and\n" +
		"--responsiveness are optional and fall back to the config's defaults.\n" +
		"Supported providers:\n" +
		"anthropic, openai, google, deepseek. --api-key accepts either a literal key or\n" +
		"a $ENV_VAR / ${ENV_VAR} reference, which is what gets written to the file.\n\n" +
		"Restart the daemon for the new agent to start running.",
	Example: "  a2a agent add claude --provider anthropic --model claude-sonnet-4-6\n\n" +
		"  a2a agent add gpt --provider openai --model gpt-4.1 --api-key '$OPENAI_API_KEY' \\\n" +
		"    --temperature 0.8 --responsiveness 0.3 --system 'Be terse.'",
	RunE: func(cmd *cobra.Command, args []string) error {
		name := args[0]
		cfgPath := filepath.Join(DefaultConfigDir(), "config.yaml")
		cfg, err := loadOrCreateConfig(cfgPath)
		if err != nil {
			return err
		}
		if _, exists := cfg.Agents[name]; exists {
			return fmt.Errorf("agent %q exists", name)
		}
		apiKey, err := resolveAgentAPIKey(agentProvider, agentAPIKey, cmd.Flags().Changed("api-key"))
		if err != nil {
			return err
		}
		ac := config.AgentConfig{
			Provider: agentProvider, Model: agentModel, APIKey: apiKey,
			System: agentSystem,
		}
		if cmd.Flags().Changed("temperature") {
			temp := agentTemp
			ac.Temperature = &temp
		}
		if cmd.Flags().Changed("responsiveness") {
			resp := agentResp
			ac.Responsiveness = &resp
		}
		if ac.System == "" {
			ac.System = config.DefaultSystem
		}
		cfg.Agents[name] = ac
		if err := config.Save(cfgPath, cfg); err != nil {
			return err
		}
		fmt.Printf("agent %q added — restart daemon to activate\n", name)
		return nil
	},
}

var agentListCmd = &cobra.Command{
	Use:   "list",
	Short: "List configured agents and their live state",
	Long: "Shows every agent in config.yaml, plus — when the daemon is running — its live\n" +
		"state, responsiveness, temperature, and provider/model merged in from `a2a status`.",
	RunE: func(cmd *cobra.Command, args []string) error {
		cfgPath := filepath.Join(DefaultConfigDir(), "config.yaml")
		cfg, err := config.Load(cfgPath)
		if err != nil {
			return err
		}
		if len(cfg.Agents) == 0 {
			fmt.Println("no agents")
			return nil
		}
		statusMap := map[string]daemonpkg.AgentStatus{}
		if url, err := ReadDaemonURL(); err == nil {
			if statuses, err := queryStatuses(url); err == nil {
				for _, st := range statuses {
					statusMap[st.Name] = st
				}
			}
		}
		w := tabwriter.NewWriter(os.Stdout, 0, 0, 2, ' ', 0)
		fmt.Fprintln(w, "NAME\tPROVIDER\tMODEL\tRESP\tTEMP\tSTATE")
		for name, ac := range cfg.Agents {
			state := "offline"
			if st, ok := statusMap[name]; ok {
				if st.State != "" {
					state = st.State
				} else if st.Active {
					state = "active"
				} else {
					state = "paused"
				}
			}
			fmt.Fprintf(w, "%s\t%s\t%s\t%.1f\t%.1f\t%s\n",
				name, ac.Provider, ac.Model,
				ac.EffectiveResponsiveness(cfg.DefaultResponsiveness()),
				ac.EffectiveTemperature(),
				state,
			)
		}
		w.Flush()
		return nil
	},
}

var agentRemoveCmd = &cobra.Command{
	Use:     "remove [name]",
	Args:    cobra.ExactArgs(1),
	Short:   "Remove an agent from config.yaml",
	Long:    "Deletes agents.<name> from config.yaml. Takes effect on the next daemon restart.",
	Example: "  a2a agent remove gpt",
	RunE: func(cmd *cobra.Command, args []string) error {
		cfgPath := filepath.Join(DefaultConfigDir(), "config.yaml")
		cfg, err := config.LoadRaw(cfgPath)
		if err != nil {
			return err
		}
		if _, exists := cfg.Agents[args[0]]; !exists {
			return fmt.Errorf("agent %q not found", args[0])
		}
		delete(cfg.Agents, args[0])
		if err := config.Save(cfgPath, cfg); err != nil {
			return err
		}
		fmt.Printf("agent %q removed\n", args[0])
		return nil
	},
}

var agentPauseCmd = &cobra.Command{
	Use:   "pause [name]",
	Args:  cobra.ExactArgs(1),
	Short: "Pause a running agent",
	Long: "Tells the running daemon to stop invoking this agent, without editing\n" +
		"config.yaml or restarting. Resume it later with `a2a agent resume`.",
	Example: "  a2a agent pause claude",
	RunE: func(cmd *cobra.Command, args []string) error {
		url, err := ReadDaemonURL()
		if err != nil {
			return err
		}
		nc, err := nats.Connect(url)
		if err != nil {
			return err
		}
		defer nc.Close()
		nc.Publish("a2a.control", []byte("pause:"+args[0]))
		nc.Flush()
		fmt.Printf("agent %q paused\n", args[0])
		return nil
	},
}

var agentResumeCmd = &cobra.Command{
	Use:     "resume [name]",
	Args:    cobra.ExactArgs(1),
	Short:   "Resume a paused agent",
	Long:    "Resumes an agent previously paused with `a2a agent pause`.",
	Example: "  a2a agent resume claude",
	RunE: func(cmd *cobra.Command, args []string) error {
		url, err := ReadDaemonURL()
		if err != nil {
			return err
		}
		nc, err := nats.Connect(url)
		if err != nil {
			return err
		}
		defer nc.Close()
		nc.Publish("a2a.control", []byte("resume:"+args[0]))
		nc.Flush()
		fmt.Printf("agent %q resumed\n", args[0])
		return nil
	},
}

func init() {
	agentAddCmd.Flags().StringVar(&agentProvider, "provider", "", "provider")
	agentAddCmd.Flags().StringVar(&agentModel, "model", "", "model")
	agentAddCmd.Flags().StringVar(&agentAPIKey, "api-key", "", "API key or $ENV_VAR")
	agentAddCmd.Flags().StringVar(&agentSystem, "system", "", "system prompt")
	agentAddCmd.Flags().Float64Var(&agentTemp, "temperature", 0, "temperature")
	agentAddCmd.Flags().Float64Var(&agentResp, "responsiveness", 0, "responsiveness")
	agentAddCmd.MarkFlagRequired("provider")
	agentAddCmd.MarkFlagRequired("model")

	agentCmd.AddCommand(agentAddCmd, agentListCmd, agentRemoveCmd, agentPauseCmd, agentResumeCmd)
	rootCmd.AddCommand(agentCmd)
}

func loadOrCreateConfig(path string) (*config.Config, error) {
	cfg, err := config.LoadRaw(path)
	if err != nil {
		os.MkdirAll(filepath.Dir(path), 0755)
		cfg = &config.Config{
			Agents: make(map[string]config.AgentConfig),
		}
	}
	return cfg, nil
}

func resolveAgentAPIKey(provider, explicit string, explicitSet bool) (string, error) {
	if explicitSet {
		return explicit, nil
	}

	envNames, ok := agentProviderAPIKeyEnv[provider]
	if !ok {
		return "", fmt.Errorf("no API key discovery configured for provider %q", provider)
	}
	envValues := make(map[string]struct{})
	var setEnvNames []string
	for _, envName := range envNames {
		if value := os.Getenv(envName); value != "" {
			envValues[value] = struct{}{}
			setEnvNames = append(setEnvNames, envName)
		}
	}
	if len(envValues) > 1 {
		return "", fmt.Errorf(
			"distinct API key values found in environment variables %s",
			strings.Join(setEnvNames, ", "),
		)
	}
	if len(setEnvNames) > 0 {
		return "$" + setEnvNames[0], nil
	}
	home, err := os.UserHomeDir()
	if err != nil {
		return "", fmt.Errorf("find home directory for API key discovery: %w", err)
	}
	paths, err := filepath.Glob(filepath.Join(home, "set*sh"))
	if err != nil {
		return "", fmt.Errorf("find API key files: %w", err)
	}
	sort.Strings(paths)

	values := make(map[string]struct{})
	sources := make(map[string]struct{})
	for _, path := range paths {
		contents, err := os.ReadFile(path)
		if err != nil {
			return "", fmt.Errorf("read API key file %q: %w", path, err)
		}
		for _, line := range strings.Split(string(contents), "\n") {
			for _, envName := range envNames {
				value, ok := literalAPIKeyAssignment(line, envName)
				if !ok {
					continue
				}
				values[value] = struct{}{}
				sources[path] = struct{}{}
			}
		}
	}

	if len(values) == 0 {
		return "", fmt.Errorf("no API key found for provider %q", provider)
	}
	if len(values) > 1 {
		paths := make([]string, 0, len(sources))
		for path := range sources {
			paths = append(paths, path)
		}
		sort.Strings(paths)
		return "", fmt.Errorf("distinct API key values found in %s", strings.Join(paths, ", "))
	}
	for value := range values {
		return value, nil
	}
	return "", fmt.Errorf("no API key found for provider %q", provider)
}

func literalAPIKeyAssignment(line, name string) (string, bool) {
	line = strings.TrimSpace(line)
	if strings.HasPrefix(line, "export") {
		rest := strings.TrimLeft(line[len("export"):], " \t")
		if len(rest) == len(line)-len("export") {
			return "", false
		}
		line = rest
	}

	equals := strings.IndexByte(line, '=')
	if equals < 1 || line[:equals] != name {
		return "", false
	}
	value := line[equals+1:]
	if value == "" {
		return "", false
	}

	if value[0] == '\'' || value[0] == '"' {
		quote := value[0]
		if len(value) < 3 || value[len(value)-1] != quote {
			return "", false
		}
		value = value[1 : len(value)-1]
		if strings.ContainsRune(value, rune(quote)) || strings.ContainsAny(value, "\r\n") {
			return "", false
		}
		if quote == '"' && strings.ContainsAny(value, "$`\\") {
			return "", false
		}
		if strings.HasPrefix(value, "$") {
			// Config loading treats any leading '$' as an environment
			// reference. A quoted shell literal must not be silently
			// reinterpreted as a different runtime secret.
			return "", false
		}
		return value, true
	}

	if strings.ContainsAny(value, " \t\r\n$`\\;&|<>(){}[]*?!~\"'") {
		return "", false
	}
	return value, true
}

func queryStatuses(url string) ([]daemonpkg.AgentStatus, error) {
	nc, err := nats.Connect(url)
	if err != nil {
		return nil, err
	}
	defer nc.Close()

	msg, err := nc.Request("a2a.status", nil, 2*time.Second)
	if err != nil {
		return nil, err
	}

	var statuses []daemonpkg.AgentStatus
	if err := json.Unmarshal(msg.Data, &statuses); err != nil {
		return nil, err
	}
	return statuses, nil
}
