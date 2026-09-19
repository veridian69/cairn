package cmd

import (
	"io"
	"testing"

	"github.com/spf13/cobra"
)

func TestRemoteCommandsDoNotAcquireLocalGardenRuntime(t *testing.T) {
	for _, command := range []*cobra.Command{newConnectCommand(), newAttendCommand(), newDoctorCommand()} {
		called := false
		root := &cobra.Command{Use: "a2a", PersistentPreRunE: func(*cobra.Command, []string) error { called = true; return nil }}
		root.SetOut(io.Discard)
		root.SetErr(io.Discard)
		root.AddCommand(command)
		root.SetArgs([]string{command.Name(), "--profile", "/definitely-missing-garden-profile"})
		if err := root.Execute(); err == nil {
			t.Fatal("missing profile accepted")
		}
		if called {
			t.Fatal("remote command touched the local daemon bootstrap")
		}
	}
}
