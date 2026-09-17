package config

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

func TestLoadConfig(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "config.yaml")

	yaml := `
agents:
  claude:
    provider: anthropic
    model: claude-opus-4-6
    api_key: $TEST_A2A_KEY
    temperature: 0.9
    responsiveness: 0.7
    system: "You are Claude."
defaults:
  context_window: 50
  responsiveness: 0.5
limits:
  per_agent_per_hour: 20
stream:
  data_dir: /tmp/a2a-test
`
	os.WriteFile(path, []byte(yaml), 0644)
	os.Setenv("TEST_A2A_KEY", "sk-secret-123")
	defer os.Unsetenv("TEST_A2A_KEY")

	cfg, err := Load(path)
	if err != nil {
		t.Fatalf("Load: %v", err)
	}

	claude := cfg.Agents["claude"]
	if claude.APIKey != "$TEST_A2A_KEY" {
		t.Errorf("APIKey = %q, want raw env ref", claude.APIKey)
	}
	if claude.ResolvedAPIKey != "sk-secret-123" {
		t.Errorf("ResolvedAPIKey = %q, want env-expanded value", claude.ResolvedAPIKey)
	}
	if cfg.Defaults.ContextWindow != 50 {
		t.Errorf("ContextWindow = %d, want 50", cfg.Defaults.ContextWindow)
	}
}

func TestLoadConfigDefaults(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "config.yaml")
	os.WriteFile(path, []byte("agents:\n  test:\n    provider: anthropic\n    model: x\n    api_key: k\n"), 0644)

	cfg, err := Load(path)
	if err != nil {
		t.Fatalf("Load: %v", err)
	}
	if cfg.Defaults.ContextWindow != 50 {
		t.Errorf("default context_window = %d, want 50", cfg.Defaults.ContextWindow)
	}
	if cfg.DefaultResponsiveness() != 0.5 {
		t.Errorf("default responsiveness = %f, want 0.5", cfg.DefaultResponsiveness())
	}
}

func TestLoadConfigPreservesExplicitZeroValues(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "config.yaml")
	os.WriteFile(path, []byte(`
agents:
  test:
    provider: anthropic
    model: x
    api_key: k
    temperature: 0
    responsiveness: 0
defaults:
  responsiveness: 0.5
`), 0644)

	cfg, err := Load(path)
	if err != nil {
		t.Fatalf("Load: %v", err)
	}

	agent := cfg.Agents["test"]
	if agent.EffectiveTemperature() != 0 {
		t.Fatalf("temperature = %f, want 0", agent.EffectiveTemperature())
	}
	if agent.EffectiveResponsiveness(cfg.DefaultResponsiveness()) != 0 {
		t.Fatalf("responsiveness = %f, want 0", agent.EffectiveResponsiveness(cfg.DefaultResponsiveness()))
	}
}

func TestLoadRawDoesNotInjectDefaults(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "config.yaml")
	original := `agents:
  test:
    provider: anthropic
    model: x
    api_key: $MY_KEY
`
	os.WriteFile(path, []byte(original), 0644)

	cfg, err := LoadRaw(path)
	if err != nil {
		t.Fatalf("LoadRaw: %v", err)
	}

	// Agent system prompt should NOT be injected
	if cfg.Agents["test"].System != "" {
		t.Errorf("LoadRaw injected system prompt: %q", cfg.Agents["test"].System)
	}
	// Defaults should NOT be populated
	if cfg.Defaults.ContextWindow != 0 {
		t.Errorf("LoadRaw injected context_window: %d", cfg.Defaults.ContextWindow)
	}

	// Round-trip: save should not bloat the file with injected defaults
	if err := Save(path, cfg); err != nil {
		t.Fatalf("Save: %v", err)
	}
	data, _ := os.ReadFile(path)
	if strings.Contains(string(data), DefaultSystem) {
		t.Errorf("saved config should not contain injected default system prompt")
	}
	if strings.Contains(string(data), "context_window: 50") {
		t.Errorf("saved config should not contain injected default context_window")
	}
	if strings.Contains(string(data), "per_agent_per_hour: 20") {
		t.Errorf("saved config should not contain injected default per_agent_per_hour")
	}
}

func TestSaveDropsLegacyDailyBudgetSetting(t *testing.T) {
	path := filepath.Join(t.TempDir(), "config.yaml")
	if err := os.WriteFile(path, []byte(`
limits:
  per_agent_per_hour: 20
  daily_budget_usd: 5
`), 0600); err != nil {
		t.Fatal(err)
	}

	cfg, err := LoadRaw(path)
	if err != nil {
		t.Fatal(err)
	}
	if err := Save(path, cfg); err != nil {
		t.Fatal(err)
	}
	data, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	if strings.Contains(string(data), "daily_budget_usd") {
		t.Fatalf("retired budget setting survived save:\n%s", data)
	}
	if !strings.Contains(string(data), "per_agent_per_hour: 20") {
		t.Fatalf("active rate limit was lost:\n%s", data)
	}
}

func TestSavePreservesEnvReferencesAndPermissions(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "config.yaml")
	cfg := &Config{
		Agents: map[string]AgentConfig{
			"claude": {
				Provider:       "anthropic",
				Model:          "claude-opus-4-6",
				APIKey:         "$ANTHROPIC_API_KEY",
				ResolvedAPIKey: "sk-secret",
			},
		},
		Defaults: Defaults{ContextWindow: 50, Responsiveness: ptr(0.5)},
	}

	if err := Save(path, cfg); err != nil {
		t.Fatalf("Save: %v", err)
	}

	data, err := os.ReadFile(path)
	if err != nil {
		t.Fatalf("ReadFile: %v", err)
	}
	if string(data) == "" || !strings.Contains(string(data), "$ANTHROPIC_API_KEY") {
		t.Fatalf("saved config should preserve env ref, got %q", string(data))
	}
	if strings.Contains(string(data), "sk-secret") {
		t.Fatalf("saved config should not contain resolved secret, got %q", string(data))
	}

	info, err := os.Stat(path)
	if err != nil {
		t.Fatalf("Stat: %v", err)
	}
	if info.Mode().Perm() != 0600 {
		t.Fatalf("mode = %o, want 600", info.Mode().Perm())
	}
}

func TestEnvVarNameParsesSupportedFormats(t *testing.T) {
	tests := []struct {
		raw    string
		want   string
		wantOK bool
	}{
		{raw: "$ANTHROPIC_API_KEY", want: "ANTHROPIC_API_KEY", wantOK: true},
		{raw: "${OPENAI_API_KEY}", want: "OPENAI_API_KEY", wantOK: true},
		{raw: "plain-text", want: "", wantOK: false},
		{raw: "$", want: "", wantOK: false},
	}

	for _, tt := range tests {
		got, ok := envVarName(tt.raw)
		if got != tt.want || ok != tt.wantOK {
			t.Fatalf("envVarName(%q) = (%q, %v), want (%q, %v)", tt.raw, got, ok, tt.want, tt.wantOK)
		}
	}
}

func TestStreamMaxAgeDuration(t *testing.T) {
	tests := []struct {
		value   string
		want    time.Duration
		wantErr bool
	}{
		{value: "", want: 0},
		{value: "0", want: 0},
		{value: "90m", want: 90 * time.Minute},
		{value: "definitely-not-a-duration", wantErr: true},
	}

	for _, tt := range tests {
		got, err := StreamConfig{MaxAge: tt.value}.MaxAgeDuration()
		if tt.wantErr {
			if err == nil {
				t.Fatalf("MaxAgeDuration(%q) should fail", tt.value)
			}
			continue
		}
		if err != nil {
			t.Fatalf("MaxAgeDuration(%q): %v", tt.value, err)
		}
		if got != tt.want {
			t.Fatalf("MaxAgeDuration(%q) = %v, want %v", tt.value, got, tt.want)
		}
	}
}

func TestMemoryDefaultsAndExplicitDisable(t *testing.T) {
	path := filepath.Join(t.TempDir(), "config.yaml")
	if err := os.WriteFile(path, []byte("agents: {}\n"), 0600); err != nil {
		t.Fatal(err)
	}
	cfg, err := Load(path)
	if err != nil {
		t.Fatal(err)
	}
	if !cfg.Memory.EffectiveEnabled() {
		t.Fatal("memory should default to enabled")
	}
	if cfg.Memory.EffectiveRecallLimit() != 3 ||
		cfg.Memory.EffectiveMaxPinnedInjected() != 5 ||
		cfg.Memory.EffectiveMaxNominationsPerHour() != 5 ||
		cfg.Memory.EffectiveMaxItemBytes() != 4096 ||
		cfg.Memory.EffectiveMaxBlockBytes() != 8192 ||
		cfg.Memory.EffectiveQueryBytes() != 1024 {
		t.Fatalf("unexpected memory defaults: %+v", cfg.Memory)
	}
	if !cfg.Defaults.EffectiveControlContract() {
		t.Fatal("control contract should default to enabled")
	}

	if err := os.WriteFile(path, []byte("agents: {}\nmemory:\n  enabled: false\ndefaults:\n  control_contract: false\n"), 0600); err != nil {
		t.Fatal(err)
	}
	cfg, err = Load(path)
	if err != nil {
		t.Fatal(err)
	}
	if cfg.Memory.EffectiveEnabled() || cfg.Defaults.EffectiveControlContract() {
		t.Fatal("explicit false values were overwritten")
	}
}

func TestMemoryValidationAndZeroLimitSemantics(t *testing.T) {
	tests := []struct {
		name string
		yaml string
	}{
		{"item not below block", "memory:\n  max_item_bytes: 8192\n  max_block_bytes: 8192\n"},
		{"zero item bytes", "memory:\n  max_item_bytes: 0\n"},
		{"zero query bytes", "memory:\n  query_bytes: 0\n"},
		{"negative recall limit", "memory:\n  recall_limit: -1\n"},
		{"query timeout above timeout", "memory:\n  embedding:\n    provider: openai\n    model: m\n    timeout: 1s\n    query_timeout: 2s\n"},
		{"zero embedding timeout", "memory:\n  embedding:\n    provider: openai\n    model: m\n    timeout: 0s\n"},
	}
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			path := filepath.Join(t.TempDir(), "config.yaml")
			if err := os.WriteFile(path, []byte("agents: {}\n"+test.yaml), 0600); err != nil {
				t.Fatal(err)
			}
			if _, err := Load(path); err == nil {
				t.Fatal("Load should reject invalid memory configuration")
			}
		})
	}

	path := filepath.Join(t.TempDir(), "config.yaml")
	if err := os.WriteFile(path, []byte(`agents: {}
memory:
  recall_limit: 0
  max_pinned_injected: 0
  max_nominations_per_hour: 0
`), 0600); err != nil {
		t.Fatal(err)
	}
	cfg, err := Load(path)
	if err != nil {
		t.Fatal(err)
	}
	if cfg.Memory.EffectiveRecallLimit() != 0 ||
		cfg.Memory.EffectiveMaxPinnedInjected() != 0 ||
		cfg.Memory.EffectiveMaxNominationsPerHour() != 0 {
		t.Fatalf("explicit zero limits were overwritten: %+v", cfg.Memory)
	}
}

func TestEmbeddingAPIKeyResolutionDoesNotMutateRawConfig(t *testing.T) {
	t.Setenv("TEST_EMBED_KEY", "sk-embed")
	path := filepath.Join(t.TempDir(), "config.yaml")
	if err := os.WriteFile(path, []byte(`agents: {}
memory:
  embedding:
    provider: openai
    model: text-embedding-3-small
    api_key: $TEST_EMBED_KEY
`), 0600); err != nil {
		t.Fatal(err)
	}
	resolved, err := Load(path)
	if err != nil {
		t.Fatal(err)
	}
	if got := resolved.Memory.Embedding.EffectiveAPIKey(); got != "sk-embed" {
		t.Fatalf("resolved embedding API key = %q", got)
	}
	raw, err := LoadRaw(path)
	if err != nil {
		t.Fatal(err)
	}
	if got := raw.Memory.Embedding.APIKey; got != "$TEST_EMBED_KEY" {
		t.Fatalf("LoadRaw mutated embedding API key: %q", got)
	}
}
