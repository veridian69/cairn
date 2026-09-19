package snapshot

import (
	"bytes"
	"context"
	"database/sql"
	"fmt"
	"net"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/veridian69/cairn/a2a/internal/maintenance"
	"github.com/veridian69/cairn/a2a/internal/memory"
	"github.com/veridian69/cairn/a2a/internal/model"
	"github.com/veridian69/cairn/a2a/internal/state"
)

func TestCreateAndListSnapshotPreserveExactConfig(t *testing.T) {
	root := t.TempDir()
	configDir := filepath.Join(root, ".a2a")
	dataDir := filepath.Join(root, "runtime")
	if err := os.MkdirAll(dataDir, 0700); err != nil {
		t.Fatal(err)
	}
	configBytes := []byte(fmt.Sprintf(
		"agents: {}\nstream:\n  data_dir: %s\n", dataDir,
	))
	if err := os.MkdirAll(configDir, 0700); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(configDir, "config.yaml"), configBytes, 0600); err != nil {
		t.Fatal(err)
	}

	manager := NewManager(configDir, "test-version")
	created, err := manager.Create(context.Background(), "before-test")
	if err != nil {
		t.Fatal(err)
	}
	if created.ID == "" || created.Size == 0 {
		t.Fatalf("created snapshot = %#v", created)
	}
	savedConfig, err := os.ReadFile(filepath.Join(configDir, "backups", created.ID, "config.yaml"))
	if err != nil {
		t.Fatal(err)
	}
	if !bytes.Equal(savedConfig, configBytes) {
		t.Fatalf("saved config changed:\n%s", savedConfig)
	}
	snapshots, err := manager.List(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	if len(snapshots) != 1 || snapshots[0].ID != created.ID {
		t.Fatalf("snapshots = %#v", snapshots)
	}
}

func TestCreateRejectsCorruptStateDatabase(t *testing.T) {
	root := t.TempDir()
	configDir := filepath.Join(root, ".a2a")
	dataDir := filepath.Join(root, "runtime")
	if err := os.MkdirAll(dataDir, 0700); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(dataDir, "state.db"), []byte("not sqlite"), 0600); err != nil {
		t.Fatal(err)
	}
	if err := os.MkdirAll(configDir, 0700); err != nil {
		t.Fatal(err)
	}
	configBytes := []byte(fmt.Sprintf("agents: {}\nstream:\n  data_dir: %s\n", dataDir))
	if err := os.WriteFile(filepath.Join(configDir, "config.yaml"), configBytes, 0600); err != nil {
		t.Fatal(err)
	}

	manager := NewManager(configDir, "test-version")
	if _, err := manager.Create(context.Background(), "corrupt"); err == nil {
		t.Fatal("Create accepted a corrupt state database")
	}
	entries, err := os.ReadDir(filepath.Join(configDir, "backups"))
	if err != nil {
		t.Fatal(err)
	}
	for _, entry := range entries {
		if entry.IsDir() && entry.Name()[0] != '.' {
			t.Fatalf("published corrupt snapshot %q", entry.Name())
		}
	}
}

func TestValidateStagedStateRejectsUnknownSchemaShape(t *testing.T) {
	dataDir := t.TempDir()
	path := filepath.Join(dataDir, "state.db")
	db, err := state.Open(path)
	if err != nil {
		t.Fatal(err)
	}
	if err := db.Close(); err != nil {
		t.Fatal(err)
	}
	raw, err := sql.Open("sqlite", path)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := raw.Exec("ALTER TABLE participants ADD COLUMN unexpected TEXT"); err != nil {
		t.Fatal(err)
	}
	if err := raw.Close(); err != nil {
		t.Fatal(err)
	}

	if _, err := validateSQLiteCopy(path, "state"); err == nil ||
		!strings.Contains(err.Error(), "unknown schema") {
		t.Fatalf("validateSQLiteCopy error = %v, want unknown schema", err)
	}
}

func TestResetReplacesRuntimeDataAndPreservesConfigAndBackups(t *testing.T) {
	root := t.TempDir()
	configDir := filepath.Join(root, ".a2a")
	dataDir := filepath.Join(root, "runtime")
	if err := os.MkdirAll(filepath.Join(dataDir, "jetstream"), 0700); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(dataDir, "state.db"), []byte("old-state"), 0600); err != nil {
		t.Fatal(err)
	}
	if err := os.MkdirAll(filepath.Join(configDir, "backups", "keep-me"), 0700); err != nil {
		t.Fatal(err)
	}
	configBytes := []byte(fmt.Sprintf("agents: {}\nstream:\n  data_dir: %s\n", dataDir))
	if err := os.WriteFile(filepath.Join(configDir, "config.yaml"), configBytes, 0600); err != nil {
		t.Fatal(err)
	}

	manager := NewManager(configDir, "test-version")
	if err := manager.Reset(context.Background()); err != nil {
		t.Fatal(err)
	}
	entries, err := os.ReadDir(dataDir)
	if err != nil {
		t.Fatal(err)
	}
	if len(entries) != 2 || entries[0].Name() != maintenance.StoreLockName || entries[1].Name() != ownershipMarker {
		t.Fatalf("reset data entries = %#v", entries)
	}
	gotConfig, err := os.ReadFile(filepath.Join(configDir, "config.yaml"))
	if err != nil {
		t.Fatal(err)
	}
	if !bytes.Equal(gotConfig, configBytes) {
		t.Fatal("reset changed config")
	}
	if _, err := os.Stat(filepath.Join(configDir, "backups", "keep-me")); err != nil {
		t.Fatalf("reset removed backup: %v", err)
	}
}

func TestResetPublishesOwnedEmptyDirectoryWhenRuntimeIsAbsent(t *testing.T) {
	root := t.TempDir()
	configDir := filepath.Join(root, ".a2a")
	dataDir := filepath.Join(root, "runtime")
	if err := os.MkdirAll(configDir, 0700); err != nil {
		t.Fatal(err)
	}
	configBytes := []byte(fmt.Sprintf("agents: {}\nstream:\n  data_dir: %s\n", dataDir))
	if err := os.WriteFile(filepath.Join(configDir, "config.yaml"), configBytes, 0600); err != nil {
		t.Fatal(err)
	}

	manager := NewManager(configDir, "test-version")
	if err := manager.Reset(context.Background()); err != nil {
		t.Fatal(err)
	}
	entries, err := os.ReadDir(dataDir)
	if err != nil {
		t.Fatal(err)
	}
	if len(entries) != 2 || entries[0].Name() != maintenance.StoreLockName || entries[1].Name() != ownershipMarker {
		t.Fatalf("reset absent runtime entries = %#v", entries)
	}
	if err := validateDataDirOwnership(dataDir); err != nil {
		t.Fatal(err)
	}
}

func TestRestoreReinstatesRuntimeAndCreatesRollbackSnapshot(t *testing.T) {
	root := t.TempDir()
	configDir := filepath.Join(root, ".a2a")
	dataDir := filepath.Join(root, "runtime")
	if err := os.MkdirAll(dataDir, 0700); err != nil {
		t.Fatal(err)
	}
	if err := os.MkdirAll(configDir, 0700); err != nil {
		t.Fatal(err)
	}
	configBytes := []byte(fmt.Sprintf("agents: {}\nstream:\n  data_dir: %s\n", dataDir))
	if err := os.WriteFile(filepath.Join(configDir, "config.yaml"), configBytes, 0600); err != nil {
		t.Fatal(err)
	}
	db, err := state.Open(filepath.Join(dataDir, "state.db"))
	if err != nil {
		t.Fatal(err)
	}
	if _, err := db.RegisterParticipant(model.Participant{
		Name: "before", Kind: model.KindHuman,
	}); err != nil {
		t.Fatal(err)
	}
	if err := db.Close(); err != nil {
		t.Fatal(err)
	}

	manager := NewManager(configDir, "test-version")
	saved, err := manager.Create(context.Background(), "known")
	if err != nil {
		t.Fatal(err)
	}
	db, err = state.Open(filepath.Join(dataDir, "state.db"))
	if err != nil {
		t.Fatal(err)
	}
	if err := db.DeleteParticipant("before"); err != nil {
		t.Fatal(err)
	}
	if _, err := db.RegisterParticipant(model.Participant{
		Name: "after", Kind: model.KindHuman,
	}); err != nil {
		t.Fatal(err)
	}
	if err := db.Close(); err != nil {
		t.Fatal(err)
	}

	result, err := manager.Restore(context.Background(), saved.ID)
	if err != nil {
		t.Fatal(err)
	}
	if result.RestoredID != saved.ID || result.RollbackID == "" {
		t.Fatalf("restore result = %#v", result)
	}
	db, err = state.Open(filepath.Join(dataDir, "state.db"))
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()
	if _, err := db.GetParticipantByName("before"); err != nil {
		t.Fatalf("restored participant missing: %v", err)
	}
	if _, err := db.GetParticipantByName("after"); err == nil {
		t.Fatal("post-snapshot participant survived restore")
	}
}

func TestCreateRejectsSymlinkedDataDirectory(t *testing.T) {
	root := t.TempDir()
	configDir := filepath.Join(root, ".a2a")
	actualDataDir := filepath.Join(root, "actual-runtime")
	configuredDataDir := filepath.Join(root, "runtime-link")
	if err := os.MkdirAll(actualDataDir, 0700); err != nil {
		t.Fatal(err)
	}
	if err := os.Symlink(actualDataDir, configuredDataDir); err != nil {
		t.Fatal(err)
	}
	if err := os.MkdirAll(configDir, 0700); err != nil {
		t.Fatal(err)
	}
	configBytes := []byte(fmt.Sprintf(
		"agents: {}\nstream:\n  data_dir: %s\n", configuredDataDir,
	))
	if err := os.WriteFile(filepath.Join(configDir, "config.yaml"), configBytes, 0600); err != nil {
		t.Fatal(err)
	}

	manager := NewManager(configDir, "test-version")
	if _, err := manager.Create(context.Background(), "symlinked"); err == nil {
		t.Fatal("Create accepted a symlinked data directory")
	}
}

func TestPreparePathsRejectsDangerousDirectoryBeforeMutation(t *testing.T) {
	home := t.TempDir()
	if err := os.Chmod(home, 0755); err != nil {
		t.Fatal(err)
	}
	configDir := filepath.Join(home, ".a2a")
	if err := os.Mkdir(configDir, 0700); err != nil {
		t.Fatal(err)
	}

	manager := NewManager(configDir, "test-version")
	if _, _, err := manager.preparePaths(home); err == nil {
		t.Fatal("preparePaths accepted the home directory")
	}
	info, err := os.Stat(home)
	if err != nil {
		t.Fatal(err)
	}
	if got := info.Mode().Perm(); got != 0755 {
		t.Fatalf("dangerous path mode changed to %o before rejection", got)
	}
}

func TestOwnershipMarkerIsWrittenOnlyToStagedData(t *testing.T) {
	dataDir := t.TempDir()
	if err := validateDataDirOwnership(dataDir); err != nil {
		t.Fatal(err)
	}
	if _, err := os.Stat(filepath.Join(dataDir, ownershipMarker)); !os.IsNotExist(err) {
		t.Fatalf("ownership validation mutated live data: %v", err)
	}
	stage := t.TempDir()
	if err := ensureOwnershipMarker(stage, dataDir); err != nil {
		t.Fatal(err)
	}
	markerPath := filepath.Join(stage, ownershipMarker)
	marker, err := os.ReadFile(markerPath)
	if err != nil {
		t.Fatal(err)
	}
	if !bytes.Contains(marker, []byte(dataDir)) {
		t.Fatalf("ownership marker does not identify %s: %s", dataDir, marker)
	}
	if err := os.WriteFile(markerPath, []byte("a2a-runtime-v1\n"), 0600); err != nil {
		t.Fatal(err)
	}
	if err := ensureOwnershipMarker(stage, dataDir); err == nil ||
		!strings.Contains(err.Error(), "invalid ownership marker") {
		t.Fatalf("ensureOwnershipMarker error = %v, want invalid ownership marker", err)
	}
}

func TestCreateRefusesInsufficientSpaceBeforeStaging(t *testing.T) {
	root := t.TempDir()
	configDir := filepath.Join(root, ".a2a")
	dataDir := filepath.Join(root, "runtime")
	if err := os.MkdirAll(dataDir, 0700); err != nil {
		t.Fatal(err)
	}
	if err := os.MkdirAll(configDir, 0700); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(dataDir, "state.db"), []byte("runtime"), 0600); err != nil {
		t.Fatal(err)
	}
	if err := os.Chmod(dataDir, 0755); err != nil {
		t.Fatal(err)
	}
	configBytes := []byte(fmt.Sprintf("agents: {}\nstream:\n  data_dir: %s\n", dataDir))
	if err := os.WriteFile(filepath.Join(configDir, "config.yaml"), configBytes, 0600); err != nil {
		t.Fatal(err)
	}

	manager := NewManager(configDir, "test-version")
	manager.availableBytes = func(string) (uint64, error) { return 0, nil }
	if _, err := manager.Create(context.Background(), "no-space"); err == nil ||
		!strings.Contains(err.Error(), "insufficient free space") {
		t.Fatalf("Create error = %v, want insufficient free space", err)
	}
	entries, err := os.ReadDir(filepath.Join(configDir, "backups"))
	if err != nil {
		t.Fatal(err)
	}
	for _, entry := range entries {
		if strings.HasPrefix(entry.Name(), ".snapshot-stage-") {
			t.Fatalf("Create staged data despite failed space preflight: %s", entry.Name())
		}
	}
	for _, name := range []string{ownershipMarker, "memory.db.lock"} {
		if _, err := os.Stat(filepath.Join(dataDir, name)); !os.IsNotExist(err) {
			t.Fatalf("Create left live %s after failed preflight: %v", name, err)
		}
	}
	if info, err := os.Stat(dataDir); err != nil {
		t.Fatal(err)
	} else if got := info.Mode().Perm(); got != 0755 {
		t.Fatalf("Create changed live data mode to %o before failing", got)
	}
}

func TestCreateExcludesMemoryLockFile(t *testing.T) {
	root := t.TempDir()
	configDir := filepath.Join(root, ".a2a")
	dataDir := filepath.Join(root, "runtime")
	if err := os.MkdirAll(dataDir, 0700); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(dataDir, "memory.db.lock"), nil, 0600); err != nil {
		t.Fatal(err)
	}
	if err := os.MkdirAll(configDir, 0700); err != nil {
		t.Fatal(err)
	}
	configBytes := []byte(fmt.Sprintf("agents: {}\nstream:\n  data_dir: %s\n", dataDir))
	if err := os.WriteFile(filepath.Join(configDir, "config.yaml"), configBytes, 0600); err != nil {
		t.Fatal(err)
	}

	manager := NewManager(configDir, "test-version")
	saved, err := manager.Create(context.Background(), "without-lock")
	if err != nil {
		t.Fatal(err)
	}
	lockPath := filepath.Join(configDir, "backups", saved.ID, "data", "memory.db.lock")
	if _, err := os.Stat(lockPath); !os.IsNotExist(err) {
		t.Fatalf("snapshot contains memory lock: %v", err)
	}
}

func TestCreateRefusesWhileLegacyMemoryStoreIsOpen(t *testing.T) {
	root := t.TempDir()
	configDir := filepath.Join(root, ".a2a")
	dataDir := filepath.Join(root, "runtime")
	if err := os.MkdirAll(configDir, 0700); err != nil {
		t.Fatal(err)
	}
	if err := os.MkdirAll(dataDir, 0700); err != nil {
		t.Fatal(err)
	}
	configBytes := []byte(fmt.Sprintf("agents: {}\nstream:\n  data_dir: %s\n", dataDir))
	if err := os.WriteFile(filepath.Join(configDir, "config.yaml"), configBytes, 0600); err != nil {
		t.Fatal(err)
	}
	store, err := memory.Open(dataDir, memory.Options{})
	if err != nil {
		t.Fatal(err)
	}
	defer store.Close()

	manager := NewManager(configDir, "test-version")
	if _, err := manager.Create(context.Background(), "while-open"); err == nil {
		t.Fatal("Create succeeded while a legacy memory store held its shared lock")
	}
}

func TestListRefusesWhenLegacyDaemonPIDIsLive(t *testing.T) {
	configDir := t.TempDir()
	child := exec.Command("sh", "-c", "read _")
	stdin, err := child.StdinPipe()
	if err != nil {
		t.Fatal(err)
	}
	if err := child.Start(); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() {
		_ = stdin.Close()
		_ = child.Wait()
	})
	if err := os.WriteFile(
		filepath.Join(configDir, "daemon.pid"),
		[]byte(fmt.Sprintf("%d", child.Process.Pid)),
		0600,
	); err != nil {
		t.Fatal(err)
	}

	manager := NewManager(configDir, "test-version")
	if _, err := manager.List(context.Background()); err == nil {
		t.Fatal("List succeeded while a legacy daemon PID was live")
	}
}

func TestLegacyDaemonCheckRefusesNonLocalURLWithoutDeletingIt(t *testing.T) {
	configDir := t.TempDir()
	urlPath := filepath.Join(configDir, "daemon.url")
	if err := os.WriteFile(urlPath, []byte("nats://example.com:4222\n"), 0600); err != nil {
		t.Fatal(err)
	}

	err := ensureLegacyDaemonStopped(configDir)
	if err == nil || !strings.Contains(err.Error(), "cannot prove daemon is stopped") {
		t.Fatalf("ensureLegacyDaemonStopped error = %v", err)
	}
	if _, statErr := os.Stat(urlPath); statErr != nil {
		t.Fatalf("daemon URL was removed despite inconclusive probe: %v", statErr)
	}
}

func TestLegacyDaemonCheckRefusesLiveLocalURL(t *testing.T) {
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	defer listener.Close()
	configDir := t.TempDir()
	urlPath := filepath.Join(configDir, "daemon.url")
	rawURL := "nats://" + listener.Addr().String()
	if err := os.WriteFile(urlPath, []byte(rawURL+"\n"), 0600); err != nil {
		t.Fatal(err)
	}

	err = ensureLegacyDaemonStopped(configDir)
	if err == nil || !strings.Contains(err.Error(), "runtime is in use") {
		t.Fatalf("ensureLegacyDaemonStopped error = %v", err)
	}
	if _, statErr := os.Stat(urlPath); statErr != nil {
		t.Fatalf("live daemon URL was removed: %v", statErr)
	}
}

func TestRestoreRefusesConfigMismatchBeforeCreatingRollback(t *testing.T) {
	root := t.TempDir()
	configDir := filepath.Join(root, ".a2a")
	dataDir := filepath.Join(root, "runtime")
	if err := os.MkdirAll(dataDir, 0700); err != nil {
		t.Fatal(err)
	}
	if err := os.MkdirAll(configDir, 0700); err != nil {
		t.Fatal(err)
	}
	configPath := filepath.Join(configDir, "config.yaml")
	configBytes := []byte(fmt.Sprintf("agents: {}\nstream:\n  data_dir: %s\n", dataDir))
	if err := os.WriteFile(configPath, configBytes, 0600); err != nil {
		t.Fatal(err)
	}
	manager := NewManager(configDir, "test-version")
	saved, err := manager.Create(context.Background(), "config-gate")
	if err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(configPath, append(configBytes, '\n'), 0600); err != nil {
		t.Fatal(err)
	}

	_, err = manager.Restore(context.Background(), saved.ID)
	if err == nil || !strings.Contains(err.Error(), filepath.Join("backups", saved.ID, "config.yaml")) {
		t.Fatalf("restore config mismatch error = %v", err)
	}
	snapshots, err := manager.List(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	if len(snapshots) != 1 {
		t.Fatalf("config mismatch created rollback snapshots: %#v", snapshots)
	}
}

func TestRestoreRefusesInsufficientSpaceBeforeCreatingRollback(t *testing.T) {
	root := t.TempDir()
	configDir := filepath.Join(root, ".a2a")
	dataDir := filepath.Join(root, "runtime")
	if err := os.MkdirAll(configDir, 0700); err != nil {
		t.Fatal(err)
	}
	if err := os.MkdirAll(dataDir, 0700); err != nil {
		t.Fatal(err)
	}
	configBytes := []byte(fmt.Sprintf("agents: {}\nstream:\n  data_dir: %s\n", dataDir))
	if err := os.WriteFile(filepath.Join(configDir, "config.yaml"), configBytes, 0600); err != nil {
		t.Fatal(err)
	}
	db, err := state.Open(filepath.Join(dataDir, "state.db"))
	if err != nil {
		t.Fatal(err)
	}
	if err := db.Close(); err != nil {
		t.Fatal(err)
	}

	manager := NewManager(configDir, "test-version")
	saved, err := manager.Create(context.Background(), "space-gate")
	if err != nil {
		t.Fatal(err)
	}
	if err := os.Chmod(dataDir, 0755); err != nil {
		t.Fatal(err)
	}
	manager.availableBytes = func(string) (uint64, error) { return 0, nil }
	if _, err := manager.Restore(context.Background(), saved.ID); err == nil ||
		!strings.Contains(err.Error(), "insufficient free space") {
		t.Fatalf("Restore error = %v, want insufficient free space", err)
	}
	snapshots, err := manager.List(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	if len(snapshots) != 1 {
		t.Fatalf("space preflight created rollback snapshots: %#v", snapshots)
	}
	for _, name := range []string{ownershipMarker, "memory.db.lock"} {
		if _, err := os.Stat(filepath.Join(dataDir, name)); !os.IsNotExist(err) {
			t.Fatalf("Restore left live %s after failed preflight: %v", name, err)
		}
	}
	if info, err := os.Stat(dataDir); err != nil {
		t.Fatal(err)
	} else if got := info.Mode().Perm(); got != 0755 {
		t.Fatalf("Restore changed live data mode to %o before failing", got)
	}
}

func TestRestoreRejectsHardLinkedSnapshotBeforeRollback(t *testing.T) {
	root := t.TempDir()
	configDir := filepath.Join(root, ".a2a")
	dataDir := filepath.Join(root, "runtime")
	if err := os.MkdirAll(configDir, 0700); err != nil {
		t.Fatal(err)
	}
	configBytes := []byte(fmt.Sprintf("agents: {}\nstream:\n  data_dir: %s\n", dataDir))
	if err := os.WriteFile(filepath.Join(configDir, "config.yaml"), configBytes, 0600); err != nil {
		t.Fatal(err)
	}
	manager := NewManager(configDir, "test-version")
	saved, err := manager.Create(context.Background(), "hard-link")
	if err != nil {
		t.Fatal(err)
	}
	markerPath := filepath.Join(
		configDir, "backups", saved.ID, "data", ownershipMarker,
	)
	if err := os.Link(markerPath, filepath.Join(configDir, "linked-marker")); err != nil {
		t.Fatal(err)
	}

	if _, err := manager.Restore(context.Background(), saved.ID); err == nil ||
		!strings.Contains(err.Error(), "hard-linked snapshot file") {
		t.Fatalf("Restore error = %v, want hard-link refusal", err)
	}
	snapshots, err := manager.List(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	if len(snapshots) != 1 {
		t.Fatalf("hard-link refusal created rollback snapshots: %#v", snapshots)
	}
}

func TestCreateRefusesSnapshotIDCollision(t *testing.T) {
	root := t.TempDir()
	configDir := filepath.Join(root, ".a2a")
	dataDir := filepath.Join(root, "runtime")
	if err := os.MkdirAll(configDir, 0700); err != nil {
		t.Fatal(err)
	}
	configBytes := []byte(fmt.Sprintf("agents: {}\nstream:\n  data_dir: %s\n", dataDir))
	if err := os.WriteFile(filepath.Join(configDir, "config.yaml"), configBytes, 0600); err != nil {
		t.Fatal(err)
	}
	manager := NewManager(configDir, "test-version")
	fixedTime := time.Date(2026, 7, 27, 12, 0, 0, 0, time.UTC)
	manager.now = func() time.Time { return fixedTime }
	first, err := manager.Create(context.Background(), "collision")
	if err != nil {
		t.Fatal(err)
	}
	if _, err := manager.Create(context.Background(), "collision"); err == nil ||
		!strings.Contains(err.Error(), "already exists") {
		t.Fatalf("second Create error = %v, want collision refusal", err)
	}
	snapshots, err := manager.List(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	if len(snapshots) != 1 || snapshots[0].ID != first.ID {
		t.Fatalf("collision changed published snapshots: %#v", snapshots)
	}
}

func TestCreateTightensBackupDirectoryPermissions(t *testing.T) {
	root := t.TempDir()
	configDir := filepath.Join(root, ".a2a")
	dataDir := filepath.Join(root, "runtime")
	backupDir := filepath.Join(configDir, "backups")
	if err := os.MkdirAll(dataDir, 0755); err != nil {
		t.Fatal(err)
	}
	if err := os.MkdirAll(backupDir, 0755); err != nil {
		t.Fatal(err)
	}
	configBytes := []byte(fmt.Sprintf("agents: {}\nstream:\n  data_dir: %s\n", dataDir))
	if err := os.WriteFile(filepath.Join(configDir, "config.yaml"), configBytes, 0644); err != nil {
		t.Fatal(err)
	}

	manager := NewManager(configDir, "test-version")
	if _, err := manager.Create(context.Background(), "permissions"); err != nil {
		t.Fatal(err)
	}
	info, err := os.Stat(backupDir)
	if err != nil {
		t.Fatal(err)
	}
	if got := info.Mode().Perm(); got != 0700 {
		t.Fatalf("backup directory mode = %o, want 700", got)
	}
}
