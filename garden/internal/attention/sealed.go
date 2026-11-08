package attention

import (
	"errors"
	"fmt"
	"io"
	"os"
	"os/signal"
	"strings"
	"sync"
	"syscall"

	"github.com/veridian69/cairn/garden/internal/identityseal"
	"golang.org/x/sys/unix"
)

// A human identity's token is stored sealed under a passphrase, because agents on the same
// machine run as the same account and could otherwise post as the human. Harness plugins never
// read sealed identities. The passphrase comes only from the controlling terminal.

// The sealing itself lives in internal/identityseal; these names keep the attention API.

// SealedIdentity is the content of identities/<name>.sealed.
type SealedIdentity = identityseal.SealedIdentity

// ErrWrongPassphrase reports a passphrase that does not open the sealed identity.
var ErrWrongPassphrase = identityseal.ErrWrongPassphrase

// SealIdentityToken encrypts token for name under passphrase and returns the file content.
func SealIdentityToken(name, token string, passphrase []byte) ([]byte, error) {
	return identityseal.Seal(name, token, passphrase)
}

// OpenIdentityToken decrypts a sealed identity file for name.
func OpenIdentityToken(name string, data, passphrase []byte) (string, error) {
	return identityseal.Open(name, data, passphrase)
}

func validToken(token string) bool { return identityseal.ValidToken(token) }

func checkPassphrase(p []byte) error { return identityseal.CheckPassphrase(p) }

// openTTY is a test seam; it returns the controlling terminal for reading and writing.
var openTTY = func() (*os.File, error) { return os.OpenFile("/dev/tty", os.O_RDWR, 0) }

// ReadPassphrase asks on the controlling terminal without echo. With confirm it asks twice and
// enforces the minimum length. It refuses when there is no terminal: a passphrase is never
// taken from arguments, the environment or standard input.
func ReadPassphrase(prompt string, confirm bool) ([]byte, error) {
	tty, err := openTTY()
	if err != nil {
		return nil, errors.New("a sealed Garden identity needs a terminal to ask for its passphrase")
	}
	defer tty.Close()
	t, err := newQuietTTY(tty)
	if err != nil {
		return nil, errors.New("cannot use the terminal to ask for the passphrase")
	}
	defer t.close()
	first, err := t.read(prompt)
	if err != nil {
		return nil, err
	}
	if !confirm {
		return first, nil
	}
	if err = checkPassphrase(first); err != nil {
		return nil, err
	}
	second, err := t.read("again: ")
	if err != nil {
		return nil, err
	}
	if string(first) != string(second) {
		return nil, errors.New("the passphrases do not match")
	}
	return first, nil
}

// quietTTY reads lines without echo and puts the terminal back however the process ends. Echo is
// switched only under mu, and a signal handler that takes mu restores the original settings and
// exits while still holding it, so no prompt can switch echo off again after the restore
// (review findings, 8 November 2026: default SIGINT exits skipped term.ReadPassword's own
// restore, and a restore could race with a prompt disabling echo).
type quietTTY struct {
	tty      *os.File
	fd       int
	original unix.Termios
	mu       sync.Mutex
	signals  chan os.Signal
	stop     chan struct{}
	stopped  chan struct{}
}

func newQuietTTY(tty *os.File) (*quietTTY, error) {
	fd := int(tty.Fd())
	original, err := unix.IoctlGetTermios(fd, unix.TCGETS)
	if err != nil {
		return nil, err
	}
	q := &quietTTY{tty: tty, fd: fd, original: *original, signals: make(chan os.Signal, 1), stop: make(chan struct{}), stopped: make(chan struct{})}
	signal.Notify(q.signals, syscall.SIGINT, syscall.SIGTERM, syscall.SIGHUP)
	go q.watch()
	// Echo goes off before any prompt is shown and stays off until close, so nothing typed
	// early or between the two prompts is echoed.
	if err := q.setEcho(false); err != nil {
		q.close()
		return nil, err
	}
	return q, nil
}

func (q *quietTTY) watch() {
	defer close(q.stopped)
	select {
	case sig := <-q.signals:
		q.mu.Lock() // never released: the process exits
		// No terminal output here: a write can block (output paused with Ctrl-S) and keep
		// the process alive holding the lock.
		_ = unix.IoctlSetTermios(q.fd, unix.TCSETS, &q.original)
		code := 128
		if s, ok := sig.(syscall.Signal); ok {
			code += int(s)
		}
		os.Exit(code)
	case <-q.stop:
	}
}

// close restores the terminal first, while the handler can still catch a signal, and only then
// stops and joins the handler: a signal between the two finds echo already restored.
func (q *quietTTY) close() {
	q.mu.Lock()
	_ = unix.IoctlSetTermios(q.fd, unix.TCSETS, &q.original)
	q.mu.Unlock()
	signal.Stop(q.signals)
	close(q.stop)
	<-q.stopped
}

func (q *quietTTY) setEcho(on bool) error {
	q.mu.Lock()
	defer q.mu.Unlock()
	mode := q.original
	if !on {
		mode.Lflag &^= unix.ECHO
		mode.Lflag |= unix.ICANON | unix.ISIG
	}
	return unix.IoctlSetTermios(q.fd, unix.TCSETS, &mode)
}

func (q *quietTTY) read(prompt string) ([]byte, error) {
	fmt.Fprint(q.tty, prompt)
	line, err := readLine(q.tty)
	fmt.Fprintln(q.tty)
	if err != nil {
		return nil, errors.New("cannot read the passphrase from the terminal")
	}
	return line, nil
}

// readLine reads up to a newline, byte by byte so nothing past it is consumed, at most 4 KiB.
func readLine(f *os.File) ([]byte, error) {
	var line []byte
	b := make([]byte, 1)
	for len(line) <= 4096 {
		n, err := f.Read(b)
		if n == 1 {
			if b[0] == '\n' {
				return []byte(strings.TrimSuffix(string(line), "\r")), nil
			}
			line = append(line, b[0])
			continue
		}
		if err != nil {
			if err == io.EOF && len(line) > 0 {
				return line, nil
			}
			return nil, err
		}
	}
	return nil, errors.New("line too long")
}
