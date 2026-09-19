package snapshot

import (
	"context"
	"github.com/veridian69/cairn/a2a/internal/transport"
	"os"
	"path/filepath"
	"testing"
)

func TestStoreLockSurvivesMaintenanceAndExcludesLiveDaemon(t *testing.T) {
	root := t.TempDir()
	configDir := filepath.Join(root, "config")
	dataDir := filepath.Join(root, "runtime")
	if err := os.Mkdir(configDir, 0700); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(configDir, "config.yaml"), []byte("agents: {}\nstream:\n  data_dir: "+dataDir+"\n"), 0600); err != nil {
		t.Fatal(err)
	}
	seedRuntimeVersion(t, dataDir)
	manager := NewManager(configDir, "test")
	saved, err := manager.Create(context.Background(), "saved")
	if err != nil {
		t.Fatal(err)
	}
	if _, err := os.Stat(filepath.Join(configDir, "backups", saved.ID, "data", ".a2a-nats.lock")); !os.IsNotExist(err) {
		t.Fatalf("snapshot copied runtime lock: %v", err)
	}
	lockPath := filepath.Join(dataDir, ".a2a-nats.lock")
	before, err := os.Stat(lockPath)
	if err != nil {
		t.Fatal(err)
	}
	server, err := transport.NewServer(dataDir)
	if err != nil {
		t.Fatal(err)
	}
	defer server.Stop()
	if _, err := manager.Create(context.Background(), "live"); err == nil {
		t.Fatal("snapshot accepted live daemon")
	}
	if err := manager.Reset(context.Background()); err == nil {
		t.Fatal("reset accepted live daemon")
	}
	if _, err := manager.Restore(context.Background(), saved.ID); err == nil {
		t.Fatal("restore accepted live daemon")
	}
	server.Stop()
	if err := manager.Reset(context.Background()); err != nil {
		t.Fatal(err)
	}
	after, err := os.Stat(lockPath)
	if err != nil || !os.SameFile(before, after) {
		t.Fatalf("reset replaced store lock inode: %v", err)
	}
	if _, err := manager.Restore(context.Background(), saved.ID); err != nil {
		t.Fatal(err)
	}
	after, err = os.Stat(lockPath)
	if err != nil || !os.SameFile(before, after) {
		t.Fatalf("restore replaced store lock inode: %v", err)
	}
	resumed, err := transport.NewServer(dataDir)
	if err != nil {
		t.Fatal(err)
	}
	resumed.Stop()
}

func TestOwnershipRejectsInvalidStoreLockEvenWithMarker(t *testing.T) {
	for _, kind := range []string{"symlink", "contents", "permissions", "directory"} {
		t.Run(kind, func(t *testing.T) {
			dir := t.TempDir()
			if err := writeOwnershipMarker(dir, dir); err != nil {
				t.Fatal(err)
			}
			path := filepath.Join(dir, ".a2a-nats.lock")
			var err error
			switch kind {
			case "symlink":
				target := filepath.Join(t.TempDir(), "target")
				err = os.WriteFile(target, nil, 0600)
				if err == nil {
					err = os.Symlink(target, path)
				}
			case "contents":
				err = os.WriteFile(path, []byte("foreign"), 0600)
			case "permissions":
				err = os.WriteFile(path, nil, 0644)
			case "directory":
				err = os.Mkdir(path, 0700)
			}
			if err != nil {
				t.Fatal(err)
			}
			if err := validateDataDirOwnership(dir); err == nil {
				t.Fatal("accepted invalid store lock")
			}
		})
	}
}
