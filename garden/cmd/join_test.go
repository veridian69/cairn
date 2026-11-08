package cmd

import (
	"bytes"
	"errors"
	"io"
	"net"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/veridian69/cairn/garden/internal/invite"
)

// joinCommandEnv points the Garden home and the user home at fresh directories for one test.
func joinCommandEnv(t *testing.T) string {
	t.Helper()
	t.Setenv("HOME", t.TempDir())
	home := filepath.Join(t.TempDir(), "garden")
	t.Setenv("GARDEN_HOME", home)
	return home
}

// joinRun runs `garden join` with args and standard input text, and returns its error.
func joinRun(t *testing.T, input string, args ...string) error {
	t.Helper()
	command := newJoinCommand()
	command.SetOut(io.Discard)
	command.SetErr(io.Discard)
	command.SetIn(strings.NewReader(input))
	command.SetArgs(args)
	return command.Execute()
}

// unreachableCode is a well-formed invite for a local port nothing listens on. The port is bound
// and released, so the dial is refused at once rather than left to time out.
func unreachableCode(t *testing.T) string {
	t.Helper()
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	addr := listener.Addr().String()
	if err := listener.Close(); err != nil {
		t.Fatal(err)
	}
	code, err := invite.Code{Endpoint: "https://" + addr + "/mcp", CASHA256: strings.Repeat("0", 64), Secret: bytes.Repeat([]byte{1}, 32)}.Encode()
	if err != nil {
		t.Fatal(err)
	}
	return code
}

func TestJoinRejectsBadCodeBeforeTouchingHome(t *testing.T) {
	home := joinCommandEnv(t)
	err := joinRun(t, "garden1:not-a-code\n", "-")
	if err == nil || !strings.Contains(err.Error(), "not a valid Garden invite code") {
		t.Fatalf("bad code: %v", err)
	}
	if _, statErr := os.Stat(home); !errors.Is(statErr, os.ErrNotExist) {
		t.Fatalf("home created for a bad code: %v", statErr)
	}
}

func TestJoinStdinCodeMustBeOneLine(t *testing.T) {
	home := joinCommandEnv(t)
	// A passphrase pasted after the code is not part of the code and is never read here.
	err := joinRun(t, unreachableCode(t)+"\nthis is a passphrase\n", "-")
	if err == nil || !strings.Contains(err.Error(), "single line") {
		t.Fatalf("multi-line stdin accepted: %v", err)
	}
	if _, statErr := os.Stat(home); !errors.Is(statErr, os.ErrNotExist) {
		t.Fatalf("home created for multi-line stdin: %v", statErr)
	}
}

// The code is a secret until redeemed, so an argument (process list, shell history) is refused
// before anything else happens.
func TestJoinRefusesTheCodeAsAnArgument(t *testing.T) {
	home := joinCommandEnv(t)
	err := joinRun(t, "", unreachableCode(t))
	if err == nil || !strings.Contains(err.Error(), "do not pass the invite code as an argument") {
		t.Fatalf("code as argument: %v", err)
	}
	if _, statErr := os.Stat(home); !errors.Is(statErr, os.ErrNotExist) {
		t.Fatalf("home created for a refused argument: %v", statErr)
	}
}

func TestJoinCodeFromStdinReachesRedemption(t *testing.T) {
	code := unreachableCode(t)
	for name, run := range map[string]func(*testing.T) error{
		"dash":    func(t *testing.T) error { return joinRun(t, code+"\n", "-") },
		"no args": func(t *testing.T) error { return joinRun(t, code+"\n") },
	} {
		t.Run(name, func(t *testing.T) {
			home := joinCommandEnv(t)
			err := run(t)
			if err == nil || !strings.Contains(err.Error(), "cannot redeem the invite") {
				t.Fatalf("unreachable gateway: %v", err)
			}
			for _, rel := range []string{"garden.json", "identities", "tls"} {
				if _, statErr := os.Stat(filepath.Join(home, rel)); !errors.Is(statErr, os.ErrNotExist) {
					t.Fatalf("%s written for an unreachable gateway: %v", rel, statErr)
				}
			}
		})
	}
}

// A code typed or pasted at a terminal is read without echo even with "-" (re-review finding).
func TestJoinDashAtATerminalStillReadsWithoutEcho(t *testing.T) {
	joinCommandEnv(t)
	tty, err := os.CreateTemp(t.TempDir(), "tty")
	if err != nil {
		t.Fatal(err)
	}
	defer tty.Close()
	oldTerminal, oldPrompt := stdinIsTerminal, joinCodePrompt
	t.Cleanup(func() { stdinIsTerminal, joinCodePrompt = oldTerminal, oldPrompt })
	stdinIsTerminal = func(*os.File) bool { return true }
	prompted := false
	joinCodePrompt = func() ([]byte, error) { prompted = true; return []byte("garden1:not-a-code"), nil }
	command := newJoinCommand()
	command.SetIn(tty)
	command.SetOut(io.Discard)
	command.SetErr(io.Discard)
	command.SetArgs([]string{"-"})
	if err := command.Execute(); err == nil || !strings.Contains(err.Error(), "not a valid Garden invite code") {
		t.Fatalf("join - at a terminal: %v", err)
	}
	if !prompted {
		t.Fatal("join - at a terminal read the code with echo instead of the no-echo prompt")
	}
}
