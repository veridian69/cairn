package garden

import (
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"fmt"

	"github.com/google/uuid"
	"github.com/veridian69/cairn/a2a/internal/model"
)

const gardenSchema = `
CREATE TABLE IF NOT EXISTS garden_binding (singleton INTEGER PRIMARY KEY CHECK(singleton=1), binding TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS garden_inbox (
 principal TEXT PRIMARY KEY, name TEXT NOT NULL UNIQUE, participant TEXT NOT NULL UNIQUE,
 generation TEXT NOT NULL, cursor INTEGER NOT NULL, pending_seq INTEGER NOT NULL DEFAULT 0,
 pending_id TEXT NOT NULL DEFAULT '', receipt TEXT NOT NULL DEFAULT '', last_receipt TEXT NOT NULL DEFAULT '',
 consumer TEXT NOT NULL DEFAULT '', lease_until INTEGER NOT NULL DEFAULT 0
);`

type inbox struct {
	principal, name, participant, generation  string
	cursor, pending                           uint64
	pendingID, receipt, lastReceipt, consumer string
	leaseUntil                                int64
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
func (s *Server) loadInbox(ctx context.Context, principal string) (inbox, error) {
	var b inbox
	b.principal = principal
	err := s.sql.QueryRowContext(ctx, `SELECT name,participant,generation,cursor,pending_seq,pending_id,receipt,last_receipt,consumer,lease_until FROM garden_inbox WHERE principal=?`, principal).Scan(&b.name, &b.participant, &b.generation, &b.cursor, &b.pending, &b.pendingID, &b.receipt, &b.lastReceipt, &b.consumer, &b.leaseUntil)
	return b, err
}
func (s *Server) saveInbox(ctx context.Context, b inbox) error {
	_, err := s.sql.ExecContext(ctx, `UPDATE garden_inbox SET cursor=?,pending_seq=?,pending_id=?,receipt=?,last_receipt=?,consumer=?,lease_until=? WHERE principal=?`, b.cursor, b.pending, b.pendingID, b.receipt, b.lastReceipt, b.consumer, b.leaseUntil, b.principal)
	return err
}
