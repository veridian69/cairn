package memory

import (
	"strings"
	"testing"
	"unicode/utf8"
)

func TestQueryConstructionIsBoundedRuneSafeAndOperatorFree(t *testing.T) {
	prefix := QueryPrefix(strings.Repeat("é", 700), 1023)
	if len(prefix) > 1023 || !utf8.ValidString(prefix) {
		t.Fatalf("invalid prefix: bytes=%d valid=%v", len(prefix), utf8.ValidString(prefix))
	}
	query := BuildFTSQuery(`fractal AND (gap OR minds) NEAR "quote" col:x ^start -neg wild*`)
	for _, forbidden := range []string{"(", ")", ":", "^", "*", "-"} {
		if strings.Contains(query, forbidden) {
			t.Fatalf("operator %q survived in %q", forbidden, query)
		}
	}
	if !strings.Contains(query, `"fractal"`) || strings.Count(query, " OR ") >= 32 {
		t.Fatalf("bad FTS query: %q", query)
	}
}

func TestFTSQuerySkipsUnusableInputAndCapsUsefulTokens(t *testing.T) {
	for _, input := range []string{"!!! -- : ^ *", "🧠✨"} {
		if got := BuildFTSQuery(input); got != "" {
			t.Fatalf("BuildFTSQuery(%q) = %q, want empty", input, got)
		}
	}
	var tokens []string
	for i := 0; i < 40; i++ {
		tokens = append(tokens, "word"+string(rune('a'+i%26)))
	}
	query := BuildFTSQuery("a I " + strings.Join(tokens, " "))
	if strings.Contains(query, `"a"`) || strings.Contains(query, `"I"`) {
		t.Fatalf("single-character token survived: %q", query)
	}
	if got := strings.Count(query, " OR ") + 1; got != 32 {
		t.Fatalf("token count = %d, want 32: %q", got, query)
	}
}
