package snapshot

import (
	"context"
	"database/sql"
	"fmt"
	"os"
	"path/filepath"
	"strings"

	"github.com/veridian69/cairn/garden/internal/maintenance"
	"github.com/veridian69/cairn/garden/internal/transport"
	_ "modernc.org/sqlite"
)

func validateStagedData(ctx context.Context, dataDir string) (int, int, error) {
	if err := ctx.Err(); err != nil {
		return 0, 0, err
	}
	stateVersion, err := validateSQLiteCopy(filepath.Join(dataDir, "state.db"), "state")
	if err != nil {
		return 0, 0, err
	}
	memoryVersion, err := validateSQLiteCopy(filepath.Join(dataDir, "memory.db"), "memory")
	if err != nil {
		return 0, 0, err
	}
	server, err := transport.NewServer(dataDir)
	if err != nil {
		return 0, 0, fmt.Errorf("validating JetStream: %w", err)
	}
	server.Stop()
	// This is an unpublished private staging tree, never the live runtime.
	// NATS validation creates a temporary lock; remove only after obtaining its
	// exclusive lease, so no running daemon's lock can be silently discarded.
	stagedLease, err := maintenance.AcquireStore(dataDir)
	if err != nil {
		return 0, 0, err
	}
	defer stagedLease.Close()
	if err := os.Remove(filepath.Join(dataDir, maintenance.StoreLockName)); err != nil {
		return 0, 0, err
	}
	return stateVersion, memoryVersion, nil
}

func validateSQLiteCopy(path, kind string) (int, error) {
	if _, err := os.Stat(path); os.IsNotExist(err) {
		return 1, nil
	} else if err != nil {
		return 0, err
	}
	db, err := sql.Open("sqlite", path+"?_pragma=busy_timeout(5000)")
	if err != nil {
		return 0, fmt.Errorf("opening staged %s database: %w", kind, err)
	}
	if _, err := db.Exec("PRAGMA wal_checkpoint(TRUNCATE)"); err != nil {
		_ = db.Close()
		return 0, fmt.Errorf("checkpointing staged %s database: %w", kind, err)
	}
	var check string
	if err := db.QueryRow("PRAGMA quick_check").Scan(&check); err != nil {
		_ = db.Close()
		return 0, fmt.Errorf("checking staged %s database: %w", kind, err)
	}
	if check != "ok" {
		_ = db.Close()
		return 0, fmt.Errorf("staged %s database failed quick_check: %s", kind, check)
	}
	version, err := stagedSchemaVersion(db, kind)
	if err != nil {
		_ = db.Close()
		return 0, err
	}
	if err := db.Close(); err != nil {
		return 0, err
	}
	for _, suffix := range []string{"-wal", "-shm"} {
		if err := os.Remove(path + suffix); err != nil && !os.IsNotExist(err) {
			return 0, err
		}
	}
	return version, nil
}

func stagedSchemaVersion(db *sql.DB, kind string) (int, error) {
	switch kind {
	case "state":
		if err := validateStateSchemaV1(db); err != nil {
			return 0, fmt.Errorf("staged state database has an unknown schema")
		}
		return 1, nil
	case "memory":
		var version int
		if err := db.QueryRow("SELECT version FROM schema_version WHERE id = 1").Scan(&version); err != nil {
			return 0, fmt.Errorf("reading staged memory schema: %w", err)
		}
		if version != 1 {
			return 0, fmt.Errorf("unsupported staged memory schema version %d", version)
		}
		return version, nil
	default:
		return 0, fmt.Errorf("unknown database kind %q", kind)
	}
}

// gatewayBaseTables and gatewayLoopGuardTables group the gateway tables by the upgrade that created them.
var gatewayBaseTables = []string{"garden_binding", "garden_inbox"}
var gatewayLoopGuardTables = []string{"garden_message_depth", "garden_attention", "garden_attention_pair", "garden_attention_expiry", "garden_message_depth_seq"}

// gatewayIndexesV1 are the loop-guard indexes, created in the same upgrade as their table.
var gatewayIndexesV1 = map[string]string{
	"garden_attention_pair":    `CREATE INDEX garden_attention_pair ON garden_attention(pair_low, pair_high, attended_at)`,
	"garden_attention_expiry":  `CREATE INDEX garden_attention_expiry ON garden_attention(attended_at)`,
	"garden_message_depth_seq": `CREATE INDEX garden_message_depth_seq ON garden_message_depth(seq)`,
}

// gatewaySchemaV1 is the state.db schema the gateway adds in internal/garden/store.go.
// It is optional, because a daemon-only data directory never has it, but when present
// all of it must match one of the accepted shapes exactly. garden_inbox has three stored
// texts: the original create, the same table after the in-place deliverable migration
// (SQLite rewrites the stored SQL on ALTER TABLE ADD COLUMN, placing the new column after a
// newline and before the closing parenthesis), and a fresh create that includes the column.
var gatewaySchemaV1 = map[string][]string{
	"garden_binding": {`
CREATE TABLE garden_binding (singleton INTEGER PRIMARY KEY CHECK(singleton=1), binding TEXT NOT NULL)`},
	"garden_inbox": {`
CREATE TABLE garden_inbox (
 principal TEXT PRIMARY KEY, name TEXT NOT NULL UNIQUE, participant TEXT NOT NULL UNIQUE,
 generation TEXT NOT NULL, cursor INTEGER NOT NULL, pending_seq INTEGER NOT NULL DEFAULT 0,
 pending_id TEXT NOT NULL DEFAULT '', receipt TEXT NOT NULL DEFAULT '', last_receipt TEXT NOT NULL DEFAULT '',
 consumer TEXT NOT NULL DEFAULT '', lease_until INTEGER NOT NULL DEFAULT 0
)`, `
CREATE TABLE garden_inbox (
 principal TEXT PRIMARY KEY, name TEXT NOT NULL UNIQUE, participant TEXT NOT NULL UNIQUE,
 generation TEXT NOT NULL, cursor INTEGER NOT NULL, pending_seq INTEGER NOT NULL DEFAULT 0,
 pending_id TEXT NOT NULL DEFAULT '', receipt TEXT NOT NULL DEFAULT '', last_receipt TEXT NOT NULL DEFAULT '',
 consumer TEXT NOT NULL DEFAULT '', lease_until INTEGER NOT NULL DEFAULT 0
, deliverable INTEGER NOT NULL DEFAULT 0)`, `
CREATE TABLE garden_inbox (
 principal TEXT PRIMARY KEY, name TEXT NOT NULL UNIQUE, participant TEXT NOT NULL UNIQUE,
 generation TEXT NOT NULL, cursor INTEGER NOT NULL, pending_seq INTEGER NOT NULL DEFAULT 0,
 pending_id TEXT NOT NULL DEFAULT '', receipt TEXT NOT NULL DEFAULT '', last_receipt TEXT NOT NULL DEFAULT '',
 consumer TEXT NOT NULL DEFAULT '', lease_until INTEGER NOT NULL DEFAULT 0, deliverable INTEGER NOT NULL DEFAULT 0
)`},
	"garden_message_depth": {`
CREATE TABLE garden_message_depth (message_id TEXT PRIMARY KEY, depth INTEGER NOT NULL, seq INTEGER NOT NULL)`},
	"garden_attention": {`
CREATE TABLE garden_attention (
 pair_low TEXT NOT NULL, pair_high TEXT NOT NULL, attended_at INTEGER NOT NULL
)`},
}

func validateStateSchemaV1(db *sql.DB) error {
	expected := map[string]string{
		"participants": `
			CREATE TABLE participants (
				id TEXT PRIMARY KEY,
				name TEXT UNIQUE NOT NULL,
				kind TEXT NOT NULL,
				provider TEXT DEFAULT '',
				model TEXT DEFAULT '',
				created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
			)`,
		"checkpoints": `
			CREATE TABLE checkpoints (
				participant_id TEXT PRIMARY KEY REFERENCES participants(id) ON DELETE CASCADE,
				last_seen_seq INTEGER DEFAULT 0,
				last_processed_id TEXT DEFAULT '',
				last_responded_id TEXT DEFAULT '',
				hourly_count INTEGER DEFAULT 0,
				hourly_reset_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
				responsiveness REAL DEFAULT 0.5
			)`,
		"redactions": `
			CREATE TABLE redactions (
				message_id TEXT PRIMARY KEY,
				reason TEXT NOT NULL,
				redacted_by TEXT NOT NULL,
				created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
			)`,
	}
	rows, err := db.Query(`
		SELECT type, name, sql
		FROM sqlite_schema
		WHERE name NOT LIKE 'sqlite_%'
		ORDER BY type, name
	`)
	if err != nil {
		return err
	}
	defer rows.Close()
	seen := make(map[string]bool, len(expected)+len(gatewaySchemaV1)+len(gatewayIndexesV1))
	for rows.Next() {
		var objectType string
		var name string
		var definition sql.NullString
		if err := rows.Scan(&objectType, &name, &definition); err != nil {
			return err
		}
		matchesType := func(kind, want string) bool {
			return objectType == kind && definition.Valid && normaliseSchemaSQL(definition.String) == normaliseSchemaSQL(want)
		}
		matches := func(want string) bool { return matchesType("table", want) }
		matched := false
		if want, ok := expected[name]; ok {
			matched = matches(want)
		} else if want, ok := gatewayIndexesV1[name]; ok {
			matched = matchesType("index", want)
		} else {
			for _, want := range gatewaySchemaV1[name] {
				matched = matched || matches(want)
			}
		}
		if !matched {
			return fmt.Errorf("unexpected schema object %s %s", objectType, name)
		}
		seen[name] = true
	}
	if err := rows.Err(); err != nil {
		return err
	}
	// The first gateway created two tables. The loop guard added two tables and two
	// indexes in one upgrade transaction, so a snapshot holds either both groups or only the first.
	baseSeen, laterSeen := 0, 0
	for _, name := range gatewayBaseTables {
		if seen[name] {
			baseSeen++
		}
	}
	for _, name := range gatewayLoopGuardTables {
		if seen[name] {
			laterSeen++
		}
	}
	if (baseSeen != 0 && baseSeen != len(gatewayBaseTables)) ||
		(baseSeen == 0 && laterSeen != 0) ||
		(laterSeen != 0 && laterSeen != len(gatewayLoopGuardTables)) {
		return fmt.Errorf("partial gateway state schema objects")
	}
	gatewaySeen := baseSeen + laterSeen
	if len(seen)-gatewaySeen != len(expected) {
		return fmt.Errorf("missing state schema objects")
	}
	return nil
}

func normaliseSchemaSQL(definition string) string {
	normalised := strings.ToLower(strings.Join(strings.Fields(definition), " "))
	normalised = strings.Replace(normalised, "create table if not exists ", "create table ", 1)
	return strings.Replace(normalised, "create index if not exists ", "create index ", 1)
}
