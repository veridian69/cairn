// Package snapshot manages stopped-only A2A runtime snapshots.
package snapshot

import (
	"context"
	"crypto/sha256"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"regexp"
	"sort"
	"strings"
	"time"

	"github.com/veridian69/cairn/a2a/internal/config"
	"github.com/veridian69/cairn/a2a/internal/maintenance"
)

const ownershipMarker = ".a2a-owned"

var snapshotNamePattern = regexp.MustCompile(`^[a-z0-9][a-z0-9_-]{0,47}$`)

type ownershipMarkerDocument struct {
	FormatVersion    int    `json:"format_version"`
	CanonicalDataDir string `json:"canonical_data_dir"`
}

// Manager owns snapshot storage below one A2A configuration directory.
type Manager struct {
	configDir      string
	binaryVersion  string
	now            func() time.Time
	availableBytes func(string) (uint64, error)
}

// NewManager constructs a snapshot manager.
func NewManager(configDir, binaryVersion string) *Manager {
	return &Manager{
		configDir: configDir, binaryVersion: binaryVersion,
		now: time.Now, availableBytes: filesystemAvailableBytes,
	}
}

// Create saves the exact configuration and complete stopped runtime data.
func (m *Manager) Create(ctx context.Context, name string) (Snapshot, error) {
	lease, err := maintenance.Acquire(m.configDir, maintenance.Exclusive)
	if err != nil {
		return Snapshot{}, err
	}
	defer lease.Close()
	if err := ensureLegacyDaemonStopped(m.configDir); err != nil {
		return Snapshot{}, err
	}
	return m.createLocked(ctx, name, true)
}

func (m *Manager) createLocked(ctx context.Context, name string, acquireLegacy bool) (Snapshot, error) {
	if !snapshotNamePattern.MatchString(name) {
		return Snapshot{}, fmt.Errorf("snapshot name %q must match %s", name, snapshotNamePattern)
	}
	configPath := filepath.Join(m.configDir, "config.yaml")
	configBytes, err := os.ReadFile(configPath)
	if err != nil {
		return Snapshot{}, fmt.Errorf("reading config: %w", err)
	}
	cfg, err := config.Parse(configBytes)
	if err != nil {
		return Snapshot{}, err
	}
	dataDir, backupRoot, err := m.preparePaths(cfg.Stream.DataDir)
	if err != nil {
		return Snapshot{}, err
	}
	if err := validateDataDirOwnership(dataDir); err != nil {
		return Snapshot{}, err
	}
	if acquireLegacy {
		storeLease, err := maintenance.AcquireStore(dataDir)
		if err != nil {
			return Snapshot{}, err
		}
		defer storeLease.Close()
		legacyLease, err := acquireLegacyMemoryLease(dataDir)
		if err != nil {
			return Snapshot{}, err
		}
		defer legacyLease.Close()
	}
	if err := ctx.Err(); err != nil {
		return Snapshot{}, err
	}
	sourceBytes, sourceObjects, err := treeBytes(dataDir)
	if err != nil {
		return Snapshot{}, err
	}
	if err := m.requireSpace(
		backupRoot,
		snapshotBytesRequired(sourceBytes, sourceObjects, int64(len(configBytes))),
	); err != nil {
		return Snapshot{}, err
	}

	createdAt := m.now().UTC()
	id, finalPath, err := nextSnapshotPath(backupRoot,
		createdAt.Format("20060102T150405Z")+"-"+name)
	if err != nil {
		return Snapshot{}, err
	}
	stage, err := os.MkdirTemp(backupRoot, ".snapshot-stage-")
	if err != nil {
		return Snapshot{}, err
	}
	keepStage := false
	defer func() {
		if !keepStage {
			_ = os.RemoveAll(stage)
		}
	}()
	if err := os.Chmod(stage, 0700); err != nil {
		return Snapshot{}, err
	}
	if err := writeFileSynced(filepath.Join(stage, "config.yaml"), configBytes, 0600); err != nil {
		return Snapshot{}, err
	}
	stagedData := filepath.Join(stage, "data")
	if _, err := os.Stat(dataDir); os.IsNotExist(err) {
		if err := os.Mkdir(stagedData, 0700); err != nil {
			return Snapshot{}, err
		}
	} else if err != nil {
		return Snapshot{}, err
	} else if err := copyTree(dataDir, stagedData); err != nil {
		return Snapshot{}, err
	}
	if err := ensureOwnershipMarker(stagedData, dataDir); err != nil {
		return Snapshot{}, err
	}
	stateVersion, memoryVersion, err := validateStagedData(ctx, stagedData)
	if err != nil {
		return Snapshot{}, err
	}
	files, total, err := inventory(stage)
	if err != nil {
		return Snapshot{}, err
	}
	configDigest := sha256.Sum256(configBytes)
	doc := manifest{
		FormatVersion: manifestFormatVersion, ID: id, CreatedAt: createdAt,
		BinaryVersion: m.binaryVersion, CanonicalDataDir: dataDir,
		ConfigSHA256:       fmt.Sprintf("%x", configDigest[:]),
		StateSchemaVersion: stateVersion, MemorySchemaVersion: memoryVersion,
		TotalBytes: total, Files: files,
	}
	manifestBytes, err := json.MarshalIndent(doc, "", "  ")
	if err != nil {
		return Snapshot{}, err
	}
	manifestBytes = append(manifestBytes, '\n')
	if err := writeFileSynced(filepath.Join(stage, "manifest.json"), manifestBytes, 0600); err != nil {
		return Snapshot{}, err
	}
	if err := syncTree(stage); err != nil {
		return Snapshot{}, err
	}
	if err := renameNoReplace(stage, finalPath); err != nil {
		return Snapshot{}, err
	}
	keepStage = true
	if err := syncDirectory(backupRoot); err != nil {
		return Snapshot{}, fmt.Errorf(
			"snapshot %s published but syncing backup directory failed: %w",
			id, err,
		)
	}
	return Snapshot{ID: id, CreatedAt: createdAt, Size: total}, nil
}

func nextSnapshotPath(backupRoot, base string) (string, string, error) {
	path := filepath.Join(backupRoot, base)
	if _, err := os.Lstat(path); os.IsNotExist(err) {
		return base, path, nil
	} else if err != nil {
		return "", "", err
	}
	return "", "", fmt.Errorf("snapshot ID %q already exists", base)
}

// List returns saved snapshots newest first.
func (m *Manager) List(ctx context.Context) ([]Snapshot, error) {
	lease, err := maintenance.Acquire(m.configDir, maintenance.Exclusive)
	if err != nil {
		return nil, err
	}
	defer lease.Close()
	if err := ensureLegacyDaemonStopped(m.configDir); err != nil {
		return nil, err
	}
	backupRoot := filepath.Join(m.configDir, "backups")
	entries, err := os.ReadDir(backupRoot)
	if os.IsNotExist(err) {
		return nil, nil
	}
	if err != nil {
		return nil, err
	}
	var snapshots []Snapshot
	for _, entry := range entries {
		if err := ctx.Err(); err != nil {
			return nil, err
		}
		if !entry.IsDir() || entry.Name()[0] == '.' {
			continue
		}
		data, err := os.ReadFile(filepath.Join(backupRoot, entry.Name(), "manifest.json"))
		if err != nil {
			return nil, fmt.Errorf("reading snapshot %q: %w", entry.Name(), err)
		}
		var doc manifest
		if err := json.Unmarshal(data, &doc); err != nil {
			return nil, fmt.Errorf("parsing snapshot %q: %w", entry.Name(), err)
		}
		if doc.FormatVersion != manifestFormatVersion || doc.ID != entry.Name() {
			return nil, fmt.Errorf("invalid snapshot manifest %q", entry.Name())
		}
		snapshots = append(snapshots, Snapshot{
			ID: doc.ID, CreatedAt: doc.CreatedAt, Size: doc.TotalBytes,
		})
	}
	sort.Slice(snapshots, func(i, j int) bool {
		return snapshots[i].CreatedAt.After(snapshots[j].CreatedAt)
	})
	return snapshots, nil
}

func (m *Manager) preparePaths(configuredDataDir string) (string, string, error) {
	dataPath, err := filepath.Abs(configuredDataDir)
	if err != nil {
		return "", "", err
	}
	configPath, err := filepath.Abs(m.configDir)
	if err != nil {
		return "", "", err
	}
	backupPath := filepath.Join(configPath, "backups")
	home, err := os.UserHomeDir()
	if err != nil {
		return "", "", err
	}
	home, err = filepath.Abs(home)
	if err != nil {
		return "", "", err
	}
	if err := validatePathRelationship(dataPath, configPath, backupPath, home); err != nil {
		return "", "", err
	}
	if err := rejectSymlinkComponents(dataPath); err != nil {
		return "", "", err
	}
	if err := os.MkdirAll(m.configDir, 0700); err != nil {
		return "", "", err
	}
	backupRoot := filepath.Join(m.configDir, "backups")
	if err := os.MkdirAll(backupRoot, 0700); err != nil {
		return "", "", err
	}
	if err := os.Chmod(backupRoot, 0700); err != nil {
		return "", "", err
	}
	dataDir := dataPath
	if _, err := os.Stat(dataPath); err == nil {
		dataDir, err = filepath.EvalSymlinks(dataPath)
		if err != nil {
			return "", "", err
		}
	} else if !os.IsNotExist(err) {
		return "", "", err
	}
	configDir, err := filepath.EvalSymlinks(m.configDir)
	if err != nil {
		return "", "", err
	}
	backupRoot, err = filepath.EvalSymlinks(backupRoot)
	if err != nil {
		return "", "", err
	}
	if err := validatePathRelationship(dataDir, configDir, backupRoot, home); err != nil {
		return "", "", err
	}
	return dataDir, backupRoot, nil
}

func validatePathRelationship(dataDir, configDir, backupRoot, home string) error {
	switch {
	case dataDir == string(os.PathSeparator), dataDir == home, dataDir == configDir:
		return fmt.Errorf("refusing dangerous data directory %s", dataDir)
	case pathWithin(dataDir, configDir), pathWithin(dataDir, backupRoot),
		pathWithin(backupRoot, dataDir):
		return fmt.Errorf("data directory %s overlaps A2A configuration or backups", dataDir)
	default:
		return nil
	}
}

func rejectSymlinkComponents(path string) error {
	absolute, err := filepath.Abs(path)
	if err != nil {
		return err
	}
	volume := filepath.VolumeName(absolute)
	current := volume + string(os.PathSeparator)
	relative := strings.TrimPrefix(absolute[len(volume):], string(os.PathSeparator))
	for _, part := range strings.Split(relative, string(os.PathSeparator)) {
		if part == "" {
			continue
		}
		current = filepath.Join(current, part)
		info, err := os.Lstat(current)
		if os.IsNotExist(err) {
			return nil
		}
		if err != nil {
			return err
		}
		if info.Mode()&os.ModeSymlink != 0 {
			return fmt.Errorf("refusing symlinked data path component %s", current)
		}
	}
	return nil
}

func validateDataDirOwnership(dataDir string) error {
	if err := maintenance.ValidateStoreLock(dataDir); err != nil {
		return err
	}
	if _, err := os.Stat(dataDir); os.IsNotExist(err) {
		return nil
	} else if err != nil {
		return err
	}
	marker := filepath.Join(dataDir, ownershipMarker)
	if info, err := os.Lstat(marker); err == nil {
		if !info.Mode().IsRegular() {
			return fmt.Errorf("invalid ownership marker %s", marker)
		}
		return validateOwnershipMarker(dataDir, dataDir)
	} else if !os.IsNotExist(err) {
		return err
	}
	allowed := map[string]bool{
		"state.db": true, "state.db-wal": true, "state.db-shm": true,
		"memory.db": true, "memory.db-wal": true, "memory.db-shm": true,
		"memory.db.lock": true, "jetstream": true, maintenance.StoreLockName: true,
	}
	entries, err := os.ReadDir(dataDir)
	if err != nil {
		return err
	}
	for _, entry := range entries {
		if !allowed[entry.Name()] {
			return fmt.Errorf("refusing unowned data directory %s: unknown entry %s", dataDir, entry.Name())
		}
	}
	return nil
}

func ensureOwnershipMarker(directory, canonicalDataDir string) error {
	if _, err := os.Lstat(filepath.Join(directory, ownershipMarker)); err == nil {
		return validateOwnershipMarker(directory, canonicalDataDir)
	} else if !os.IsNotExist(err) {
		return err
	}
	return writeOwnershipMarker(directory, canonicalDataDir)
}

func validateOwnershipMarker(directory, canonicalDataDir string) error {
	marker := filepath.Join(directory, ownershipMarker)
	data, err := os.ReadFile(marker)
	if err != nil {
		return err
	}
	var document ownershipMarkerDocument
	if err := json.Unmarshal(data, &document); err != nil ||
		document.FormatVersion != 1 ||
		document.CanonicalDataDir != canonicalDataDir {
		return fmt.Errorf("invalid ownership marker %s", marker)
	}
	return nil
}

func writeOwnershipMarker(directory, canonicalDataDir string) error {
	data, err := json.Marshal(ownershipMarkerDocument{
		FormatVersion: 1, CanonicalDataDir: canonicalDataDir,
	})
	if err != nil {
		return err
	}
	data = append(data, '\n')
	return writeFileSynced(filepath.Join(directory, ownershipMarker), data, 0600)
}
