package state

import (
	"database/sql"
	"errors"
	"time"
)

type Checkpoint struct {
	ParticipantID   string
	LastSeenSeq     uint64
	LastProcessedID string
	LastRespondedID string
	HourlyCount     int
	HourlyResetAt   time.Time
	Responsiveness  float64
}

func (d *DB) GetCheckpoint(participantID string, initialResponsiveness float64) (Checkpoint, error) {
	cp := Checkpoint{ParticipantID: participantID}

	var hourlyResetAt sql.NullString
	err := d.db.QueryRow(
		`SELECT last_seen_seq, last_processed_id, last_responded_id,
		        hourly_count, hourly_reset_at, responsiveness
		 FROM checkpoints WHERE participant_id = ?`, participantID,
	).Scan(&cp.LastSeenSeq, &cp.LastProcessedID, &cp.LastRespondedID,
		&cp.HourlyCount, &hourlyResetAt, &cp.Responsiveness)

	if errors.Is(err, sql.ErrNoRows) {
		// Row not found — seed it from the effective agent configuration.
		_, err = d.db.Exec(
			`INSERT OR IGNORE INTO checkpoints (participant_id, responsiveness) VALUES (?, ?)`,
			participantID, initialResponsiveness,
		)
		if err != nil {
			return cp, err
		}
		cp.Responsiveness = initialResponsiveness
		return cp, nil
	}
	if err != nil {
		return cp, err
	}

	if hourlyResetAt.Valid && hourlyResetAt.String != "" {
		for _, layout := range []string{time.RFC3339, "2006-01-02 15:04:05", "2006-01-02T15:04:05Z"} {
			if t, err := time.Parse(layout, hourlyResetAt.String); err == nil {
				cp.HourlyResetAt = t
				break
			}
		}
	}

	return cp, nil
}

func (d *DB) SaveCheckpoint(cp Checkpoint) error {
	var hourlyResetAt string
	if !cp.HourlyResetAt.IsZero() {
		hourlyResetAt = cp.HourlyResetAt.UTC().Format(time.RFC3339)
	} else {
		hourlyResetAt = time.Now().UTC().Format(time.RFC3339)
	}

	_, err := d.db.Exec(
		`INSERT INTO checkpoints
		   (participant_id, last_seen_seq, last_processed_id, last_responded_id,
		    hourly_count, hourly_reset_at, responsiveness)
		 VALUES (?, ?, ?, ?, ?, ?, ?)
		 ON CONFLICT(participant_id) DO UPDATE SET
		   last_seen_seq      = excluded.last_seen_seq,
		   last_processed_id  = excluded.last_processed_id,
		   last_responded_id  = excluded.last_responded_id,
		   hourly_count       = excluded.hourly_count,
		   hourly_reset_at    = excluded.hourly_reset_at,
		   responsiveness     = excluded.responsiveness`,
		cp.ParticipantID, cp.LastSeenSeq, cp.LastProcessedID, cp.LastRespondedID,
		cp.HourlyCount, hourlyResetAt, cp.Responsiveness,
	)
	return err
}
