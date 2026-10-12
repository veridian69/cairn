package chat

import "testing"

func TestColourMapDeterministicAssignment(t *testing.T) {
	colours := newColourMap("operator", []string{"gpt", "claude"})

	if got := colours.colour("operator"); got != colorCoral {
		t.Fatalf("human colour = %v, want coral", got)
	}
	if got := colours.colour("claude"); got != agentPalette[0] {
		t.Fatalf("claude = %v, want first palette colour", got)
	}
	if got := colours.colour("gpt"); got != agentPalette[1] {
		t.Fatalf("gpt = %v, want second palette colour", got)
	}
	first := colours.colour("mystery")
	if first != agentPalette[2] {
		t.Fatalf("mystery = %v, want third palette colour", first)
	}
	if colours.colour("mystery") != first {
		t.Fatal("colour not stable across lookups")
	}
}

func TestColourMapWrapsPalette(t *testing.T) {
	colours := newColourMap("operator", []string{"a", "b", "c", "d", "e", "f"})
	if colours.colour("f") != agentPalette[0] {
		t.Fatal("sixth agent should wrap to first palette colour")
	}
}
