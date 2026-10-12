package memory

import (
	"database/sql"
	"errors"
	"fmt"
	"time"
)

func (s *Store) Descendants(messageID string, redacted map[string]bool) ([]Item, error) {
	items, err := s.queryItems(`SELECT `+itemColumns+` FROM memories
		WHERE reply_to = ? ORDER BY source_created_at ASC, id ASC`, messageID)
	if err != nil {
		return nil, err
	}
	return filterRedacted(items, redacted), nil
}

func (s *Store) Temporal(
	anchor time.Time,
	window time.Duration,
	excludeItemID string,
	redacted map[string]bool,
) ([]Item, error) {
	items, err := s.queryItems(`SELECT `+itemColumns+` FROM memories
		WHERE source_created_at BETWEEN ? AND ? AND id != ?
		ORDER BY source_created_at ASC, id ASC`,
		anchor.Add(-window).UTC(), anchor.Add(window).UTC(), excludeItemID)
	if err != nil {
		return nil, err
	}
	return filterRedacted(items, redacted), nil
}

func (s *Store) EmbeddingOf(id string) ([]float32, string, error) {
	var encoded []byte
	var model *string
	var dimension *int
	err := s.db.QueryRow(`SELECT embedding, embedding_model, embedding_dim
		FROM memories WHERE id = ?`, id).Scan(&encoded, &model, &dimension)
	if errors.Is(err, sql.ErrNoRows) {
		return nil, "", fmt.Errorf("%w: %s", ErrNotFound, id)
	}
	if err != nil {
		return nil, "", err
	}
	if encoded == nil || model == nil || dimension == nil ||
		*dimension <= 0 || len(encoded) != 4**dimension {
		return nil, "", nil
	}
	return decodeVector(encoded), *model, nil
}

func (s *Store) SemanticNeighbours(
	vector []float32,
	model, excludeItemID string,
	maximum int,
	redacted map[string]bool,
) ([]Item, error) {
	if maximum <= 0 {
		return nil, nil
	}
	// Filtering must happen before the top-k limit. Cached provenance lets the
	// vector scan reject hidden rows before hydrating only the retained items.
	items, ok, err := s.searchVectorFiltered(
		vector, model, maximum,
		func(candidate cachedVector) bool {
			return candidate.id != excludeItemID && !redacted[candidate.sourceMessageID]
		},
	)
	if err != nil || !ok {
		return nil, err
	}
	return items, nil
}
