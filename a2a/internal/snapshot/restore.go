package snapshot

import (
	"context"
	"crypto/sha256"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"

	"github.com/veridian69/cairn/a2a/internal/config"
	"github.com/veridian69/cairn/a2a/internal/maintenance"
)

// Restore atomically replaces live runtime data with a validated snapshot.
func (m *Manager) Restore(ctx context.Context, id string) (RestoreResult, error) {
	lease, err := maintenance.Acquire(m.configDir, maintenance.Exclusive)
	if err != nil {
		return RestoreResult{}, err
	}
	defer lease.Close()
	if err := ensureLegacyDaemonStopped(m.configDir); err != nil {
		return RestoreResult{}, err
	}

	doc, snapshotPath, err := m.validateSnapshot(id)
	if err != nil {
		return RestoreResult{}, err
	}
	configBytes, err := os.ReadFile(filepath.Join(m.configDir, "config.yaml"))
	if err != nil {
		return RestoreResult{}, fmt.Errorf("reading current config: %w", err)
	}
	currentDigest := sha256.Sum256(configBytes)
	if fmt.Sprintf("%x", currentDigest[:]) != doc.ConfigSHA256 {
		return RestoreResult{}, fmt.Errorf(
			"current config does not match snapshot %s; review %s",
			id, filepath.Join(snapshotPath, "config.yaml"),
		)
	}
	cfg, err := config.Parse(configBytes)
	if err != nil {
		return RestoreResult{}, err
	}
	dataDir, backupRoot, err := m.preparePaths(cfg.Stream.DataDir)
	if err != nil {
		return RestoreResult{}, err
	}
	if dataDir != doc.CanonicalDataDir {
		return RestoreResult{}, fmt.Errorf(
			"snapshot data path %s does not match configured path %s",
			doc.CanonicalDataDir, dataDir,
		)
	}
	if err := validateDataDirOwnership(dataDir); err != nil {
		return RestoreResult{}, err
	}
	storeLease, err := maintenance.AcquireStore(dataDir)
	if err != nil {
		return RestoreResult{}, err
	}
	defer storeLease.Close()
	_, statErr := os.Stat(dataDir)
	dataExists := statErr == nil
	if statErr != nil && !os.IsNotExist(statErr) {
		return RestoreResult{}, statErr
	}
	legacyLease, err := acquireLegacyMemoryLease(dataDir)
	if err != nil {
		return RestoreResult{}, err
	}
	defer legacyLease.Close()

	currentBytes, currentFiles, err := treeBytes(dataDir)
	if err != nil {
		return RestoreResult{}, err
	}
	savedBytes, savedObjects, err := treeBytes(filepath.Join(snapshotPath, "data"))
	if err != nil {
		return RestoreResult{}, err
	}
	rollbackRequired := snapshotBytesRequired(
		currentBytes, currentFiles, int64(len(configBytes)),
	)
	restoreRequired := snapshotBytesRequired(savedBytes, savedObjects, 0)
	parent := filepath.Dir(dataDir)
	sharedFilesystem, err := sameFilesystem(backupRoot, parent)
	if err != nil {
		return RestoreResult{}, fmt.Errorf("checking restore filesystems: %w", err)
	}
	if sharedFilesystem {
		if err := m.requireSpace(
			backupRoot,
			saturatedAdd(rollbackRequired, restoreRequired),
		); err != nil {
			return RestoreResult{}, err
		}
	} else {
		if err := m.requireSpace(backupRoot, rollbackRequired); err != nil {
			return RestoreResult{}, err
		}
		if err := m.requireSpace(parent, restoreRequired); err != nil {
			return RestoreResult{}, err
		}
	}

	rollback, err := m.createLocked(ctx, "pre-restore", false)
	if err != nil {
		return RestoreResult{}, fmt.Errorf("creating rollback snapshot: %w", err)
	}
	stage, err := os.MkdirTemp(parent, "."+filepath.Base(dataDir)+"-restore-")
	if err != nil {
		return RestoreResult{}, err
	}
	if err := os.Remove(stage); err != nil {
		return RestoreResult{}, err
	}
	committed := false
	defer func() {
		if !committed {
			_ = os.RemoveAll(stage)
		}
	}()
	if err := copyTree(filepath.Join(snapshotPath, "data"), stage); err != nil {
		return RestoreResult{}, err
	}
	if err := ensureOwnershipMarker(stage, dataDir); err != nil {
		return RestoreResult{}, err
	}
	stateVersion, memoryVersion, err := validateStagedData(ctx, stage)
	if err != nil {
		return RestoreResult{}, err
	}
	if stateVersion != doc.StateSchemaVersion || memoryVersion != doc.MemorySchemaVersion {
		return RestoreResult{}, fmt.Errorf(
			"staged schema versions state=%d memory=%d do not match manifest state=%d memory=%d",
			stateVersion, memoryVersion,
			doc.StateSchemaVersion, doc.MemorySchemaVersion,
		)
	}
	if err := ctx.Err(); err != nil {
		return RestoreResult{}, err
	}
	if err := storeLease.PreserveStoreLock(stage); err != nil {
		return RestoreResult{}, err
	}
	if err := syncTree(stage); err != nil {
		return RestoreResult{}, err
	}
	if dataExists {
		if err := exchangeDirectories(dataDir, stage); err != nil {
			return RestoreResult{}, fmt.Errorf("atomic restore exchange: %w", err)
		}
	} else if err := renameNoReplace(stage, dataDir); err != nil {
		return RestoreResult{}, fmt.Errorf("atomic restore publication: %w", err)
	}
	committed = true
	result := RestoreResult{RestoredID: id, RollbackID: rollback.ID}
	if err := syncDirectory(parent); err != nil {
		return result, &CommittedCleanupError{
			Operation: "restore", RetainedPath: stage, Cause: err,
		}
	}
	if dataExists {
		if err := os.RemoveAll(stage); err != nil {
			return result, &CommittedCleanupError{
				Operation: "restore", RetainedPath: stage, Cause: err,
			}
		}
	}
	if err := syncDirectory(parent); err != nil {
		return result, &CommittedCleanupError{
			Operation: "restore", Cause: err,
		}
	}
	return result, nil
}

func (m *Manager) validateSnapshot(id string) (manifest, string, error) {
	if !validSnapshotID(id) {
		return manifest{}, "", fmt.Errorf("invalid snapshot ID %q", id)
	}
	path := filepath.Join(m.configDir, "backups", id)
	manifestBytes, err := os.ReadFile(filepath.Join(path, "manifest.json"))
	if err != nil {
		return manifest{}, "", fmt.Errorf("reading snapshot manifest: %w", err)
	}
	var doc manifest
	if err := json.Unmarshal(manifestBytes, &doc); err != nil {
		return manifest{}, "", fmt.Errorf("parsing snapshot manifest: %w", err)
	}
	if doc.FormatVersion != manifestFormatVersion {
		return manifest{}, "", fmt.Errorf("unsupported snapshot format %d", doc.FormatVersion)
	}
	if doc.ID != id {
		return manifest{}, "", fmt.Errorf("snapshot manifest ID %q does not match %q", doc.ID, id)
	}
	files, total, err := inventory(path)
	if err != nil {
		return manifest{}, "", err
	}
	if total != doc.TotalBytes || !sameFiles(files, doc.Files) {
		return manifest{}, "", fmt.Errorf("snapshot %s failed file integrity validation", id)
	}
	savedConfig, err := os.ReadFile(filepath.Join(path, "config.yaml"))
	if err != nil {
		return manifest{}, "", err
	}
	configDigest := sha256.Sum256(savedConfig)
	if fmt.Sprintf("%x", configDigest[:]) != doc.ConfigSHA256 {
		return manifest{}, "", fmt.Errorf("snapshot %s config checksum mismatch", id)
	}
	if doc.StateSchemaVersion != 1 || doc.MemorySchemaVersion != 1 {
		return manifest{}, "", fmt.Errorf(
			"unsupported snapshot schema versions state=%d memory=%d",
			doc.StateSchemaVersion, doc.MemorySchemaVersion,
		)
	}
	return doc, path, nil
}

func validSnapshotID(id string) bool {
	if len(id) < len("20060102T150405Z-a") {
		return false
	}
	if id[8] != 'T' || id[15] != 'Z' || id[16] != '-' {
		return false
	}
	for index, char := range id {
		switch {
		case index < 8 || (index > 8 && index < 15):
			if char < '0' || char > '9' {
				return false
			}
		case index == 8:
			if char != 'T' {
				return false
			}
		case index == 15:
			if char != 'Z' {
				return false
			}
		case index == 16:
			if char != '-' {
				return false
			}
		default:
			if !(char >= 'a' && char <= 'z') &&
				!(char >= '0' && char <= '9') &&
				char != '_' && char != '-' {
				return false
			}
		}
	}
	return true
}

func sameFiles(left, right []fileInfo) bool {
	if len(left) != len(right) {
		return false
	}
	for index := range left {
		if left[index] != right[index] {
			return false
		}
	}
	return true
}
