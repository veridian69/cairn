package memory

import (
	"strings"
	"unicode"
	"unicode/utf8"
)

func QueryPrefix(value string, maxBytes int) string {
	if maxBytes <= 0 {
		return ""
	}
	if len(value) <= maxBytes {
		return value
	}
	cut := maxBytes
	for cut > 0 && !utf8.RuneStart(value[cut]) {
		cut--
	}
	return value[:cut]
}

func BuildFTSQuery(prefix string) string {
	tokens := strings.FieldsFunc(prefix, func(r rune) bool {
		return !unicode.IsLetter(r) && !unicode.IsDigit(r)
	})
	kept := make([]string, 0, min(len(tokens), 32))
	for _, token := range tokens {
		if utf8.RuneCountInString(token) < 2 {
			continue
		}
		kept = append(kept, `"`+token+`"`)
		if len(kept) == 32 {
			break
		}
	}
	return strings.Join(kept, " OR ")
}

func (s *Store) searchBM25(query string, limit int) ([]Item, error) {
	if query == "" || limit <= 0 {
		return nil, nil
	}
	return s.queryItems(`
		SELECT m.id, m.content, m.author_id, m.author_name, m.source_message_id,
		       m.source_created_at, m.reply_to, m.nominated_by, m.pinned, m.created_at
		FROM memories_fts AS f
		JOIN memories AS m ON m.id = f.memory_id
		WHERE memories_fts MATCH ?
		ORDER BY bm25(memories_fts) ASC, m.source_created_at DESC, m.id ASC
		LIMIT ?`, query, limit)
}
