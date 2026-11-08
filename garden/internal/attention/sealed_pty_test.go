package attention

import (
	"fmt"
	"os"
	"os/exec"
	"strings"
	"syscall"
	"testing"
	"time"

	"golang.org/x/sys/unix"
)

// openPTY returns a pseudo-terminal pair through /dev/ptmx, without a third-party package.
func openPTY(t *testing.T) (master, slave *os.File) {
	t.Helper()
	m, err := os.OpenFile("/dev/ptmx", os.O_RDWR|syscall.O_NOCTTY, 0)
	if err != nil {
		t.Skipf("no pseudo-terminals here: %v", err)
	}
	if err = unix.IoctlSetPointerInt(int(m.Fd()), unix.TIOCSPTLCK, 0); err != nil {
		m.Close()
		t.Skipf("cannot unlock the pseudo-terminal: %v", err)
	}
	n, err := unix.IoctlGetInt(int(m.Fd()), unix.TIOCGPTN)
	if err != nil {
		m.Close()
		t.Skipf("cannot name the pseudo-terminal: %v", err)
	}
	s, err := os.OpenFile(fmt.Sprintf("/dev/pts/%d", n), os.O_RDWR|syscall.O_NOCTTY, 0)
	if err != nil {
		m.Close()
		t.Skipf("cannot open the pseudo-terminal: %v", err)
	}
	return m, s
}

// A signal at any point of a passphrase prompt must leave the terminal echoing and exit with
// 128 plus the signal (review findings, 8 November 2026). The prompt runs in a child whose
// controlling terminal is a real pty; the test signals it during setup, at the prompt and
// between the two prompts, then checks ECHO on the pty itself.
func TestReadPassphraseRestoresEchoOnSignal(t *testing.T) {
	if os.Getenv("GARDEN_PASSPHRASE_CHILD") == "1" {
		_, _ = ReadPassphrase("passphrase: ", true)
		os.Exit(0)
	}
	cases := []struct {
		name string
		sig  syscall.Signal
		when string
		code int
	}{
		{"interrupt during setup", syscall.SIGINT, "start", 130},
		{"interrupt at the prompt", syscall.SIGINT, "quiet", 130},
		{"interrupt once the prompt is shown", syscall.SIGINT, "prompt", 130},
		{"interrupt between prompts", syscall.SIGINT, "again", 130},
		{"hangup at the prompt", syscall.SIGHUP, "quiet", 129},
		{"terminate at the prompt", syscall.SIGTERM, "quiet", 143},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			master, slave := openPTY(t)
			defer master.Close()
			defer slave.Close()
			child := exec.Command(os.Args[0], "-test.run=^TestReadPassphraseRestoresEchoOnSignal$")
			child.Env = append(os.Environ(), "GARDEN_PASSPHRASE_CHILD=1")
			child.Stdin, child.Stdout, child.Stderr = slave, slave, slave
			child.SysProcAttr = &syscall.SysProcAttr{Setsid: true, Setctty: true, Ctty: 0}
			if err := child.Start(); err != nil {
				t.Fatal(err)
			}
			output := make(chan string, 64)
			go func() {
				buf := make([]byte, 4096)
				for {
					n, err := master.Read(buf)
					if n > 0 {
						output <- string(buf[:n])
					}
					if err != nil {
						close(output)
						return
					}
				}
			}()
			echoOff := func() bool {
				termios, err := unix.IoctlGetTermios(int(slave.Fd()), unix.TCGETS)
				return err == nil && termios.Lflag&unix.ECHO == 0
			}
			waitFor := func(cond func() bool, what string) {
				deadline := time.Now().Add(10 * time.Second)
				for !cond() {
					if time.Now().After(deadline) {
						_ = child.Process.Kill()
						t.Fatalf("timed out waiting for %s", what)
					}
					time.Sleep(5 * time.Millisecond)
				}
			}
			switch tc.when {
			case "quiet":
				waitFor(echoOff, "the prompt to turn echo off")
			case "again":
				waitFor(echoOff, "the first prompt")
				if _, err := master.Write([]byte("correct horse battery\n")); err != nil {
					t.Fatal(err)
				}
				seen := ""
				waitFor(func() bool {
					select {
					case s := <-output:
						seen += s
					default:
					}
					return strings.Contains(seen, "again: ")
				}, "the second prompt")
				if !echoOff() {
					t.Fatal("echo was on between the two prompts")
				}
			case "prompt":
				seen := ""
				waitFor(func() bool {
					select {
					case s := <-output:
						seen += s
					default:
					}
					return strings.Contains(seen, "passphrase: ")
				}, "the first prompt")
				if !echoOff() {
					t.Fatal("the prompt was shown while echo was still on")
				}
			}
			_ = child.Process.Signal(tc.sig)
			err := child.Wait()
			// A signal that lands before the child reaches the prompt kills it by default; there
			// is nothing to restore then, and echo must simply still be on.
			early := tc.when == "start" && err != nil && strings.Contains(err.Error(), "signal: ")
			if !early && (err == nil || !strings.Contains(err.Error(), fmt.Sprintf("exit status %d", tc.code))) {
				t.Fatalf("signalled prompt exited with %v, want status %d", err, tc.code)
			}
			if echoOff() {
				t.Fatal("the signal left the terminal without echo")
			}
		})
	}
}
