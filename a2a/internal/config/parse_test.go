package config

import (
	"bytes"
	"path/filepath"
	"testing"
)

func TestParseResolvesConfigWithoutChangingSourceBytes(t *testing.T) {
	home := t.TempDir()
	t.Setenv("HOME", home)
	t.Setenv("OPENAI_API_KEY", "resolved-secret")
	source := []byte(`
agents:
  val:
    provider: openai
    model: gpt-test
    api_key: $OPENAI_API_KEY
stream:
  data_dir: ~/.a2a/custom-data
`)
	before := append([]byte(nil), source...)

	cfg, err := Parse(source)
	if err != nil {
		t.Fatal(err)
	}
	if string(source) != string(before) {
		t.Fatal("Parse changed its source bytes")
	}
	if got, want := cfg.Stream.DataDir, filepath.Join(home, ".a2a", "custom-data"); got != want {
		t.Fatalf("data dir = %q, want %q", got, want)
	}
	if got := cfg.Agents["val"].EffectiveAPIKey(); got != "resolved-secret" {
		t.Fatalf("effective API key = %q", got)
	}
	if !bytes.Contains(source, []byte("$OPENAI_API_KEY")) {
		t.Fatal("source no longer contains the environment reference")
	}
}
