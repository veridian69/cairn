package state

import (
	"database/sql"
	"errors"
	"fmt"

	"github.com/google/uuid"
	"github.com/veridian69/cairn/a2a/internal/model"
)

func (d *DB) RegisterParticipant(p model.Participant) (model.Participant, error) {
	// Check if already exists by name
	existing, err := d.GetParticipantByName(p.Name)
	if err == nil {
		if existing.Kind != p.Kind {
			return existing, fmt.Errorf("participant %q kind mismatch: %s != %s", p.Name, existing.Kind, p.Kind)
		}
		if _, err := d.db.Exec(
			"UPDATE participants SET provider = ?, model = ? WHERE id = ?",
			p.Provider, p.Model, existing.ID,
		); err != nil {
			return existing, fmt.Errorf("updating participant: %w", err)
		}
		existing.Provider = p.Provider
		existing.Model = p.Model
		return existing, nil
	}

	p.ID = uuid.New().String()
	_, err = d.db.Exec(
		"INSERT INTO participants (id, name, kind, provider, model) VALUES (?, ?, ?, ?, ?)",
		p.ID, p.Name, p.Kind, p.Provider, p.Model,
	)
	if err != nil {
		return p, fmt.Errorf("inserting participant: %w", err)
	}
	return p, nil
}

func (d *DB) GetParticipantByName(name string) (model.Participant, error) {
	var p model.Participant
	err := d.db.QueryRow(
		"SELECT id, name, kind, provider, model FROM participants WHERE name = ?", name,
	).Scan(&p.ID, &p.Name, &p.Kind, &p.Provider, &p.Model)
	if errors.Is(err, sql.ErrNoRows) {
		return p, fmt.Errorf("participant %q not found", name)
	}
	return p, err
}

func (d *DB) GetParticipantByID(id string) (model.Participant, error) {
	var p model.Participant
	err := d.db.QueryRow(
		"SELECT id, name, kind, provider, model FROM participants WHERE id = ?", id,
	).Scan(&p.ID, &p.Name, &p.Kind, &p.Provider, &p.Model)
	if errors.Is(err, sql.ErrNoRows) {
		return p, fmt.Errorf("participant %q not found", id)
	}
	return p, err
}

func (d *DB) ListParticipants() ([]model.Participant, error) {
	rows, err := d.db.Query("SELECT id, name, kind, provider, model FROM participants")
	if err != nil {
		return nil, err
	}
	defer rows.Close()

	var ps []model.Participant
	for rows.Next() {
		var p model.Participant
		if err := rows.Scan(&p.ID, &p.Name, &p.Kind, &p.Provider, &p.Model); err != nil {
			return nil, err
		}
		ps = append(ps, p)
	}
	return ps, rows.Err()
}

func (d *DB) DeleteParticipant(name string) error {
	result, err := d.db.Exec("DELETE FROM participants WHERE name = ?", name)
	if err != nil {
		return err
	}
	n, _ := result.RowsAffected()
	if n == 0 {
		return fmt.Errorf("participant %q not found", name)
	}
	return nil
}
