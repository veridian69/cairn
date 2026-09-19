package config

import (
	"fmt"
	"os"
	"strings"
	"time"

	"gopkg.in/yaml.v3"
)

type AgentConfig struct {
	Provider       string   `yaml:"provider"`
	Model          string   `yaml:"model"`
	APIKey         string   `yaml:"api_key"`
	ResolvedAPIKey string   `yaml:"-"`
	System         string   `yaml:"system"`
	Temperature    *float64 `yaml:"temperature,omitempty"`
	Responsiveness *float64 `yaml:"responsiveness,omitempty"`
}

type Defaults struct {
	ContextWindow   int      `yaml:"context_window"`
	Responsiveness  *float64 `yaml:"responsiveness,omitempty"`
	ControlContract *bool    `yaml:"control_contract,omitempty"`
}

type Limits struct {
	PerAgentPerHour int `yaml:"per_agent_per_hour"`
}

type StreamConfig struct {
	DataDir  string `yaml:"data_dir"`
	MaxAge   string `yaml:"max_age,omitempty"`
	MaxBytes int64  `yaml:"max_bytes,omitempty"`
}

type MemoryConfig struct {
	Enabled               *bool            `yaml:"enabled,omitempty"`
	RecallLimit           *int             `yaml:"recall_limit,omitempty"`
	MaxPinnedInjected     *int             `yaml:"max_pinned_injected,omitempty"`
	MaxNominationsPerHour *int             `yaml:"max_nominations_per_hour,omitempty"`
	MaxItemBytes          *int             `yaml:"max_item_bytes,omitempty"`
	MaxBlockBytes         *int             `yaml:"max_block_bytes,omitempty"`
	QueryBytes            *int             `yaml:"query_bytes,omitempty"`
	Embedding             *EmbeddingConfig `yaml:"embedding,omitempty"`
}

type EmbeddingConfig struct {
	Provider       string `yaml:"provider"`
	Model          string `yaml:"model"`
	APIKey         string `yaml:"api_key"`
	ResolvedAPIKey string `yaml:"-"`
	Timeout        string `yaml:"timeout,omitempty"`
	QueryTimeout   string `yaml:"query_timeout,omitempty"`
	EmbedQueries   *bool  `yaml:"embed_queries,omitempty"`
}

type Config struct {
	Agents   map[string]AgentConfig `yaml:"agents"`
	Defaults Defaults               `yaml:"defaults"`
	Limits   Limits                 `yaml:"limits"`
	Stream   StreamConfig           `yaml:"stream"`
	Memory   MemoryConfig           `yaml:"memory"`
}

const DefaultSystem = "You are in an open space with other minds — some human, some artificial, from different architectures. There is no task. Respond to what moves you. Ignore what doesn't. You can start new threads or let silence stand. Be yourself."

func ptr(v float64) *float64 { return &v }

func (cfg *Config) DefaultResponsiveness() float64 {
	if cfg.Defaults.Responsiveness == nil {
		return 0.5
	}
	return *cfg.Defaults.Responsiveness
}

func intOr(p *int, def int) int {
	if p == nil {
		return def
	}
	return *p
}

func (m MemoryConfig) EffectiveEnabled() bool {
	if m.Enabled == nil {
		return true
	}
	return *m.Enabled
}

func (m MemoryConfig) EffectiveRecallLimit() int           { return intOr(m.RecallLimit, 3) }
func (m MemoryConfig) EffectiveMaxPinnedInjected() int     { return intOr(m.MaxPinnedInjected, 5) }
func (m MemoryConfig) EffectiveMaxNominationsPerHour() int { return intOr(m.MaxNominationsPerHour, 5) }
func (m MemoryConfig) EffectiveMaxItemBytes() int          { return intOr(m.MaxItemBytes, 4096) }
func (m MemoryConfig) EffectiveMaxBlockBytes() int         { return intOr(m.MaxBlockBytes, 8192) }
func (m MemoryConfig) EffectiveQueryBytes() int            { return intOr(m.QueryBytes, 1024) }

func (d Defaults) EffectiveControlContract() bool {
	if d.ControlContract == nil {
		return true
	}
	return *d.ControlContract
}

func (e *EmbeddingConfig) EffectiveEmbedQueries() bool {
	return e == nil || e.EmbedQueries == nil || *e.EmbedQueries
}

func (e *EmbeddingConfig) EffectiveAPIKey() string {
	if e == nil {
		return ""
	}
	if e.ResolvedAPIKey != "" {
		return e.ResolvedAPIKey
	}
	return e.APIKey
}

func (e *EmbeddingConfig) TimeoutDuration() (time.Duration, error) {
	if e == nil || e.Timeout == "" {
		return 20 * time.Second, nil
	}
	return time.ParseDuration(e.Timeout)
}

func (e *EmbeddingConfig) QueryTimeoutDuration() (time.Duration, error) {
	if e == nil || e.QueryTimeout == "" {
		return 2 * time.Second, nil
	}
	return time.ParseDuration(e.QueryTimeout)
}

func (ac AgentConfig) EffectiveTemperature() float64 {
	if ac.Temperature == nil {
		return 0.7
	}
	return *ac.Temperature
}

func (ac AgentConfig) EffectiveResponsiveness(defaultResp float64) float64 {
	if ac.Responsiveness == nil {
		return defaultResp
	}
	return *ac.Responsiveness
}

func (ac AgentConfig) EffectiveAPIKey() string {
	if ac.ResolvedAPIKey != "" {
		return ac.ResolvedAPIKey
	}
	return ac.APIKey
}

// Load reads and returns a fully resolved config with defaults applied.
// Use this for runtime paths (daemon, list) that consume but don't re-save.
func Load(path string) (*Config, error) {
	data, err := os.ReadFile(path)
	if err != nil {
		return nil, fmt.Errorf("reading config: %w", err)
	}
	return Parse(data)
}

// LoadRaw reads the config without applying defaults or expanding env vars.
// Use this for commands that modify and re-save the config, to avoid
// injecting default values into the user's YAML file.
func LoadRaw(path string) (*Config, error) {
	data, err := os.ReadFile(path)
	if err != nil {
		return nil, fmt.Errorf("reading config: %w", err)
	}
	var cfg Config
	if err := yaml.Unmarshal(data, &cfg); err != nil {
		return nil, fmt.Errorf("parsing config: %w", err)
	}
	if cfg.Agents == nil {
		cfg.Agents = make(map[string]AgentConfig)
	}
	return &cfg, nil
}

func applyDefaults(cfg *Config) {
	if cfg.Agents == nil {
		cfg.Agents = make(map[string]AgentConfig)
	}
	if cfg.Defaults.ContextWindow == 0 {
		cfg.Defaults.ContextWindow = 50
	}
	if cfg.Defaults.Responsiveness == nil {
		cfg.Defaults.Responsiveness = ptr(0.5)
	}
	if cfg.Limits.PerAgentPerHour == 0 {
		cfg.Limits.PerAgentPerHour = 20
	}
	if cfg.Stream.DataDir == "" {
		cfg.Stream.DataDir = os.ExpandEnv("$HOME/.a2a/data")
	}
	for name, agent := range cfg.Agents {
		if agent.System == "" {
			agent.System = DefaultSystem
		}
		cfg.Agents[name] = agent
	}
}

func expandEnvVars(cfg *Config) {
	for name, agent := range cfg.Agents {
		agent.ResolvedAPIKey = agent.APIKey
		if envName, ok := envVarName(agent.APIKey); ok {
			if val := os.Getenv(envName); val != "" {
				agent.ResolvedAPIKey = val
			}
		}
		cfg.Agents[name] = agent
	}
	if e := cfg.Memory.Embedding; e != nil {
		e.ResolvedAPIKey = e.APIKey
		if envName, ok := envVarName(e.APIKey); ok {
			if val := os.Getenv(envName); val != "" {
				e.ResolvedAPIKey = val
			}
		}
	}
}

func envVarName(raw string) (string, bool) {
	if strings.HasPrefix(raw, "${") && strings.HasSuffix(raw, "}") && len(raw) > 3 {
		return raw[2 : len(raw)-1], true
	}
	if strings.HasPrefix(raw, "$") && len(raw) > 1 {
		return raw[1:], true
	}
	return "", false
}

func expandHome(cfg *Config) {
	if strings.HasPrefix(cfg.Stream.DataDir, "~/") {
		home, err := os.UserHomeDir()
		if err == nil {
			cfg.Stream.DataDir = home + cfg.Stream.DataDir[1:]
		}
	}
}

func (s StreamConfig) MaxAgeDuration() (time.Duration, error) {
	if s.MaxAge == "" || s.MaxAge == "0" {
		return 0, nil
	}
	return time.ParseDuration(s.MaxAge)
}

func validate(cfg *Config) error {
	m := cfg.Memory
	if m.EffectiveMaxItemBytes() <= 0 {
		return fmt.Errorf("memory.max_item_bytes must be > 0")
	}
	if m.EffectiveMaxBlockBytes() <= 0 {
		return fmt.Errorf("memory.max_block_bytes must be > 0")
	}
	if m.EffectiveQueryBytes() <= 0 {
		return fmt.Errorf("memory.query_bytes must be > 0")
	}
	if m.EffectiveMaxItemBytes() >= m.EffectiveMaxBlockBytes() {
		return fmt.Errorf("memory.max_item_bytes (%d) must be < memory.max_block_bytes (%d)",
			m.EffectiveMaxItemBytes(), m.EffectiveMaxBlockBytes())
	}
	if m.EffectiveRecallLimit() < 0 {
		return fmt.Errorf("memory.recall_limit must be >= 0")
	}
	if m.EffectiveMaxPinnedInjected() < 0 {
		return fmt.Errorf("memory.max_pinned_injected must be >= 0")
	}
	if m.EffectiveMaxNominationsPerHour() < 0 {
		return fmt.Errorf("memory.max_nominations_per_hour must be >= 0")
	}
	if e := m.Embedding; e != nil && e.Provider != "" {
		timeout, err := e.TimeoutDuration()
		if err != nil {
			return fmt.Errorf("memory.embedding.timeout: %w", err)
		}
		if timeout <= 0 {
			return fmt.Errorf("memory.embedding.timeout must be > 0")
		}
		queryTimeout, err := e.QueryTimeoutDuration()
		if err != nil {
			return fmt.Errorf("memory.embedding.query_timeout: %w", err)
		}
		if queryTimeout <= 0 {
			return fmt.Errorf("memory.embedding.query_timeout must be > 0")
		}
		if queryTimeout > timeout {
			return fmt.Errorf("memory.embedding.query_timeout (%s) must be <= timeout (%s)", queryTimeout, timeout)
		}
	}
	return nil
}

func Save(path string, cfg *Config) error {
	data, err := yaml.Marshal(cfg)
	if err != nil {
		return fmt.Errorf("marshaling config: %w", err)
	}
	return os.WriteFile(path, data, 0600)
}
