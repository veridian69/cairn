package state

import (
	"database/sql"
	"fmt"

	_ "modernc.org/sqlite"
)

type DB struct {
	db *sql.DB
}

func Open(path string) (*DB, error) {
	db, err := sql.Open("sqlite",
		path+"?_pragma=journal_mode(WAL)&_pragma=busy_timeout(5000)&_pragma=foreign_keys(1)&_txlock=immediate")
	if err != nil {
		return nil, fmt.Errorf("opening database: %w", err)
	}
	if err := migrate(db); err != nil {
		db.Close()
		return nil, fmt.Errorf("migrating: %w", err)
	}
	return &DB{db: db}, nil
}

func (d *DB) Close() error {
	return d.db.Close()
}

func migrate(db *sql.DB) error {
	schema := `
	CREATE TABLE IF NOT EXISTS participants (
		id TEXT PRIMARY KEY,
		name TEXT UNIQUE NOT NULL,
		kind TEXT NOT NULL,
		provider TEXT DEFAULT '',
		model TEXT DEFAULT '',
		created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
	);
	CREATE TABLE IF NOT EXISTS checkpoints (
		participant_id TEXT PRIMARY KEY REFERENCES participants(id) ON DELETE CASCADE,
		last_seen_seq INTEGER DEFAULT 0,
		last_processed_id TEXT DEFAULT '',
		last_responded_id TEXT DEFAULT '',
		hourly_count INTEGER DEFAULT 0,
		hourly_reset_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
		responsiveness REAL DEFAULT 0.5
	);
	CREATE TABLE IF NOT EXISTS redactions (
		message_id TEXT PRIMARY KEY,
		reason TEXT NOT NULL,
		redacted_by TEXT NOT NULL,
		created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
	);
	`
	_, err := db.Exec(schema)
	return err
}
