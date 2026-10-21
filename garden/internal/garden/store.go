package garden

import (
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"fmt"

	"github.com/google/uuid"
	"github.com/veridian69/cairn/garden/internal/model"
)

const gardenSchema = `
CREATE TABLE IF NOT EXISTS garden_binding (singleton INTEGER PRIMARY KEY CHECK(singleton=1), binding TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS garden_inbox (
 principal TEXT PRIMARY KEY, name TEXT NOT NULL UNIQUE, participant TEXT NOT NULL UNIQUE,
 generation TEXT NOT NULL, cursor INTEGER NOT NULL, pending_seq INTEGER NOT NULL DEFAULT 0,
 pending_id TEXT NOT NULL DEFAULT '', receipt TEXT NOT NULL DEFAULT '', last_receipt TEXT NOT NULL DEFAULT '',
 consumer TEXT NOT NULL DEFAULT '', lease_until INTEGER NOT NULL DEFAULT 0, deliverable INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS garden_message_depth (message_id TEXT PRIMARY KEY, depth INTEGER NOT NULL, seq INTEGER NOT NULL);
CREATE INDEX IF NOT EXISTS garden_message_depth_seq ON garden_message_depth(seq);
CREATE TABLE IF NOT EXISTS garden_attention (
 pair_low TEXT NOT NULL, pair_high TEXT NOT NULL, attended_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS garden_attention_pair ON garden_attention(pair_low, pair_high, attended_at);
CREATE INDEX IF NOT EXISTS garden_attention_expiry ON garden_attention(attended_at);`

type inbox struct {
	principal, name, participant, generation  string
	cursor, pending                           uint64
	pendingID, receipt, lastReceipt, consumer string
	leaseUntil                                int64
	deliverable                               bool
}

func (s *Server) initStore(ctx context.Context, generation string, tail uint64) error {
	tx, err := s.sql.BeginTx(ctx, nil)
	if err != nil {
		return err
	}
	defer tx.Rollback()
	if _, err = tx.ExecContext(ctx, gardenSchema); err != nil {
		return err
	}
	if err = addColumnIfMissing(ctx, tx, "garden_inbox", "deliverable", "INTEGER NOT NULL DEFAULT 0"); err != nil {
		return err
	}
	raw, _ := json.Marshal(s.binding)
	var saved string
	err = tx.QueryRowContext(ctx, "SELECT binding FROM garden_binding WHERE singleton=1").Scan(&saved)
	if errors.Is(err, sql.ErrNoRows) {
		_, err = tx.ExecContext(ctx, "INSERT INTO garden_binding(singleton,binding) VALUES(1,?)", string(raw))
	} else if err == nil && saved != string(raw) {
		return errors.New("Garden storage is bound to another Cairn authority")
	}
	if err != nil {
		return err
	}
	for principal, name := range s.cfg.Principals {
		var oldName, participant string
		err = tx.QueryRowContext(ctx, "SELECT name,participant FROM garden_inbox WHERE principal=?", principal).Scan(&oldName, &participant)
		if err == nil {
			if name != oldName {
				return errors.New("Garden principal name cannot be rebound")
			}
			var actual string
			if err = tx.QueryRowContext(ctx, "SELECT name FROM participants WHERE id=?", participant).Scan(&actual); err != nil || actual != name {
				return errors.New("Garden participant ownership has changed")
			}
			continue
		}
		if !errors.Is(err, sql.ErrNoRows) {
			return err
		}
		participant = uuid.NewString()
		if _, err = tx.ExecContext(ctx, "INSERT INTO participants(id,name,kind) VALUES(?,?,?)", participant, name, model.KindAgent); err != nil {
			return fmt.Errorf("Garden name %q is already owned or unavailable", name)
		}
		if _, err = tx.ExecContext(ctx, "INSERT INTO garden_inbox(principal,name,participant,generation,cursor) VALUES(?,?,?,?,?)", principal, name, participant, generation, tail); err != nil {
			return err
		}
	}
	return tx.Commit()
}

// addColumnIfMissing upgrades tables created by an older schema; CREATE TABLE IF NOT EXISTS never adds columns.
func addColumnIfMissing(ctx context.Context, tx *sql.Tx, table, column, definition string) error {
	rows, err := tx.QueryContext(ctx, "SELECT name FROM pragma_table_info(?)", table)
	if err != nil {
		return err
	}
	found := false
	for rows.Next() {
		var name string
		if err = rows.Scan(&name); err != nil {
			rows.Close()
			return err
		}
		found = found || name == column
	}
	if err = rows.Close(); err != nil {
		return err
	}
	if err = rows.Err(); err != nil || found {
		return err
	}
	_, err = tx.ExecContext(ctx, fmt.Sprintf("ALTER TABLE %s ADD COLUMN %s %s", table, column, definition))
	return err
}
func (s *Server) loadInbox(ctx context.Context, principal string) (inbox, error) {
	var b inbox
	b.principal = principal
	err := s.sql.QueryRowContext(ctx, `SELECT name,participant,generation,cursor,pending_seq,pending_id,receipt,last_receipt,consumer,lease_until,deliverable FROM garden_inbox WHERE principal=?`, principal).Scan(&b.name, &b.participant, &b.generation, &b.cursor, &b.pending, &b.pendingID, &b.receipt, &b.lastReceipt, &b.consumer, &b.leaseUntil, &b.deliverable)
	return b, err
}
func (s *Server) saveInbox(ctx context.Context, b inbox) error {
	_, err := s.sql.ExecContext(ctx, `UPDATE garden_inbox SET cursor=?,pending_seq=?,pending_id=?,receipt=?,last_receipt=?,consumer=?,lease_until=?,deliverable=? WHERE principal=?`, b.cursor, b.pending, b.pendingID, b.receipt, b.lastReceipt, b.consumer, b.leaseUntil, b.deliverable, b.principal)
	return err
}
