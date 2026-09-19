package snapshot

import (
	"context"
	"database/sql"
	"fmt"
	"os"
	"path/filepath"
	"strings"

	"github.com/veridian69/cairn/a2a/internal/maintenance"
	"github.com/veridian69/cairn/a2a/internal/transport"
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
	seen := make(map[string]bool, len(expected))
	for rows.Next() {
		var objectType string
		var name string
		var definition sql.NullString
		if err := rows.Scan(&objectType, &name, &definition); err != nil {
			return err
		}
		want, ok := expected[name]
		if !ok || objectType != "table" || !definition.Valid ||
			normaliseSchemaSQL(definition.String) != normaliseSchemaSQL(want) {
			return fmt.Errorf("unexpected schema object %s %s", objectType, name)
		}
		seen[name] = true
	}
	if err := rows.Err(); err != nil {
		return err
	}
	if len(seen) != len(expected) {
		return fmt.Errorf("missing state schema objects")
	}
	return nil
}

func normaliseSchemaSQL(definition string) string {
	normalised := strings.ToLower(strings.Join(strings.Fields(definition), " "))
	return strings.Replace(normalised, "create table if not exists ", "create table ", 1)
}
