package attention

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
)

const validProfile = `{"garden_endpoint":"http://127.0.0.1:8090/mcp","credential_file":"token","instance_id":"11111111-1111-4111-8111-111111111111","scope":{"realm":"example","segments":[{"kind":"project","identifier":"garden"}]},"classification":"internal","participant":"Val","adapter":"codex","session_id":"thread-id","codex_socket":"/tmp/test-codex.sock"}`

func TestProfileUsesOnlyExplicitCredentialFileAndPinnedBinding(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "profile.json")
	if err := os.WriteFile(path, []byte(validProfile), 0600); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(dir, "token"), []byte("synthetic-token\n"), 0600); err != nil {
		t.Fatal(err)
	}
	p, err := LoadProfile(path)
	if err != nil {
		t.Fatal(err)
	}
	if p.CredentialFile != filepath.Join(dir, "token") || p.Participant != "Val" {
		t.Fatalf("incorrect profile: %#v", p)
	}
	token, err := ReadCredential(p.CredentialFile)
	if err != nil || token != "synthetic-token" {
		t.Fatalf("credential file: %v", err)
	}
}

func TestProfileRejectsAmbiguousAndInsecureConfiguration(t *testing.T) {
	for _, text := range []string{
		strings.Replace(validProfile, `"adapter":"codex"`, `"adapter":"codex","adapter":"claude"`, 1),
		strings.Replace(validProfile, `"adapter":"codex"`, `"Adapter":"codex"`, 1),
		strings.Replace(validProfile, `http://127.0.0.1:8090/mcp`, `http://garden.example/mcp`, 1),
		strings.Replace(validProfile, `"participant":"Val"`, `"participant":""`, 1),
		strings.Replace(validProfile, `"instance_id":"11111111-1111-4111-8111-111111111111"`, `"instance_id":"unknown"`, 1),
	} {
		path := filepath.Join(t.TempDir(), "profile.json")
		_ = os.WriteFile(path, []byte(text), 0600)
		if _, err := LoadProfile(path); err == nil {
			t.Fatal("invalid profile accepted")
		}
	}
}

func TestCredentialRejectsSymlinksAndBroadPermissions(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "token")
	_ = os.WriteFile(path, []byte("credential"), 0644)
	if _, err := ReadCredential(path); err == nil {
		t.Fatal("world-readable credential accepted")
	}
	_ = os.Chmod(path, 0600)
	link := filepath.Join(dir, "link")
	if err := os.Symlink(path, link); err != nil {
		t.Fatal(err)
	}
	if _, err := ReadCredential(link); err == nil {
		t.Fatal("symlink credential accepted")
	}
}

func TestProfileTLSVerificationOverridesAreExplicit(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "profile.json")
	raw := strings.Replace(validProfile, "http://127.0.0.1:8090/mcp", "https://127.0.0.1:9443/mcp", 1)
	raw = strings.TrimSuffix(raw, "}") + `,"garden_tls_ca_file":"tls/ca.crt","garden_tls_server_name":"garden.example.test"}`
	if err := os.WriteFile(path, []byte(raw), 0600); err != nil {
		t.Fatal(err)
	}
	p, err := LoadProfile(path)
	if err != nil {
		t.Fatal(err)
	}
	if p.GardenTLSCAFile != filepath.Join(dir, "tls", "ca.crt") || p.GardenTLSServerName != "garden.example.test" {
		t.Fatal("TLS profile overrides were lost")
	}
	raw = strings.Replace(raw, "https://127.0.0.1:9443/mcp", "http://127.0.0.1:9443/mcp", 1)
	_ = os.WriteFile(path, []byte(raw), 0600)
	if _, err := LoadProfile(path); err == nil {
		t.Fatal("TLS profile overrides were silently ignored for HTTP")
	}
}
