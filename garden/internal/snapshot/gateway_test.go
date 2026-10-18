package snapshot

import (
	"context"
	"database/sql"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"syscall"
	"testing"

	"github.com/veridian69/cairn/garden/internal/garden"
	"github.com/veridian69/cairn/garden/internal/gardenauth"
	"github.com/veridian69/cairn/garden/internal/state"
	"github.com/veridian69/cairn/garden/internal/transport"
)

const gatewayPrincipal = "11111111-1111-4111-8111-111111111111"
const gatewayInstance = "33333333-3333-4333-8333-333333333333"

// seedGatewayRuntime starts a real daemon store and gateway in dataDir, then
// stops both, leaving the stopped runtime a snapshot must accept.
func seedGatewayRuntime(t *testing.T, root, dataDir string) {
	t.Helper()
	ctx := context.Background()
	if err := os.MkdirAll(dataDir, 0700); err != nil {
		t.Fatal(err)
	}
	server, err := transport.NewServer(dataDir)
	if err != nil {
		t.Fatal(err)
	}
	stream, err := transport.NewManagedStream(server.ClientURL(), transport.StreamOptions{})
	if err != nil {
		server.Stop()
		t.Fatal(err)
	}
	urlFile := filepath.Join(root, "daemon.url")
	if err := os.WriteFile(urlFile, []byte(server.ClientURL()), 0600); err != nil {
		stream.Close()
		server.Stop()
		t.Fatal(err)
	}
	gateway, err := garden.New(ctx, garden.Config{
		Listen: "127.0.0.1:0", DataDir: dataDir, DaemonURLFile: urlFile,
		Auth: gardenauth.Config{
			Endpoint: "http://127.0.0.1:9/memory/v1/diagnose", InstanceID: gatewayInstance,
			Scope:          gardenauth.Scope{Realm: "test", Segments: []gardenauth.Segment{}},
			Classification: "internal",
		},
		Principals: map[string]string{gatewayPrincipal: "alice"},
	})
	if err != nil {
		stream.Close()
		server.Stop()
		t.Fatal(err)
	}
	if err := gateway.Close(); err != nil {
		t.Fatal(err)
	}
	stream.Close()
	server.Stop()
	// Give the inbox a non-default delivery position so restore must keep it.
	raw, err := sql.Open("sqlite", filepath.Join(dataDir, "state.db"))
	if err != nil {
		t.Fatal(err)
	}
	if _, err := raw.Exec("UPDATE garden_inbox SET cursor=7, pending_seq=3, pending_id='msg-3'"); err != nil {
		t.Fatal(err)
	}
	if err := raw.Close(); err != nil {
		t.Fatal(err)
	}
}

// gatewayRows renders the gateway tables in a stable order for comparison.
func gatewayRows(t *testing.T, dataDir string) string {
	t.Helper()
	db, err := sql.Open("sqlite", filepath.Join(dataDir, "state.db")+"?_pragma=busy_timeout(5000)")
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()
	var out strings.Builder
	for _, query := range []string{
		"SELECT singleton, binding FROM garden_binding ORDER BY singleton",
		"SELECT principal, name, participant, generation, cursor, pending_seq, pending_id, receipt, last_receipt, consumer, lease_until FROM garden_inbox ORDER BY principal",
	} {
		rows, err := db.Query(query)
		if err != nil {
			t.Fatal(err)
		}
		columns, err := rows.Columns()
		if err != nil {
			t.Fatal(err)
		}
		for rows.Next() {
			values := make([]any, len(columns))
			pointers := make([]any, len(columns))
			for i := range values {
				pointers[i] = &values[i]
			}
			if err := rows.Scan(pointers...); err != nil {
				t.Fatal(err)
			}
			fmt.Fprintln(&out, values...)
		}
		if err := rows.Err(); err != nil {
			t.Fatal(err)
		}
		if err := rows.Close(); err != nil {
			t.Fatal(err)
		}
	}
	return out.String()
}

func writeGatewayConfig(t *testing.T, configDir, dataDir string) {
	t.Helper()
	if err := os.MkdirAll(configDir, 0700); err != nil {
		t.Fatal(err)
	}
	configBytes := []byte(fmt.Sprintf("agents: {}\nstream:\n  data_dir: %s\n", dataDir))
	if err := os.WriteFile(filepath.Join(configDir, "config.yaml"), configBytes, 0600); err != nil {
		t.Fatal(err)
	}
}

func TestGatewayStateRoundTripsThroughSnapshotAndRestore(t *testing.T) {
	root := t.TempDir()
	configDir := filepath.Join(root, ".garden")
	dataDir := filepath.Join(root, "runtime")
	writeGatewayConfig(t, configDir, dataDir)
	seedGatewayRuntime(t, root, dataDir)
	before := gatewayRows(t, dataDir)
	if !strings.Contains(before, "msg-3") {
		t.Fatalf("seeded inbox lacks the delivery cursor: %q", before)
	}
	// Host mode also leaves host.lock behind; it must not be snapshotted either.
	if err := os.WriteFile(filepath.Join(dataDir, "host.lock"), nil, 0600); err != nil {
		t.Fatal(err)
	}

	manager := NewManager(configDir, "test-version")
	created, err := manager.Create(context.Background(), "gateway")
	if err != nil {
		t.Fatalf("Create gateway runtime: %v", err)
	}
	for _, name := range []string{"garden.lock", "host.lock"} {
		if _, err := os.Stat(filepath.Join(configDir, "backups", created.ID, "data", name)); !os.IsNotExist(err) {
			t.Fatalf("snapshot copied gateway lock %s: %v", name, err)
		}
	}
	if err := manager.Reset(context.Background()); err != nil {
		t.Fatal(err)
	}
	if _, err := manager.Restore(context.Background(), created.ID); err != nil {
		t.Fatalf("Restore gateway snapshot: %v", err)
	}
	if after := gatewayRows(t, dataDir); after != before {
		t.Fatalf("gateway rows changed across restore:\nbefore:\n%s\nafter:\n%s", before, after)
	}
}

func TestGatewayLocksRefuseCreateRestoreAndReset(t *testing.T) {
	root := t.TempDir()
	configDir := filepath.Join(root, ".garden")
	dataDir := filepath.Join(root, "runtime")
	writeGatewayConfig(t, configDir, dataDir)
	seedGatewayRuntime(t, root, dataDir)
	manager := NewManager(configDir, "test-version")
	created, err := manager.Create(context.Background(), "before-lock")
	if err != nil {
		t.Fatal(err)
	}
	// A running gateway or host holds an exclusive flock on its lock file for its lifetime.
	for _, name := range []string{"garden.lock", "host.lock"} {
		lock, err := os.OpenFile(filepath.Join(dataDir, name), os.O_RDWR|os.O_CREATE, 0600)
		if err != nil {
			t.Fatal(err)
		}
		if err := syscall.Flock(int(lock.Fd()), syscall.LOCK_EX|syscall.LOCK_NB); err != nil {
			t.Fatal(err)
		}
		if _, err := manager.Create(context.Background(), "while-"+strings.TrimSuffix(name, ".lock")); err == nil ||
			!strings.Contains(err.Error(), "gateway lock") {
			t.Fatalf("Create error while %s held = %v", name, err)
		}
		if _, err := manager.Restore(context.Background(), created.ID); err == nil ||
			!strings.Contains(err.Error(), "gateway lock") {
			t.Fatalf("Restore error while %s held = %v", name, err)
		}
		if err := manager.Reset(context.Background()); err == nil ||
			!strings.Contains(err.Error(), "gateway lock") {
			t.Fatalf("Reset error while %s held = %v", name, err)
		}
		if err := syscall.Flock(int(lock.Fd()), syscall.LOCK_UN); err != nil {
			t.Fatal(err)
		}
		if err := lock.Close(); err != nil {
			t.Fatal(err)
		}
	}
	if _, err := manager.Create(context.Background(), "after-locks"); err != nil {
		t.Fatalf("Create after gateway locks released: %v", err)
	}
}

func TestValidateStagedStateRejectsUnknownGatewayShape(t *testing.T) {
	root := t.TempDir()
	dataDir := filepath.Join(root, "runtime")
	seedGatewayRuntime(t, root, dataDir)
	path := filepath.Join(dataDir, "state.db")
	raw, err := sql.Open("sqlite", path)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := raw.Exec("CREATE TABLE garden_extra (id TEXT)"); err != nil {
		t.Fatal(err)
	}
	if err := raw.Close(); err != nil {
		t.Fatal(err)
	}
	if _, err := validateSQLiteCopy(path, "state"); err == nil ||
		!strings.Contains(err.Error(), "unknown schema") {
		t.Fatalf("validateSQLiteCopy with extra table = %v, want unknown schema", err)
	}
}

func TestValidateStagedStateRejectsTamperedGatewaySchema(t *testing.T) {
	root := t.TempDir()
	dataDir := filepath.Join(root, "runtime")
	seedGatewayRuntime(t, root, dataDir)
	path := filepath.Join(dataDir, "state.db")
	raw, err := sql.Open("sqlite", path)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := raw.Exec("ALTER TABLE garden_inbox ADD COLUMN unexpected TEXT"); err != nil {
		t.Fatal(err)
	}
	if err := raw.Close(); err != nil {
		t.Fatal(err)
	}
	if _, err := validateSQLiteCopy(path, "state"); err == nil ||
		!strings.Contains(err.Error(), "unknown schema") {
		t.Fatalf("validateSQLiteCopy with tampered inbox = %v, want unknown schema", err)
	}
}

func TestValidateStagedStateRejectsPartialGatewaySchema(t *testing.T) {
	root := t.TempDir()
	dataDir := filepath.Join(root, "runtime")
	seedGatewayRuntime(t, root, dataDir)
	path := filepath.Join(dataDir, "state.db")
	raw, err := sql.Open("sqlite", path)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := raw.Exec("DROP TABLE garden_inbox"); err != nil {
		t.Fatal(err)
	}
	if err := raw.Close(); err != nil {
		t.Fatal(err)
	}
	if _, err := validateSQLiteCopy(path, "state"); err == nil ||
		!strings.Contains(err.Error(), "unknown schema") {
		t.Fatalf("validateSQLiteCopy with partial gateway schema = %v, want unknown schema", err)
	}
}

func TestDaemonOnlyStateStillValidatesWithoutGatewayTables(t *testing.T) {
	dataDir := t.TempDir()
	path := filepath.Join(dataDir, "state.db")
	db, err := state.Open(path)
	if err != nil {
		t.Fatal(err)
	}
	if err := db.Close(); err != nil {
		t.Fatal(err)
	}
	if version, err := validateSQLiteCopy(path, "state"); err != nil || version != 1 {
		t.Fatalf("validateSQLiteCopy daemon-only = %d, %v", version, err)
	}
}
