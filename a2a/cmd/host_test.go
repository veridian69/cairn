package cmd

import (
	"github.com/spf13/cobra"
	"io"
	"testing"
)

func TestHostUsesExplicitRuntimeWithoutDefaultHomeLease(t *testing.T) {
	touched := false
	root := &cobra.Command{Use: "a2a", PersistentPreRunE: func(*cobra.Command, []string) error { touched = true; return nil }}
	root.SetOut(io.Discard)
	root.SetErr(io.Discard)
	root.AddCommand(newHostCommand())
	root.SetArgs([]string{"host", "--config", "/definitely-missing-garden-host.json"})
	if err := root.Execute(); err == nil {
		t.Fatal("missing host configuration accepted")
	}
	if touched {
		t.Fatal("host acquired the unrelated default-home daemon lease")
	}
}
