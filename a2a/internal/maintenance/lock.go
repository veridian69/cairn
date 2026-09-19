// Package maintenance serialises operations that replace A2A's runtime data.
package maintenance

import (
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"sync"
	"syscall"
)

// Mode selects a shared ordinary-command lease or an exclusive maintenance lease.
type Mode uint8

const (
	// Shared permits concurrent ordinary commands but excludes maintenance.
	Shared Mode = iota
	// Exclusive permits one maintenance operation and excludes every command.
	Exclusive
)

// InUseError reports that another A2A process holds an incompatible lease.
type InUseError struct {
	// Path is the installation lock file.
	Path string
	// Mode is the lease mode that could not be acquired.
	Mode Mode
}

// Error describes the incompatible installation lease.
func (e *InUseError) Error() string {
	if e.Mode == Exclusive {
		return fmt.Sprintf("runtime is in use (lock %s): stop every A2A command first", e.Path)
	}
	return fmt.Sprintf("runtime maintenance is in progress (lock %s)", e.Path)
}

// Lease holds an installation lock until Close.
type Lease struct {
	file      *os.File
	closeOnce sync.Once
	closeErr  error
}

// Acquire obtains a non-blocking installation lease.
func Acquire(configDir string, mode Mode) (*Lease, error) {
	if mode != Shared && mode != Exclusive {
		return nil, fmt.Errorf("invalid maintenance lock mode %d", mode)
	}
	if err := os.MkdirAll(configDir, 0700); err != nil {
		return nil, fmt.Errorf("creating config directory: %w", err)
	}
	path := filepath.Join(configDir, "runtime.lock")
	file, err := os.OpenFile(path, os.O_CREATE|os.O_RDWR, 0600)
	if err != nil {
		return nil, fmt.Errorf("opening maintenance lock: %w", err)
	}
	operation := syscall.LOCK_SH | syscall.LOCK_NB
	if mode == Exclusive {
		operation = syscall.LOCK_EX | syscall.LOCK_NB
	}
	if err := syscall.Flock(int(file.Fd()), operation); err != nil {
		_ = file.Close()
		if errors.Is(err, syscall.EWOULDBLOCK) || errors.Is(err, syscall.EAGAIN) {
			return nil, &InUseError{Path: path, Mode: mode}
		}
		return nil, fmt.Errorf("locking %s: %w", path, err)
	}
	return &Lease{file: file}, nil
}

// Close releases the lease.
func (l *Lease) Close() error {
	if l == nil {
		return nil
	}
	l.closeOnce.Do(func() {
		if err := syscall.Flock(int(l.file.Fd()), syscall.LOCK_UN); err != nil {
			l.closeErr = err
		}
		if err := l.file.Close(); err != nil && l.closeErr == nil {
			l.closeErr = err
		}
	})
	return l.closeErr
}
