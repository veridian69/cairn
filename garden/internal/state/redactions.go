package state

import (
	"database/sql"
	"errors"
	"fmt"
)

type Redaction struct {
	MessageID  string
	Reason     string
	RedactedBy string
}

func (d *DB) Redact(messageID, reason, redactedBy string) error {
	_, err := d.db.Exec(
		"INSERT OR IGNORE INTO redactions (message_id, reason, redacted_by) VALUES (?, ?, ?)",
		messageID, reason, redactedBy,
	)
	return err
}

func (d *DB) IsRedacted(messageID string) (bool, error) {
	var count int
	if err := d.db.QueryRow("SELECT COUNT(*) FROM redactions WHERE message_id = ?", messageID).Scan(&count); err != nil {
		return true, err
	}
	return count > 0, nil
}

func (d *DB) RedactedIDs() (map[string]bool, error) {
	reasons, err := d.RedactionReasons()
	if err != nil {
		return nil, err
	}
	ids := make(map[string]bool, len(reasons))
	for id := range reasons {
		ids[id] = true
	}
	return ids, nil
}

func (d *DB) RedactionReasons() (map[string]string, error) {
	rows, err := d.db.Query("SELECT message_id, reason FROM redactions")
	if err != nil {
		return nil, err
	}
	defer rows.Close()

	reasons := make(map[string]string)
	for rows.Next() {
		var id string
		var reason string
		if err := rows.Scan(&id, &reason); err != nil {
			return nil, err
		}
		reasons[id] = reason
	}
	return reasons, rows.Err()
}

func (d *DB) RedactionReason(messageID string) (string, bool, error) {
	var reason string
	switch err := d.db.QueryRow(
		"SELECT reason FROM redactions WHERE message_id = ?",
		messageID,
	).Scan(&reason); {
	case err == nil:
		return reason, true, nil
	case errors.Is(err, sql.ErrNoRows):
		return "", false, nil
	default:
		return "", false, fmt.Errorf("lookup redaction: %w", err)
	}
}
