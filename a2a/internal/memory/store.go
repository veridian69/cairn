// Package memory implements the curated participant-facing memory store.
package memory

import (
	"context"
	"database/sql"
	"errors"
	"fmt"
	"log"
	"log/slog"
	"os"
	"path/filepath"
	"sync"
	"syscall"
	"time"

	_ "modernc.org/sqlite"
)

const currentSchemaVersion = 1

var ErrFutureSchema = errors.New("memory database schema is newer than this binary")

type cachedVector struct {
	id              string
	sourceMessageID string
	sourceCreatedAt time.Time
	vec             []float32
}

type Store struct {
	db      *sql.DB
	genConn *sql.Conn
	lock    *os.File

	MaxItemBytes int

	cacheMu    sync.Mutex
	cache      []cachedVector
	cacheGen   int64
	cacheModel string

	afterVectorLoad func()

	closeOnce sync.Once
	closeErr  error
}

type Options struct {
	RebuildFTS   bool
	MaxItemBytes int
}

func Open(dataDir string, options Options) (*Store, error) {
	if err := os.MkdirAll(dataDir, 0700); err != nil {
		return nil, fmt.Errorf("creating data dir: %w", err)
	}
	path := filepath.Join(dataDir, "memory.db")
	lock, err := os.OpenFile(path+".lock", os.O_CREATE|os.O_RDWR, 0600)
	if err != nil {
		return nil, fmt.Errorf("opening memory lock file: %w", err)
	}
	if err := syscall.Flock(int(lock.Fd()), syscall.LOCK_SH); err != nil {
		_ = lock.Close()
		return nil, fmt.Errorf("locking %s: %w", path+".lock", err)
	}
	fail := func(cause error) (*Store, error) {
		_ = syscall.Flock(int(lock.Fd()), syscall.LOCK_UN)
		_ = lock.Close()
		return nil, cause
	}

	db, err := sql.Open("sqlite",
		path+"?_pragma=journal_mode(WAL)&_pragma=busy_timeout(5000)&_txlock=immediate")
	if err != nil {
		return fail(fmt.Errorf("opening memory database: %w", err))
	}
	if err := migrate(db); err != nil {
		_ = db.Close()
		return fail(fmt.Errorf("migrating memory database: %w", err))
	}
	if err := os.Chmod(path, 0600); err != nil {
		log.Printf("warning: could not tighten %s to 0600: %v", path, err)
	}
	genConn, err := db.Conn(context.Background())
	if err != nil {
		_ = db.Close()
		return fail(fmt.Errorf("reserving generation connection: %w", err))
	}
	store := &Store{
		db: db, genConn: genConn, lock: lock,
		MaxItemBytes: options.MaxItemBytes,
	}
	if options.RebuildFTS {
		started := time.Now()
		rows, err := store.rebuildFTS()
		if err != nil {
			_ = store.Close()
			return nil, fmt.Errorf("rebuilding FTS index: %w", err)
		}
		slog.Debug("memory: rebuilt FTS index",
			"rows", rows, "duration", time.Since(started))
	}
	return store, nil
}

func (s *Store) Close() error {
	s.closeOnce.Do(func() {
		if s.genConn != nil {
			if err := s.genConn.Close(); err != nil {
				s.closeErr = err
			}
		}
		if s.db != nil {
			if err := s.db.Close(); err != nil && s.closeErr == nil {
				s.closeErr = err
			}
		}
		if s.lock != nil {
			_ = syscall.Flock(int(s.lock.Fd()), syscall.LOCK_UN)
			if err := s.lock.Close(); err != nil && s.closeErr == nil {
				s.closeErr = err
			}
		}
	})
	return s.closeErr
}

func migrate(db *sql.DB) error {
	tx, err := db.Begin()
	if err != nil {
		return err
	}
	defer tx.Rollback()
	if _, err := tx.Exec(`CREATE TABLE IF NOT EXISTS schema_version (
		id INTEGER PRIMARY KEY CHECK (id = 1),
		version INTEGER NOT NULL
	)`); err != nil {
		return err
	}
	var version int
	err = tx.QueryRow(`SELECT version FROM schema_version WHERE id = 1`).Scan(&version)
	switch {
	case errors.Is(err, sql.ErrNoRows):
		version = 0
	case err != nil:
		return err
	}
	if version > currentSchemaVersion {
		return fmt.Errorf("%w: database has v%d, binary understands v%d",
			ErrFutureSchema, version, currentSchemaVersion)
	}
	if version < 1 {
		if _, err := tx.Exec(schemaV1); err != nil {
			return err
		}
		if _, err := tx.Exec(`INSERT INTO schema_version (id, version) VALUES (1, 1)
			ON CONFLICT(id) DO UPDATE SET version = excluded.version`); err != nil {
			return err
		}
	}
	return tx.Commit()
}

const schemaV1 = `
CREATE TABLE IF NOT EXISTS memories (
	rowid             INTEGER PRIMARY KEY,
	id                TEXT      NOT NULL UNIQUE,
	content           TEXT      NOT NULL,
	author_id         TEXT      NOT NULL,
	author_name       TEXT      NOT NULL,
	source_message_id TEXT      NOT NULL,
	source_created_at TIMESTAMP NOT NULL,
	reply_to          TEXT,
	nominated_by      TEXT      NOT NULL,
	pinned            INTEGER   NOT NULL DEFAULT 0,
	created_at        TIMESTAMP NOT NULL,
	content_sha256    TEXT      NOT NULL,
	embedding         BLOB,
	embedding_model   TEXT,
	embedding_dim     INTEGER
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_memories_dedup
	ON memories(source_message_id, content_sha256);
CREATE INDEX IF NOT EXISTS idx_memories_source_created ON memories(source_created_at);
CREATE INDEX IF NOT EXISTS idx_memories_pinned ON memories(pinned) WHERE pinned = 1;
CREATE INDEX IF NOT EXISTS idx_memories_source ON memories(source_message_id);
CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts
	USING fts5(content, memory_id UNINDEXED);
`

func (s *Store) rebuildFTS() (int64, error) {
	tx, err := s.db.Begin()
	if err != nil {
		return 0, err
	}
	defer tx.Rollback()
	if _, err := tx.Exec(`DELETE FROM memories_fts`); err != nil {
		return 0, err
	}
	result, err := tx.Exec(`INSERT INTO memories_fts (content, memory_id)
		SELECT content, id FROM memories`)
	if err != nil {
		return 0, err
	}
	rows, err := result.RowsAffected()
	if err != nil {
		return 0, err
	}
	if err := tx.Commit(); err != nil {
		return 0, err
	}
	return rows, nil
}
