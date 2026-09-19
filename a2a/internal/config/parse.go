package config

import (
	"fmt"

	"gopkg.in/yaml.v3"
)

// Parse resolves configuration from an immutable YAML byte slice.
func Parse(data []byte) (*Config, error) {
	var cfg Config
	if err := yaml.Unmarshal(data, &cfg); err != nil {
		return nil, fmt.Errorf("parsing config: %w", err)
	}
	var supplied struct {
		Defaults struct {
			ContextWindow *int `yaml:"context_window"`
		} `yaml:"defaults"`
	}
	if err := yaml.Unmarshal(data, &supplied); err != nil {
		return nil, fmt.Errorf("parsing config: %w", err)
	}
	if supplied.Defaults.ContextWindow != nil {
		if err := validateContextWindow(*supplied.Defaults.ContextWindow); err != nil {
			return nil, err
		}
	}
	applyDefaults(&cfg)
	expandEnvVars(&cfg)
	expandHome(&cfg)
	if err := validate(&cfg); err != nil {
		return nil, err
	}
	return &cfg, nil
}
