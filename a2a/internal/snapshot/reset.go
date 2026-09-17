package snapshot

import (
	"context"
	"fmt"
	"os"
	"path/filepath"

	"github.com/veridian69/cairn/a2a/internal/config"
	"github.com/veridian69/cairn/a2a/internal/maintenance"
	"golang.org/x/sys/unix"
)

// CommittedCleanupError reports a failure after an atomic exchange committed.
type CommittedCleanupError struct {
	// Operation is the maintenance operation that committed.
	Operation string
	// RetainedPath identifies old runtime data that could not be removed.
	RetainedPath string
	// Cause is the post-commit cleanup or durability failure.
	Cause error
}

// Error states that the exchange committed and describes any retained data.
func (e *CommittedCleanupError) Error() string {
	if e.RetainedPath != "" {
		return fmt.Sprintf("%s committed, but old data retained at %s: %v",
			e.Operation, e.RetainedPath, e.Cause)
	}
	return fmt.Sprintf("%s committed, but final durability sync failed: %v",
		e.Operation, e.Cause)
}

// Unwrap exposes the post-commit cleanup or durability failure.
func (e *CommittedCleanupError) Unwrap() error { return e.Cause }

// Reset atomically replaces all runtime state with an owned empty directory.
func (m *Manager) Reset(ctx context.Context) error {
	lease, err := maintenance.Acquire(m.configDir, maintenance.Exclusive)
	if err != nil {
		return err
	}
	defer lease.Close()
	if err := ensureLegacyDaemonStopped(m.configDir); err != nil {
		return err
	}

	configBytes, err := os.ReadFile(filepath.Join(m.configDir, "config.yaml"))
	if err != nil {
		return fmt.Errorf("reading config: %w", err)
	}
	cfg, err := config.Parse(configBytes)
	if err != nil {
		return err
	}
	dataDir, _, err := m.preparePaths(cfg.Stream.DataDir)
	if err != nil {
		return err
	}
	if err := validateDataDirOwnership(dataDir); err != nil {
		return err
	}
	storeLease, err := maintenance.AcquireStore(dataDir)
	if err != nil {
		return err
	}
	defer storeLease.Close()
	_, statErr := os.Stat(dataDir)
	dataExists := statErr == nil
	if statErr != nil && !os.IsNotExist(statErr) {
		return statErr
	}
	legacyLease, err := acquireLegacyMemoryLease(dataDir)
	if err != nil {
		return err
	}
	defer legacyLease.Close()
	parent := filepath.Dir(dataDir)
	stage, err := os.MkdirTemp(parent, "."+filepath.Base(dataDir)+"-reset-")
	if err != nil {
		return err
	}
	committed := false
	defer func() {
		if !committed {
			_ = os.RemoveAll(stage)
		}
	}()
	if err := os.Chmod(stage, 0700); err != nil {
		return err
	}
	if err := writeOwnershipMarker(stage, dataDir); err != nil {
		return err
	}
	if err := storeLease.PreserveStoreLock(stage); err != nil {
		return err
	}
	if err := syncDirectory(stage); err != nil {
		return err
	}
	if err := ctx.Err(); err != nil {
		return err
	}
	if dataExists {
		if err := exchangeDirectories(dataDir, stage); err != nil {
			return fmt.Errorf("atomic reset exchange: %w", err)
		}
	} else if err := renameNoReplace(stage, dataDir); err != nil {
		return fmt.Errorf("atomic reset publication: %w", err)
	}
	committed = true
	if err := syncDirectory(parent); err != nil {
		return &CommittedCleanupError{
			Operation: "reset", RetainedPath: stage, Cause: err,
		}
	}
	if dataExists {
		if err := os.RemoveAll(stage); err != nil {
			return &CommittedCleanupError{
				Operation: "reset", RetainedPath: stage, Cause: err,
			}
		}
	}
	if err := syncDirectory(parent); err != nil {
		return &CommittedCleanupError{
			Operation: "reset", Cause: err,
		}
	}
	return nil
}

func exchangeDirectories(left, right string) error {
	return unix.Renameat2(
		unix.AT_FDCWD, left,
		unix.AT_FDCWD, right,
		unix.RENAME_EXCHANGE,
	)
}

func renameNoReplace(source, destination string) error {
	return unix.Renameat2(
		unix.AT_FDCWD, source,
		unix.AT_FDCWD, destination,
		unix.RENAME_NOREPLACE,
	)
}

func syncDirectory(path string) error {
	directory, err := os.Open(path)
	if err != nil {
		return err
	}
	defer directory.Close()
	return directory.Sync()
}
