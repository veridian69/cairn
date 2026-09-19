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
	applyDefaults(&cfg)
	expandEnvVars(&cfg)
	expandHome(&cfg)
	if err := validate(&cfg); err != nil {
		return nil, err
	}
	return &cfg, nil
}
