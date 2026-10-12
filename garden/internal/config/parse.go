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
	if err := validateSuppliedContextWindow(data); err != nil {
		return nil, err
	}
	applyDefaults(&cfg)
	expandEnvVars(&cfg)
	expandHome(&cfg)
	if err := validate(&cfg); err != nil {
		return nil, err
	}
	return &cfg, nil
}

// validateSuppliedContextWindow rejects an explicit out-of-range context window.
// The decoded int cannot tell an explicit zero from an omission, so it re-reads
// the raw YAML for presence.
func validateSuppliedContextWindow(data []byte) error {
	var supplied struct {
		Defaults struct {
			ContextWindow *int `yaml:"context_window"`
		} `yaml:"defaults"`
	}
	if err := yaml.Unmarshal(data, &supplied); err != nil {
		return fmt.Errorf("parsing config: %w", err)
	}
	if supplied.Defaults.ContextWindow == nil {
		return nil
	}
	return validateContextWindow(*supplied.Defaults.ContextWindow)
}
