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
	if err := os.WriteFile(path, []byte("credential"), 0600); err != nil {
		t.Fatal(err)
	}
	// Chmod, not the create mode, so the umask cannot narrow it.
	if err := os.Chmod(path, 0644); err != nil {
		t.Fatal(err)
	}
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

func TestProfileOpenCodeAPIVersionIsExplicit(t *testing.T) {
	opencode := strings.Replace(validProfile, `"adapter":"codex","session_id":"thread-id","codex_socket":"/tmp/test-codex.sock"`, `"adapter":"opencode","session_id":"ses_target","host_endpoint":"http://127.0.0.1:4096"`, 1)
	load := func(text string) (Profile, error) {
		path := filepath.Join(t.TempDir(), "profile.json")
		if err := os.WriteFile(path, []byte(text), 0600); err != nil {
			t.Fatal(err)
		}
		return LoadProfile(path)
	}
	for version, want := range map[string]string{"": "1", "1": "1", "2": "2"} {
		text := opencode
		if version != "" {
			text = strings.Replace(opencode, `"host_endpoint":"http://127.0.0.1:4096"`, `"host_endpoint":"http://127.0.0.1:4096","host_api_version":"`+version+`"`, 1)
		}
		p, err := load(text)
		if err != nil || p.HostAPIVersion != want {
			t.Fatalf("version %q: got %q, %v", version, p.HostAPIVersion, err)
		}
	}
	if _, err := load(strings.Replace(opencode, `"host_endpoint"`, `"host_api_version":"3","host_endpoint"`, 1)); err == nil {
		t.Fatal("unknown OpenCode API version accepted")
	}
	if _, err := load(strings.Replace(validProfile, `"session_id":"thread-id"`, `"session_id":"thread-id","host_api_version":"2"`, 1)); err == nil {
		t.Fatal("API version accepted for a non-OpenCode adapter")
	}
	for _, text := range []string{
		strings.Replace(opencode, `"host_endpoint"`, `"host_api_version":null,"host_endpoint"`, 1),
		strings.Replace(validProfile, `"session_id":"thread-id"`, `"session_id":"thread-id","host_api_version":null`, 1),
	} {
		if _, err := load(text); err == nil {
			t.Fatal("explicit null API version accepted")
		}
	}
}

func TestProfileOpenCodeExampleLoads(t *testing.T) {
	p, err := LoadProfile(filepath.Join("..", "..", "deploy", "opencode.profile.example.json"))
	if err != nil {
		t.Fatal(err)
	}
	if p.HostAPIVersion != "2" {
		t.Fatalf("example API version %q", p.HostAPIVersion)
	}
}
