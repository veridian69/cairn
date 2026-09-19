package cmd

import (
	"slices"
	"testing"

	"github.com/veridian69/cairn/a2a/internal/config"
)

func TestAgentNamesAreSorted(t *testing.T) {
	cfg := &config.Config{
		Agents: map[string]config.AgentConfig{
			"zed":   {},
			"alpha": {},
			"mira":  {},
		},
	}

	got := agentNames(cfg)
	want := []string{"alpha", "mira", "zed"}
	if !slices.Equal(got, want) {
		t.Fatalf("agentNames() = %v, want %v", got, want)
	}
}

func TestDefaultIdentityHonorsExplicitValue(t *testing.T) {
	if got := defaultIdentity("  claude  "); got != "claude" {
		t.Fatalf("defaultIdentity() = %q, want %q", got, "claude")
	}
}

func TestChatHumanIdentityFallsBackForAgentIdentity(t *testing.T) {
	got := chatHumanIdentity("claude", []string{"claude", "gpt"})
	if got == "claude" {
		t.Fatalf("chatHumanIdentity() should not reuse an agent name as the human identity")
	}
	if got == "" {
		t.Fatal("chatHumanIdentity() returned empty identity")
	}
}

func TestChatHumanIdentityUsesExplicitNonAgentName(t *testing.T) {
	if got := chatHumanIdentity("  operator  ", []string{"claude", "gpt"}); got != "operator" {
		t.Fatalf("chatHumanIdentity() = %q, want %q", got, "operator")
	}
}

func TestHumanIdentityUsesExplicitValue(t *testing.T) {
	if got := humanIdentity("  dexter  "); got != "dexter" {
		t.Fatalf("humanIdentity() = %q, want %q", got, "dexter")
	}
}
