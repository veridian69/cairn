package memory

import (
	"context"
	"encoding/binary"
	"errors"
	"fmt"
	"log"
	"math"
	"sort"
)

var ErrInvalidVector = errors.New("embedding vector is empty")

func encodeVector(vector []float32) []byte {
	encoded := make([]byte, 4*len(vector))
	for i, value := range vector {
		binary.LittleEndian.PutUint32(encoded[i*4:], math.Float32bits(value))
	}
	return encoded
}

func decodeVector(encoded []byte) []float32 {
	vector := make([]float32, len(encoded)/4)
	for i := range vector {
		vector[i] = math.Float32frombits(binary.LittleEndian.Uint32(encoded[i*4:]))
	}
	return vector
}

func cosine(a, b []float32) float64 {
	if len(a) == 0 || len(a) != len(b) {
		return 0
	}
	var dot, normA, normB float64
	for i := range a {
		dot += float64(a[i]) * float64(b[i])
		normA += float64(a[i]) * float64(a[i])
		normB += float64(b[i]) * float64(b[i])
	}
	if normA == 0 || normB == 0 {
		return 0
	}
	return dot / (math.Sqrt(normA) * math.Sqrt(normB))
}

func (s *Store) SetEmbedding(id string, vector []float32, model string) error {
	if len(vector) == 0 {
		return ErrInvalidVector
	}
	if model == "" {
		return fmt.Errorf("embedding model is required")
	}
	result, err := s.db.Exec(`UPDATE memories
		SET embedding = ?, embedding_model = ?, embedding_dim = ?
		WHERE id = ?`, encodeVector(vector), model, len(vector), id)
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
	s.invalidateCache()
	return nil
}

func (s *Store) PendingEmbeddings(model string, limit int) ([]Item, error) {
	return s.queryItems(`SELECT `+itemColumns+` FROM memories
		WHERE embedding IS NULL OR embedding_model IS NULL OR embedding_model != ?
		ORDER BY rowid ASC LIMIT ?`, model, limit)
}

func (s *Store) PendingEmbeddingsPage(model string, after int64, limit int) ([]Item, int64, error) {
	rows, err := s.db.Query(`SELECT rowid, `+itemColumns+` FROM memories
		WHERE rowid > ? AND
		      (embedding IS NULL OR embedding_model IS NULL OR embedding_model != ?)
		ORDER BY rowid ASC LIMIT ?`, after, model, limit)
	if err != nil {
		return nil, after, err
	}
	defer rows.Close()
	items := make([]Item, 0, limit)
	last := after
	for rows.Next() {
		var rowID int64
		var item Item
		var pinned int
		if err := rows.Scan(
			&rowID, &item.ID, &item.Content, &item.AuthorID, &item.AuthorName,
			&item.SourceMessageID, &item.SourceCreatedAt, &item.ReplyTo,
			&item.NominatedBy, &pinned, &item.CreatedAt,
		); err != nil {
			return nil, after, err
		}
		item.Pinned = pinned == 1
		items = append(items, item)
		last = rowID
	}
	return items, last, rows.Err()
}

func (s *Store) generation() (int64, error) {
	var generation int64
	err := s.genConn.QueryRowContext(context.Background(), `PRAGMA data_version`).Scan(&generation)
	return generation, err
}

func (s *Store) vectors(model string) ([]cachedVector, bool) {
	s.cacheMu.Lock()
	defer s.cacheMu.Unlock()
	generation, err := s.generation()
	if err != nil {
		log.Printf("memory: generation read failed: %v", err)
		return nil, false
	}
	if s.cache != nil && s.cacheGen == generation && s.cacheModel == model {
		return s.cache, true
	}
	for range 2 {
		before, err := s.generation()
		if err != nil {
			return nil, false
		}
		vectors, skipped, err := s.loadVectors(model)
		if err != nil {
			log.Printf("memory: vector load failed: %v", err)
			return nil, false
		}
		if s.afterVectorLoad != nil {
			s.afterVectorLoad()
		}
		after, err := s.generation()
		if err != nil {
			return nil, false
		}
		if before == after {
			if skipped > 0 {
				log.Printf("memory: skipped %d malformed vector rows", skipped)
			}
			s.cache, s.cacheGen, s.cacheModel = vectors, after, model
			return vectors, true
		}
	}
	log.Printf("memory: vector cache churn, skipping vector arm this turn")
	return nil, false
}

func (s *Store) loadVectors(model string) ([]cachedVector, int, error) {
	rows, err := s.db.Query(`SELECT id, source_message_id, source_created_at, embedding, embedding_dim
		FROM memories WHERE embedding IS NOT NULL AND embedding_model = ?`, model)
	if err != nil {
		return nil, 0, err
	}
	defer rows.Close()
	vectors := make([]cachedVector, 0)
	skipped := 0
	for rows.Next() {
		var cached cachedVector
		var encoded []byte
		var dimension int
		if err := rows.Scan(
			&cached.id, &cached.sourceMessageID, &cached.sourceCreatedAt,
			&encoded, &dimension,
		); err != nil {
			return nil, 0, err
		}
		if dimension <= 0 || len(encoded) != 4*dimension {
			skipped++
			continue
		}
		cached.vec = decodeVector(encoded)
		vectors = append(vectors, cached)
	}
	return vectors, skipped, rows.Err()
}

func (s *Store) searchVector(queryVector []float32, model string, limit int) ([]Item, bool, error) {
	return s.searchVectorFiltered(queryVector, model, limit, nil)
}

func (s *Store) searchVectorFiltered(
	queryVector []float32,
	model string,
	limit int,
	accept func(cachedVector) bool,
) ([]Item, bool, error) {
	if len(queryVector) == 0 || model == "" || limit <= 0 {
		return nil, true, nil
	}
	vectors, ok := s.vectors(model)
	if !ok {
		return nil, false, nil
	}
	type scored struct {
		vector cachedVector
		score  float64
	}
	candidates := make([]scored, 0, len(vectors))
	dimensionSkipped := 0
	for _, vector := range vectors {
		if len(vector.vec) != len(queryVector) {
			dimensionSkipped++
			continue
		}
		candidates = append(candidates, scored{vector: vector, score: cosine(queryVector, vector.vec)})
	}
	if dimensionSkipped > 0 {
		log.Printf("memory: skipped %d vectors with mismatched dimension", dimensionSkipped)
	}
	sort.Slice(candidates, func(i, j int) bool {
		if candidates[i].score != candidates[j].score {
			return candidates[i].score > candidates[j].score
		}
		if !candidates[i].vector.sourceCreatedAt.Equal(candidates[j].vector.sourceCreatedAt) {
			return candidates[i].vector.sourceCreatedAt.After(candidates[j].vector.sourceCreatedAt)
		}
		return candidates[i].vector.id < candidates[j].vector.id
	})
	items := make([]Item, 0, limit)
	for _, candidate := range candidates {
		if accept != nil && !accept(candidate.vector) {
			continue
		}
		item, err := s.GetByID(candidate.vector.id)
		if errors.Is(err, ErrNotFound) {
			continue
		}
		if err != nil {
			return nil, true, err
		}
		items = append(items, item)
		if len(items) == limit {
			break
		}
	}
	return items, true, nil
}
