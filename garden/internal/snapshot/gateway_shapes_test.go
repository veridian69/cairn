package snapshot

import (
	"context"
	"database/sql"
	"path/filepath"
	"strings"
	"testing"
)

// oldInboxSQL is the garden_inbox create text the pre-upgrade gateway stored, verbatim.
const oldInboxSQL = `CREATE TABLE garden_inbox (
 principal TEXT PRIMARY KEY, name TEXT NOT NULL UNIQUE, participant TEXT NOT NULL UNIQUE,
 generation TEXT NOT NULL, cursor INTEGER NOT NULL, pending_seq INTEGER NOT NULL DEFAULT 0,
 pending_id TEXT NOT NULL DEFAULT '', receipt TEXT NOT NULL DEFAULT '', last_receipt TEXT NOT NULL DEFAULT '',
 consumer TEXT NOT NULL DEFAULT '', lease_until INTEGER NOT NULL DEFAULT 0
)`

// dropLoopGuardUpgrade rebuilds a current gateway database in the pre-upgrade shape: the
// garden_inbox table is recreated with the exact text the old gateway stored, the rows are
// kept, and the loop-guard tables are removed.
func dropLoopGuardUpgrade(t *testing.T, dataDir string) {
	t.Helper()
	raw, err := sql.Open("sqlite", filepath.Join(dataDir, "state.db"))
	if err != nil {
		t.Fatal(err)
	}
	defer raw.Close()
	const columns = "principal,name,participant,generation,cursor,pending_seq,pending_id,receipt,last_receipt,consumer,lease_until"
	for _, stmt := range []string{
		"CREATE TABLE garden_inbox_rows AS SELECT " + columns + " FROM garden_inbox",
		"DROP TABLE garden_inbox",
		oldInboxSQL,
		"INSERT INTO garden_inbox(" + columns + ") SELECT " + columns + " FROM garden_inbox_rows",
		"DROP TABLE garden_inbox_rows",
		"DROP TABLE garden_message_depth",
		"DROP TABLE garden_attention",
	} {
		if _, err := raw.Exec(stmt); err != nil {
			t.Fatalf("%.60s: %v", stmt, err)
		}
	}
}

// inboxSQL returns the stored CREATE text of garden_inbox, which SQLite rewrites on ALTER.
func inboxSQL(t *testing.T, dataDir string) string {
	t.Helper()
	raw, err := sql.Open("sqlite", filepath.Join(dataDir, "state.db"))
	if err != nil {
		t.Fatal(err)
	}
	defer raw.Close()
	var text string
	if err := raw.QueryRow("SELECT sql FROM sqlite_master WHERE type='table' AND name='garden_inbox'").Scan(&text); err != nil {
		t.Fatal(err)
	}
	return text
}

// TestGatewaySnapshotShapesRoundTrip snapshots and restores each database shape the
// gateway can hold: an old database, an old database migrated by the new gateway, and
// a fresh database. Each must pass the staged-state validator through real Create and Restore.
func TestGatewaySnapshotShapesRoundTrip(t *testing.T) {
	cases := []struct {
		name  string
		shape func(t *testing.T, root, dataDir string)
		// hasDeliverable says whether the stored garden_inbox SQL carries the column.
		hasDeliverable bool
	}{
		{name: "fresh", shape: func(t *testing.T, root, dataDir string) {
			seedGatewayRuntime(t, root, dataDir)
		}, hasDeliverable: true},
		{name: "old", shape: func(t *testing.T, root, dataDir string) {
			seedGatewayRuntime(t, root, dataDir)
			dropLoopGuardUpgrade(t, dataDir)
		}, hasDeliverable: false},
		{name: "migrated", shape: func(t *testing.T, root, dataDir string) {
			seedGatewayRuntime(t, root, dataDir)
			dropLoopGuardUpgrade(t, dataDir)
			// Running the new gateway over the old database performs the migration.
			seedGatewayRuntime(t, root, dataDir)
		}, hasDeliverable: true},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			root := t.TempDir()
			configDir := filepath.Join(root, ".garden")
			dataDir := filepath.Join(root, "runtime")
			writeGatewayConfig(t, configDir, dataDir)
			tc.shape(t, root, dataDir)
			if got := strings.Contains(inboxSQL(t, dataDir), "deliverable"); got != tc.hasDeliverable {
				t.Fatalf("garden_inbox has deliverable=%v, want %v: %s", got, tc.hasDeliverable, inboxSQL(t, dataDir))
			}
			before := gatewayRows(t, dataDir)
			manager := NewManager(configDir, "test-version")
			created, err := manager.Create(context.Background(), "gateway-"+tc.name)
			if err != nil {
				t.Fatalf("Create %s gateway runtime: %v", tc.name, err)
			}
			if err := manager.Reset(context.Background()); err != nil {
				t.Fatal(err)
			}
			if _, err := manager.Restore(context.Background(), created.ID); err != nil {
				t.Fatalf("Restore %s gateway snapshot: %v", tc.name, err)
			}
			if after := gatewayRows(t, dataDir); after != before {
				t.Fatalf("%s gateway rows changed across restore:\nbefore:\n%s\nafter:\n%s", tc.name, before, after)
			}
		})
	}
}

func TestValidateStagedStateAcceptsTwoTableGatewayAndRejectsHalfLoopGuard(t *testing.T) {
	root := t.TempDir()
	dataDir := filepath.Join(root, "runtime")
	seedGatewayRuntime(t, root, dataDir)
	dropLoopGuardUpgrade(t, dataDir)
	path := filepath.Join(dataDir, "state.db")
	if version, err := validateSQLiteCopy(path, "state"); err != nil || version != 1 {
		t.Fatalf("two-table gateway snapshot = %d, %v; want accepted", version, err)
	}
	raw, err := sql.Open("sqlite", path)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := raw.Exec("CREATE TABLE garden_message_depth (message_id TEXT PRIMARY KEY, depth INTEGER NOT NULL)"); err != nil {
		t.Fatal(err)
	}
	if err := raw.Close(); err != nil {
		t.Fatal(err)
	}
	if _, err := validateSQLiteCopy(path, "state"); err == nil || !strings.Contains(err.Error(), "unknown schema") {
		t.Fatalf("half loop-guard gateway = %v, want rejection", err)
	}
}
