package snapshot

import (
	"errors"
	"fmt"
	"net"
	"net/url"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"syscall"
	"time"
)

type legacyMemoryLease struct {
	file *os.File
}

func acquireLegacyMemoryLease(dataDir string) (*legacyMemoryLease, error) {
	path := filepath.Join(dataDir, "memory.db.lock")
	file, err := os.OpenFile(path, os.O_RDWR, 0)
	if os.IsNotExist(err) {
		return &legacyMemoryLease{}, nil
	}
	if err != nil {
		return nil, err
	}
	if err := syscall.Flock(int(file.Fd()), syscall.LOCK_EX|syscall.LOCK_NB); err != nil {
		_ = file.Close()
		if errors.Is(err, syscall.EWOULDBLOCK) || errors.Is(err, syscall.EAGAIN) {
			return nil, fmt.Errorf(
				"runtime is in use (legacy memory lock %s): stop every A2A command first",
				path,
			)
		}
		return nil, err
	}
	return &legacyMemoryLease{file: file}, nil
}

func (l *legacyMemoryLease) Close() error {
	if l == nil || l.file == nil {
		return nil
	}
	if err := syscall.Flock(int(l.file.Fd()), syscall.LOCK_UN); err != nil {
		_ = l.file.Close()
		return err
	}
	return l.file.Close()
}

func ensureLegacyDaemonStopped(configDir string) error {
	pidPath := filepath.Join(configDir, "daemon.pid")
	urlPath := filepath.Join(configDir, "daemon.url")
	data, err := os.ReadFile(pidPath)
	pidExists := err == nil
	if err != nil && !os.IsNotExist(err) {
		return err
	}
	if pidExists {
		pid, parseErr := strconv.Atoi(strings.TrimSpace(string(data)))
		if parseErr != nil || pid <= 0 {
			return fmt.Errorf("cannot prove daemon is stopped: invalid PID file %s", pidPath)
		}
		signalErr := syscall.Kill(pid, 0)
		switch {
		case signalErr == nil, errors.Is(signalErr, syscall.EPERM):
			return fmt.Errorf("runtime is in use by daemon pid %d", pid)
		case errors.Is(signalErr, syscall.ESRCH):
		default:
			return fmt.Errorf("checking daemon pid %d: %w", pid, signalErr)
		}
	}

	urlBytes, err := os.ReadFile(urlPath)
	urlExists := err == nil
	if err != nil && !os.IsNotExist(err) {
		return err
	}
	if urlExists {
		rawURL := strings.TrimSpace(string(urlBytes))
		live, err := probeLocalDaemonURL(rawURL)
		if err != nil {
			return fmt.Errorf("cannot prove daemon is stopped from %s: %w", urlPath, err)
		}
		if live {
			return fmt.Errorf("runtime is in use by daemon at %s", rawURL)
		}
	}
	if pidExists {
		if err := os.Remove(pidPath); err != nil && !os.IsNotExist(err) {
			return err
		}
	}
	if urlExists {
		if err := os.Remove(urlPath); err != nil && !os.IsNotExist(err) {
			return err
		}
	}
	return nil
}

func probeLocalDaemonURL(rawURL string) (bool, error) {
	parsed, err := url.Parse(rawURL)
	if err != nil || parsed.Scheme != "nats" || parsed.Host == "" {
		return false, fmt.Errorf("invalid daemon URL %q", rawURL)
	}
	host, port, err := net.SplitHostPort(parsed.Host)
	if err != nil || port == "" {
		return false, fmt.Errorf("invalid daemon URL %q", rawURL)
	}
	ip := net.ParseIP(host)
	if host != "localhost" && (ip == nil || !ip.IsLoopback()) {
		return false, fmt.Errorf("refusing to probe non-local daemon URL %q", rawURL)
	}
	connection, err := net.DialTimeout("tcp", net.JoinHostPort(host, port), 250*time.Millisecond)
	if err != nil {
		return false, nil
	}
	return true, connection.Close()
}
