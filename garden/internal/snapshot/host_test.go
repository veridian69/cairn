package snapshot

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/veridian69/cairn/garden/internal/maintenance"
	"github.com/veridian69/cairn/garden/internal/model"
	"github.com/veridian69/cairn/garden/internal/state"
)

func hostFixture(t *testing.T) (string, string, string) {
	t.Helper()
	root := t.TempDir()
	data := filepath.Join(root, "data")
	lease := filepath.Join(root, "run")
	config := filepath.Join(root, "host.json")
	// Same nesting and retention fields as garden_native.garden_prepare and
	// DockerAdapter.garden_prepare. Snapshot leaves gateway settings opaque.
	raw := fmt.Sprintf(`{"gateway":{"listen":"0.0.0.0:9443","data_dir":%q,"daemon_url_file":%q,"tls_cert_file":"/etc/garden/tls/server.crt","tls_key_file":"/etc/garden/tls/server.key","principals":{"00000000-0000-4000-8000-000000000001":"val"},"auth":{"endpoint":"http://127.0.0.1:8100/memory/v1/diagnose","instance_id":"00000000-0000-4000-8000-000000000003","scope":{"realm":"local","segments":[]},"classification":"internal"}},"stream":{"max_age":"168h","max_bytes":1073741824}}`, data, filepath.Join(lease, "daemon.url"))
	if err := os.WriteFile(config, []byte(raw), 0600); err != nil {
		t.Fatal(err)
	}
	return config, data, lease
}

func TestHostSnapshotRoundTrip(t *testing.T) {
	for _, custom := range []bool{false, true} {
		t.Run(fmt.Sprint("custom-backup-", custom), func(t *testing.T) {
			config, data, _ := hostFixture(t)
			backups := ""
			if custom {
				backups = filepath.Join(t.TempDir(), "exports")
			}
			manager, err := NewHostManager(config, backups, "test-version")
			if err != nil {
				t.Fatal(err)
			}
			if backups == "" {
				backups = filepath.Join(filepath.Dir(config), "backups")
			}
			if err := os.Mkdir(data, 0700); err != nil {
				t.Fatal(err)
			}
			db, err := state.Open(filepath.Join(data, "state.db"))
			if err != nil {
				t.Fatal(err)
			}
			if _, err = db.RegisterParticipant(model.Participant{Name: "before", Kind: model.KindHuman}); err != nil {
				t.Fatal(err)
			}
			if err = db.Close(); err != nil {
				t.Fatal(err)
			}
			saved, err := manager.Create(context.Background(), "installer")
			if err != nil {
				t.Fatal(err)
			}
			raw, err := os.ReadFile(filepath.Join(backups, saved.ID, "manifest.json"))
			if err != nil {
				t.Fatal(err)
			}
			var doc manifest
			if err = json.Unmarshal(raw, &doc); err != nil {
				t.Fatal(err)
			}
			if doc.ConfigFile != "host.json" || doc.CanonicalDataDir != data {
				t.Fatalf("manifest = %#v", doc)
			}
			original, _ := os.ReadFile(config)
			archived, err := os.ReadFile(filepath.Join(backups, saved.ID, "host.json"))
			if err != nil || !bytes.Equal(original, archived) {
				t.Fatalf("host config changed: %v", err)
			}
			if _, err = os.Stat(filepath.Join(backups, saved.ID, "config.yaml")); !os.IsNotExist(err) {
				t.Fatalf("host has daemon config: %v", err)
			}
			// One backup directory can list both layouts without selecting either one.
			daemon := NewManagerWithBackupDir(filepath.Join(t.TempDir(), "daemon"), backups, "test-version")
			listed, err := daemon.List(context.Background())
			if err != nil || len(listed) != 1 || listed[0].ID != saved.ID {
				t.Fatalf("List = %#v, %v", listed, err)
			}
			listed, err = manager.List(context.Background())
			if err != nil || len(listed) != 1 {
				t.Fatalf("host List = %#v, %v", listed, err)
			}
			db, err = state.Open(filepath.Join(data, "state.db"))
			if err != nil {
				t.Fatal(err)
			}
			if err = db.DeleteParticipant("before"); err != nil {
				t.Fatal(err)
			}
			if _, err = db.RegisterParticipant(model.Participant{Name: "after", Kind: model.KindHuman}); err != nil {
				t.Fatal(err)
			}
			if err = db.Close(); err != nil {
				t.Fatal(err)
			}
			result, err := manager.Restore(context.Background(), saved.ID)
			if err != nil {
				t.Fatal(err)
			}
			if result.RestoredID != saved.ID || result.RollbackID == "" {
				t.Fatalf("restore = %#v", result)
			}
			db, err = state.Open(filepath.Join(data, "state.db"))
			if err != nil {
				t.Fatal(err)
			}
			defer db.Close()
			if _, err = db.GetParticipantByName("before"); err != nil {
				t.Fatal(err)
			}
			if _, err = db.GetParticipantByName("after"); err == nil {
				t.Fatal("post-snapshot participant survived")
			}
		})
	}
}

func TestHostSnapshotUsesHostLease(t *testing.T) {
	config, _, leaseDir := hostFixture(t)
	manager, err := NewHostManager(config, "", "test-version")
	if err != nil {
		t.Fatal(err)
	}
	lease, err := maintenance.Acquire(leaseDir, maintenance.Exclusive)
	if err != nil {
		t.Fatal(err)
	}
	defer lease.Close()
	for name, call := range map[string]func() error{
		"create":  func() error { _, err := manager.Create(context.Background(), "blocked"); return err },
		"list":    func() error { _, err := manager.List(context.Background()); return err },
		"restore": func() error { _, err := manager.Restore(context.Background(), "20060102T150405Z-blocked"); return err },
	} {
		t.Run(name, func(t *testing.T) {
			if err := call(); err == nil || !strings.Contains(err.Error(), "runtime is in use") {
				t.Fatalf("error = %v", err)
			}
		})
	}
	if _, err := os.Stat(filepath.Join(filepath.Dir(config), "backups")); !os.IsNotExist(err) {
		t.Fatalf("blocked operation wrote backups: %v", err)
	}
}

func TestHostSnapshotRejectsInvalidPaths(t *testing.T) {
	for _, raw := range []string{`{"gateway":{"data_dir":"relative","daemon_url_file":"/tmp/run/daemon.url"}}`, `{"gateway":{"data_dir":"/tmp/data"}}`} {
		root := t.TempDir()
		path := filepath.Join(root, "host.json")
		if err := os.WriteFile(path, []byte(raw), 0600); err != nil {
			t.Fatal(err)
		}
		if _, err := NewHostManager(path, "", "test-version"); err == nil {
			t.Fatalf("accepted %s", raw)
		}
		entries, _ := os.ReadDir(root)
		if len(entries) != 1 {
			t.Fatalf("invalid configuration wrote files: %#v", entries)
		}
	}
}

func TestDaemonManifestBytesUnchanged(t *testing.T) {
	// This is the exact manifest field sequence and encoding before host support.
	type daemonManifest struct {
		FormatVersion       int        `json:"format_version"`
		ID                  string     `json:"id"`
		CreatedAt           time.Time  `json:"created_at"`
		BinaryVersion       string     `json:"binary_version"`
		CanonicalDataDir    string     `json:"canonical_data_dir"`
		ConfigSHA256        string     `json:"config_sha256"`
		StateSchemaVersion  int        `json:"state_schema_version"`
		MemorySchemaVersion int        `json:"memory_schema_version"`
		TotalBytes          int64      `json:"total_bytes"`
		Files               []fileInfo `json:"files"`
	}
	root := t.TempDir()
	config := filepath.Join(root, "config")
	data := filepath.Join(root, "data")
	if err := os.Mkdir(config, 0700); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(config, "config.yaml"), []byte(fmt.Sprintf("agents: {}\nstream:\n  data_dir: %s\n", data)), 0600); err != nil {
		t.Fatal(err)
	}
	manager := NewManager(config, "test-version")
	saved, err := manager.Create(context.Background(), "legacy")
	if err != nil {
		t.Fatal(err)
	}
	raw, err := os.ReadFile(filepath.Join(config, "backups", saved.ID, "manifest.json"))
	if err != nil {
		t.Fatal(err)
	}
	var old daemonManifest
	if err := json.Unmarshal(raw, &old); err != nil {
		t.Fatal(err)
	}
	want, err := json.MarshalIndent(old, "", "  ")
	if err != nil {
		t.Fatal(err)
	}
	want = append(want, '\n')
	if !bytes.Equal(raw, want) {
		t.Fatalf("daemon manifest bytes changed:\ngot %s\nwant %s", raw, want)
	}
}
