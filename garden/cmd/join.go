package cmd

import (
	"context"
	"errors"
	"fmt"
	"io"
	"os"
	"strings"

	"github.com/spf13/cobra"
	"github.com/veridian69/cairn/garden/internal/attention"
	"github.com/veridian69/cairn/garden/internal/invite"
	"golang.org/x/term"
)

func init() { rootCmd.AddCommand(newJoinCommand()) }

// newJoinCommand redeems an invite code issued by `garden invite` and installs the identity it
// names in the Garden home. The code is a secret until it is redeemed, so it is never taken as
// an argument, where the process list and shell history would expose it: it is asked on the
// terminal without echo, or read from standard input ("-", or any non-terminal stdin). A
// passphrase is never read from standard input: a sealed identity asks the terminal for it.
// Output names the identity, the home, the files and the harness default, never the token.
func newJoinCommand() *cobra.Command {
	command := &cobra.Command{
		Use:   "join [-]",
		Short: "Join a Garden with an invite code from its operator",
		Long: "Join a Garden with an invite code from its operator. Run `garden join` and paste the " +
			"garden1: code at the prompt (it is not echoed), or pipe it in with `garden join -`. The code " +
			"is single-use and is never accepted as an argument. A human (cli) identity is stored " +
			"passphrase-sealed, and the passphrase is asked on the terminal.",
		Args:              cobra.MaximumNArgs(1),
		PersistentPreRunE: func(*cobra.Command, []string) error { return nil },
	}
	command.RunE = func(cmd *cobra.Command, args []string) error {
		if len(args) == 1 && args[0] != "-" {
			return errors.New("do not pass the invite code as an argument: the process list and shell history would expose it; run `garden join` and paste it at the prompt, or pipe it to `garden join -`")
		}
		text, err := readJoinCode(cmd)
		if err != nil {
			return err
		}
		code, err := invite.Decode(text)
		if err != nil {
			return err
		}
		dir, err := attention.ResolveHomeDir()
		if err != nil {
			return err
		}
		ctx := cmd.Context()
		if ctx == nil {
			ctx = context.Background()
		}
		result, err := attention.Join(ctx, dir, code)
		if err != nil {
			return err
		}
		out := cmd.OutOrStdout()
		fmt.Fprintf(out, "joined Garden as %s in %s\n", result.Name, result.Home)
		for _, path := range result.Written {
			fmt.Fprintf(out, "  wrote %s\n", path)
		}
		if result.Sealed {
			fmt.Fprintln(out, "  identity is passphrase-sealed; harness sessions cannot use it")
		}
		if result.DefaultSet {
			fmt.Fprintf(out, "  default for %s is now %s\n", result.Harness, result.Name)
		} else if result.Harness != "" {
			fmt.Fprintf(out, "  default for %s already %s; garden.json unchanged\n", result.Harness, result.Name)
		}
		return nil
	}
	return command
}

// stdinIsTerminal is a test seam.
var stdinIsTerminal = func(f *os.File) bool { return term.IsTerminal(int(f.Fd())) }

// joinCodePrompt is a test seam; production asks the controlling terminal without echo.
var joinCodePrompt = func() ([]byte, error) { return attention.ReadPassphrase("invite code: ", false) }

// readJoinCode asks without echo whenever standard input is a terminal, with or without "-", so a
// pasted code never lands in scrollback; otherwise it reads standard input. One line only.
func readJoinCode(cmd *cobra.Command) (string, error) {
	var data []byte
	var err error
	if f, ok := cmd.InOrStdin().(*os.File); ok && stdinIsTerminal(f) {
		data, err = joinCodePrompt()
		if err != nil {
			return "", errors.New("cannot read the invite code from the terminal")
		}
	} else {
		data, err = io.ReadAll(io.LimitReader(cmd.InOrStdin(), 4096))
		if err != nil {
			return "", errors.New("cannot read the invite code from standard input")
		}
	}
	text := strings.TrimSpace(string(data))
	if strings.ContainsAny(text, "\r\n") {
		return "", errors.New("paste the invite code on a single line")
	}
	return text, nil
}
