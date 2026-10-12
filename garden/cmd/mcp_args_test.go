package cmd

import "testing"

func TestMCPCommandRejectsPositionalArgs(t *testing.T) {
	if mcpCmd.Args == nil {
		t.Fatal("mcp command must reject positional arguments")
	}
	if err := mcpCmd.Args(mcpCmd, []string{"claude"}); err == nil {
		t.Fatal("expected error for positional args (a stray name should fail loudly, not start nameless)")
	}
}
