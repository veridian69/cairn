package credentials

import (
	"bytes"
	"os"
	"path/filepath"
	"testing"

	"github.com/veridian69/cairn/cairn-mcp/internal/config"
)

func TestLoadSnapshotReadsFiles(t *testing.T) {
	t.Helper()
	base := t.TempDir()
	cfg := config.RelayConfig{
		LocalTokenPath:     writeSecret(t, base, "token", []byte("relay-token\n"), 0600),
		CFClientIDPath:     writeSecret(t, base, "client-id", []byte("client-id\n"), 0600),
		CFClientSecretPath: writeSecret(t, base, "client-secret", []byte("client-secret\n"), 0600),
	}

	store, err := NewCredentialStore(cfg)
	if err != nil {
		t.Fatalf("NewCredentialStore() unexpected error: %v", err)
	}

	snapshot := store.Snapshot()
	if got, want := string(snapshot.LocalToken), "relay-token"; got != want {
		t.Fatalf("local token %q, want %q", got, want)
	}
	if got, want := snapshot.CFClientID, "client-id"; got != want {
		t.Fatalf("cf client id %q, want %q", got, want)
	}
	if got, want := snapshot.CFClientSecret, "client-secret"; got != want {
		t.Fatalf("cf client secret %q, want %q", got, want)
	}
}

func TestNewAccessStoreDoesNotRequireLocalToken(t *testing.T) {
	dir := t.TempDir()
	idPath := writeSecret(t, dir, "client-id", []byte("id"), 0600)
	secretPath := writeSecret(t, dir, "client-secret", []byte("secret"), 0600)

	store, err := NewAccessStore(idPath, secretPath)
	if err != nil {
		t.Fatal(err)
	}
	got := store.AccessSnapshot()
	if got.ClientID != "id" || got.ClientSecret != "secret" {
		t.Fatalf("AccessSnapshot() = %#v", got)
	}
}

func TestNewRelayStoreRequiresLocalToken(t *testing.T) {
	dir := t.TempDir()
	idPath := writeSecret(t, dir, "client-id", []byte("id"), 0600)
	secretPath := writeSecret(t, dir, "client-secret", []byte("secret"), 0600)

	_, err := NewRelayStore(idPath, secretPath, filepath.Join(dir, "missing-token"))
	if err == nil {
		t.Fatal("NewRelayStore() succeeded without a local token")
	}
}

func TestNewRelayStoreRejectsEmptyLocalTokenPath(t *testing.T) {
	dir := t.TempDir()
	idPath := writeSecret(t, dir, "client-id", []byte("id"), 0600)
	secretPath := writeSecret(t, dir, "client-secret", []byte("secret"), 0600)

	_, err := NewRelayStore(idPath, secretPath, "")
	if err == nil {
		t.Fatal("NewRelayStore() succeeded with an empty local token path")
	}
}

func TestAccessStoreReloadsCredentialsWithoutLocalToken(t *testing.T) {
	dir := t.TempDir()
	idPath := writeSecretNoTruncate(t, dir, "client-id", []byte("first-id"), 0600)
	secretPath := writeSecretNoTruncate(t, dir, "client-secret", []byte("first-secret"), 0600)

	store, err := NewAccessStore(idPath, secretPath)
	if err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(idPath, []byte("second-id"), 0600); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(secretPath, []byte("second-secret"), 0600); err != nil {
		t.Fatal(err)
	}
	if err := store.Reload(); err != nil {
		t.Fatal(err)
	}

	got := store.AccessSnapshot()
	if got.ClientID != "second-id" || got.ClientSecret != "second-secret" {
		t.Fatalf("AccessSnapshot() after reload = %#v", got)
	}
}

func TestReadSecretFileRejectsEmptyOrOversized(t *testing.T) {
	t.Helper()
	base := t.TempDir()
	emptyPath := writeSecret(t, base, "empty", []byte("\n"), 0600)
	if _, err := ReadSecretFile(emptyPath, "local token", os.Geteuid(), 4096); err == nil {
		t.Fatal("expected error for empty file")
	}

	tooLargePath := writeSecret(t, base, "large", bytes.Repeat([]byte("x"), 4097), 0600)
	if _, err := ReadSecretFile(tooLargePath, "local token", os.Geteuid(), 4096); err == nil {
		t.Fatal("expected error for oversized file")
	}
}

func TestLoadSnapshotRejectsNonUTF8CloudflareCredentials(t *testing.T) {
	t.Helper()
	base := t.TempDir()
	cfg := config.RelayConfig{
		LocalTokenPath:     writeSecret(t, base, "token", []byte("relay-token"), 0600),
		CFClientIDPath:     writeSecret(t, base, "client-id", []byte{0xff, 0xfe, 0x00}, 0600),
		CFClientSecretPath: writeSecret(t, base, "client-secret", []byte("client-secret"), 0600),
	}

	_, err := NewCredentialStore(cfg)
	if err == nil {
		t.Fatal("expected error for invalid utf8")
	}
	if _, ok := err.(*CredentialError); !ok {
		t.Fatalf("expected CredentialError, got: %T: %v", err, err)
	}
	if got, want := err.Error(), "Cloudflare credential must be UTF-8"; got != want {
		t.Fatalf("error %q, want %q", got, want)
	}
}

func TestCredentialStoreReloadUpdatesToken(t *testing.T) {
	t.Helper()
	base := t.TempDir()
	tokenPath := writeSecretNoTruncate(t, base, "token", []byte("first"), 0600)
	cfg := config.RelayConfig{
		LocalTokenPath:     tokenPath,
		CFClientIDPath:     writeSecret(t, base, "client-id", []byte("client-id"), 0600),
		CFClientSecretPath: writeSecret(t, base, "client-secret", []byte("client-secret"), 0600),
	}
	store, err := NewCredentialStore(cfg)
	if err != nil {
		t.Fatalf("NewCredentialStore() unexpected error: %v", err)
	}
	if got := string(store.Snapshot().LocalToken); got != "first" {
		t.Fatalf("token %q, want %q", got, "first")
	}

	if err := os.WriteFile(tokenPath, []byte("second"), 0600); err != nil {
		t.Fatalf("os.WriteFile() unexpected error: %v", err)
	}
	if err := store.Reload(); err != nil {
		t.Fatalf("Reload() unexpected error: %v", err)
	}
	if got := string(store.Snapshot().LocalToken); got != "second" {
		t.Fatalf("token %q, want %q", got, "second")
	}
}

func TestTokenMatches(t *testing.T) {
	if !TokenMatches([]byte("alpha"), []byte("alpha")) {
		t.Fatal("expected matching token to match")
	}
	if TokenMatches([]byte("alpha"), []byte("beta")) {
		t.Fatal("expected different token to fail")
	}
}

func writeSecretNoTruncate(t *testing.T, base, name string, value []byte, mode os.FileMode) string {
	return writeSecret(t, base, name, value, mode)
}
