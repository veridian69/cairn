package cmd

import (
	"bytes"
	"context"
	"path/filepath"
	"reflect"
	"strings"
	"testing"
)

// A SIGHUP reload that LoadConfig rejects must leave the running principals in force and say so once.
func TestReloadFromFileRefusalKeepsRunningPrincipals(t *testing.T) {
	path := filepath.Join(t.TempDir(), "garden.json")
	current := map[string]string{"11111111-1111-4111-8111-111111111111": "alice"}
	var out bytes.Buffer
	got := reloadFromFile(context.Background(), nil, path, "test", current, &out)
	if !reflect.DeepEqual(got, current) {
		t.Fatalf("refused reload changed principals: %v", got)
	}
	line := out.String()
	if strings.Count(line, "\n") != 1 || !strings.Contains(line, "refused, keeping the running configuration") {
		t.Fatalf("unexpected reload log: %q", line)
	}
}
