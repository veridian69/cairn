package cmd

import (
	"fmt"
	"os"
	"strconv"
	"time"

	"github.com/spf13/cobra"
	"github.com/veridian69/cairn/garden/internal/remotecli"
)

// remoteFlag selects the remote Garden gateway instead of the local daemon. GARDEN_REMOTE carries
// the same meaning; an explicit --remote, true or false, overrides it.
const remoteFlag = "remote"

// openRemote opens the remote session. Tests replace it.
var openRemote = remotecli.Open

// remoteCallTimeout bounds one remote say or history call. The client applies its own limit per call.
const remoteCallTimeout = 30 * time.Second

// addRemoteFlag registers --remote on a command that has a remote mode.
func addRemoteFlag(command *cobra.Command) {
	command.Flags().Bool(remoteFlag, false, "use the Garden gateway from garden.json instead of the local daemon (GARDEN_REMOTE=1 does the same)")
}

// remoteRequested reports whether a command runs in remote mode. An explicit --remote flag decides;
// otherwise GARDEN_REMOTE does, and only 1 and 0 (or any strconv boolean) are accepted.
func remoteRequested(command *cobra.Command) (bool, error) {
	if flag := command.Flags().Lookup(remoteFlag); flag != nil && flag.Changed {
		return command.Flags().GetBool(remoteFlag)
	}
	raw := os.Getenv("GARDEN_REMOTE")
	if raw == "" {
		return false, nil
	}
	on, err := strconv.ParseBool(raw)
	if err != nil {
		return false, fmt.Errorf("GARDEN_REMOTE must be 1 or 0, not %q", raw)
	}
	return on, nil
}

// remotePreRun replaces the root's persistent pre-run hook for the commands that have a remote mode.
// Cobra runs only the nearest hook, so the local path calls the root hook itself and keeps its
// shared runtime lease. Remote mode takes no local lease and reads no local runtime file.
func remotePreRun(command *cobra.Command, args []string) error {
	remote, err := remoteRequested(command)
	if err != nil {
		return err
	}
	if remote {
		return nil
	}
	return acquireCommandLease(command, args)
}
