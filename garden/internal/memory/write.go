package memory

import (
	"crypto/sha256"
	"database/sql"
	"encoding/hex"
	"errors"
	"fmt"
	"strings"
	"time"

	"github.com/google/uuid"
)

var (
	ErrEmptyContent      = errors.New("content is empty after trimming")
	ErrTooLarge          = errors.New("content exceeds max_item_bytes")
	ErrNotFound          = errors.New("memory item not found")
	ErrAmbiguousID       = errors.New("memory ID prefix is ambiguous")
	ErrInvalidProvenance = errors.New("memory item provenance is incomplete")
)

type Item struct {
	ID              string    `json:"id"`
	Content         string    `json:"content"`
	AuthorID        string    `json:"author_id"`
	AuthorName      string    `json:"author_name"`
	SourceMessageID string    `json:"source_message_id"`
	SourceCreatedAt time.Time `json:"source_created_at"`
	ReplyTo         *string   `json:"reply_to,omitempty"`
	NominatedBy     string    `json:"nominated_by"`
	Pinned          bool      `json:"pinned"`
	CreatedAt       time.Time `json:"created_at"`
}

type InsertResult struct {
	ID      string
	Existed bool
}

const itemColumns = `id, content, author_id, author_name, source_message_id,
	source_created_at, reply_to, nominated_by, pinned, created_at`

func scanItem(row interface{ Scan(...any) error }) (Item, error) {
	var item Item
	var pinned int
	err := row.Scan(
		&item.ID, &item.Content, &item.AuthorID, &item.AuthorName,
		&item.SourceMessageID, &item.SourceCreatedAt, &item.ReplyTo,
		&item.NominatedBy, &pinned, &item.CreatedAt,
	)
	item.Pinned = pinned == 1
	return item, err
}

func validateProvenance(item Item) error {
	if strings.TrimSpace(item.AuthorID) == "" ||
		strings.TrimSpace(item.AuthorName) == "" ||
		strings.TrimSpace(item.SourceMessageID) == "" ||
		item.SourceCreatedAt.IsZero() ||
		strings.TrimSpace(item.NominatedBy) == "" {
		return ErrInvalidProvenance
	}
	return nil
}

func (s *Store) Insert(item Item) (InsertResult, error) {
	content := strings.TrimSpace(item.Content)
	if content == "" {
		return InsertResult{}, ErrEmptyContent
	}
	if s.MaxItemBytes > 0 && len(content) > s.MaxItemBytes {
		return InsertResult{}, fmt.Errorf("%w: %d bytes (limit %d)",
			ErrTooLarge, len(content), s.MaxItemBytes)
	}
	if err := validateProvenance(item); err != nil {
		return InsertResult{}, err
	}
	sum := sha256.Sum256([]byte(content))
	hash := hex.EncodeToString(sum[:])
	id := uuid.NewString()
	createdAt := time.Now().UTC()
	pinned := 0
	if item.Pinned {
		pinned = 1
	}

	tx, err := s.db.Begin()
	if err != nil {
		return InsertResult{}, err
	}
	defer tx.Rollback()
	result, err := tx.Exec(`INSERT INTO memories
		(id, content, author_id, author_name, source_message_id,
		 source_created_at, reply_to, nominated_by, pinned, created_at, content_sha256)
		VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
		ON CONFLICT(source_message_id, content_sha256) DO NOTHING`,
		id, content, item.AuthorID, item.AuthorName, item.SourceMessageID,
		item.SourceCreatedAt.UTC(), item.ReplyTo, item.NominatedBy, pinned,
		createdAt, hash)
	if err != nil {
		return InsertResult{}, err
	}
	inserted, err := result.RowsAffected()
	if err != nil {
		return InsertResult{}, err
	}
	if inserted == 1 {
		if _, err := tx.Exec(`INSERT INTO memories_fts (content, memory_id) VALUES (?, ?)`, content, id); err != nil {
			return InsertResult{}, err
		}
		if err := tx.Commit(); err != nil {
			return InsertResult{}, err
		}
		s.invalidateCache()
		return InsertResult{ID: id}, nil
	}

	if err := tx.QueryRow(`SELECT id FROM memories
		WHERE source_message_id = ? AND content_sha256 = ?`,
		item.SourceMessageID, hash).Scan(&id); err != nil {
		return InsertResult{}, err
	}
	if item.Pinned {
		if _, err := tx.Exec(`UPDATE memories SET pinned = 1 WHERE id = ?`, id); err != nil {
			return InsertResult{}, err
		}
	}
	if err := tx.Commit(); err != nil {
		return InsertResult{}, err
	}
	return InsertResult{ID: id, Existed: true}, nil
}

func (s *Store) setPinned(id string, pinned int) error {
	result, err := s.db.Exec(`UPDATE memories SET pinned = ? WHERE id = ?`, pinned, id)
	if err != nil {
		return err
	}
	n, err := result.RowsAffected()
	if err != nil {
		return err
	}
	if n == 0 {
		return fmt.Errorf("%w: %s", ErrNotFound, id)
	}
	return nil
}

func (s *Store) Pin(id string) error   { return s.setPinned(id, 1) }
func (s *Store) Unpin(id string) error { return s.setPinned(id, 0) }

func (s *Store) Forget(id string) error {
	tx, err := s.db.Begin()
	if err != nil {
		return err
	}
	defer tx.Rollback()
	result, err := tx.Exec(`DELETE FROM memories WHERE id = ?`, id)
	if err != nil {
		return err
	}
	n, err := result.RowsAffected()
	if err != nil {
		return err
	}
	if n == 0 {
		return fmt.Errorf("%w: %s", ErrNotFound, id)
	}
	if _, err := tx.Exec(`DELETE FROM memories_fts WHERE memory_id = ?`, id); err != nil {
		return err
	}
	if err := tx.Commit(); err != nil {
		return err
	}
	s.invalidateCache()
	return nil
}

func (s *Store) GetByID(id string) (Item, error) {
	item, err := scanItem(s.db.QueryRow(`SELECT `+itemColumns+` FROM memories WHERE id = ?`, id))
	if errors.Is(err, sql.ErrNoRows) {
		return Item{}, fmt.Errorf("%w: %s", ErrNotFound, id)
	}
	return item, err
}

// ResolveID accepts either a complete memory ID or an unambiguous prefix.
func (s *Store) ResolveID(prefix string) (string, error) {
	prefix = strings.TrimSpace(prefix)
	if prefix == "" {
		return "", fmt.Errorf("%w: empty prefix", ErrNotFound)
	}
	rows, err := s.db.Query(`SELECT id FROM memories
		WHERE substr(id, 1, ?) = ? ORDER BY id LIMIT 2`, len(prefix), prefix)
	if err != nil {
		return "", err
	}
	defer rows.Close()
	var ids []string
	for rows.Next() {
		var id string
		if err := rows.Scan(&id); err != nil {
			return "", err
		}
		ids = append(ids, id)
	}
	if err := rows.Err(); err != nil {
		return "", err
	}
	switch len(ids) {
	case 0:
		return "", fmt.Errorf("%w: %s", ErrNotFound, prefix)
	case 1:
		return ids[0], nil
	default:
		return "", fmt.Errorf("%w: %s", ErrAmbiguousID, prefix)
	}
}

func (s *Store) ItemsBySource(messageID string, redacted map[string]bool) ([]Item, error) {
	if redacted[messageID] {
		return nil, nil
	}
	return s.queryItems(`SELECT `+itemColumns+` FROM memories
		WHERE source_message_id = ? ORDER BY created_at ASC, id ASC`, messageID)
}

type ListFilter struct {
	ByAuthorID string
	Last       int
	PinnedOnly bool
}

func (s *Store) List(filter ListFilter, redacted map[string]bool) ([]Item, error) {
	query := `SELECT ` + itemColumns + ` FROM memories WHERE 1 = 1`
	var args []any
	if filter.ByAuthorID != "" {
		query += ` AND author_id = ?`
		args = append(args, filter.ByAuthorID)
	}
	if filter.PinnedOnly {
		query += ` AND pinned = 1`
	}
	query += ` ORDER BY created_at DESC, id ASC`
	items, err := s.queryItems(query, args...)
	if err != nil {
		return nil, err
	}
	items = filterRedacted(items, redacted)
	if filter.Last > 0 && len(items) > filter.Last {
		items = items[:filter.Last]
	}
	return items, nil
}

type PruneFilter struct {
	Before     time.Time
	ByAuthorID string
}

func (filter PruneFilter) where() (string, []any) {
	clause := `1 = 1`
	var args []any
	if !filter.Before.IsZero() {
		clause += ` AND created_at < ?`
		args = append(args, filter.Before.UTC())
	}
	if filter.ByAuthorID != "" {
		clause += ` AND author_id = ?`
		args = append(args, filter.ByAuthorID)
	}
	return clause, args
}

func (s *Store) PruneCount(filter PruneFilter) (int, error) {
	ids, err := s.PruneCandidates(filter)
	return len(ids), err
}

func (s *Store) Prune(filter PruneFilter) (int, error) {
	ids, err := s.PruneCandidates(filter)
	if err != nil {
		return 0, err
	}
	return s.PruneIDs(ids)
}

// PruneCandidates returns the exact item IDs matching a prune filter. Commands
// snapshot these before confirmation so later inserts cannot be swept into an
// already-confirmed deletion.
func (s *Store) PruneCandidates(filter PruneFilter) ([]string, error) {
	clause, args := filter.where()
	rows, err := s.db.Query(`SELECT id FROM memories WHERE `+clause+`
		ORDER BY created_at ASC, id ASC`, args...)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var ids []string
	for rows.Next() {
		var id string
		if err := rows.Scan(&id); err != nil {
			return nil, err
		}
		ids = append(ids, id)
	}
	return ids, rows.Err()
}

// PruneIDs deletes only the supplied snapshot of item IDs.
func (s *Store) PruneIDs(ids []string) (int, error) {
	if len(ids) == 0 {
		return 0, nil
	}
	tx, err := s.db.Begin()
	if err != nil {
		return 0, err
	}
	defer tx.Rollback()
	var count int64
	for _, id := range ids {
		if _, err := tx.Exec(`DELETE FROM memories_fts WHERE memory_id = ?`, id); err != nil {
			return 0, err
		}
		result, err := tx.Exec(`DELETE FROM memories WHERE id = ?`, id)
		if err != nil {
			return 0, err
		}
		deleted, err := result.RowsAffected()
		if err != nil {
			return 0, err
		}
		count += deleted
	}
	if err := tx.Commit(); err != nil {
		return 0, err
	}
	s.invalidateCache()
	return int(count), nil
}

func (s *Store) queryItems(query string, args ...any) ([]Item, error) {
	rows, err := s.db.Query(query, args...)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var items []Item
	for rows.Next() {
		item, err := scanItem(rows)
		if err != nil {
			return nil, err
		}
		items = append(items, item)
	}
	return items, rows.Err()
}

func filterRedacted(items []Item, redacted map[string]bool) []Item {
	if len(redacted) == 0 {
		return items
	}
	filtered := items[:0]
	for _, item := range items {
		if !redacted[item.SourceMessageID] {
			filtered = append(filtered, item)
		}
	}
	return filtered
}

func (s *Store) invalidateCache() {
	s.cacheMu.Lock()
	s.cache = nil
	s.cacheMu.Unlock()
}
