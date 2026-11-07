package attention

import (
	"context"
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

// Profiles and credentials get the import sources' rule (issue #200): no other account may be
// able to replace them or a directory above them.
func TestProfileAndCredentialRefuseReplaceableLocations(t *testing.T) {
	write := func(t *testing.T, dirMode, fileMode os.FileMode) (string, string) {
		t.Helper()
		dir := filepath.Join(t.TempDir(), "profile")
		if err := os.Mkdir(dir, 0700); err != nil {
			t.Fatal(err)
		}
		profile, token := filepath.Join(dir, "profile.json"), filepath.Join(dir, "token")
		if err := os.WriteFile(profile, []byte(validProfile), 0600); err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(token, []byte("synthetic-token\n"), 0600); err != nil {
			t.Fatal(err)
		}
		for _, path := range []string{profile, token} {
			if err := os.Chmod(path, fileMode); err != nil {
				t.Fatal(err)
			}
		}
		if err := os.Chmod(dir, dirMode); err != nil {
			t.Fatal(err)
		}
		return profile, token
	}
	profile, token := write(t, 0770, 0600)
	if _, err := LoadProfile(profile); err == nil {
		t.Fatal("profile in a group-writable directory accepted")
	}
	if _, err := ReadCredential(token); err == nil {
		t.Fatal("credential in a group-writable directory accepted")
	}
	profile, _ = write(t, 0700, 0620)
	if _, err := LoadProfile(profile); err == nil {
		t.Fatal("group-writable profile accepted")
	}
	profile, token = write(t, 0755, 0600)
	if err := os.Chmod(profile, 0644); err != nil {
		t.Fatal(err)
	}
	if _, err := LoadProfile(profile); err != nil {
		t.Fatalf("readable profile refused: %v", err)
	}
	if _, err := ReadCredential(token); err != nil {
		t.Fatalf("private credential refused: %v", err)
	}
}

func TestTrustedCARule(t *testing.T) {
	dir := filepath.Join(t.TempDir(), "tls")
	if err := os.Mkdir(dir, 0700); err != nil {
		t.Fatal(err)
	}
	ca := filepath.Join(dir, "ca.pem")
	if err := os.WriteFile(ca, []byte("pem"), 0600); err != nil {
		t.Fatal(err)
	}
	if err := os.Chmod(ca, 0644); err != nil {
		t.Fatal(err)
	}
	if err := checkTrustedCA(ca); err != nil {
		t.Fatalf("readable CA refused: %v", err)
	}
	if err := checkTrustedCA(""); err != nil {
		t.Fatalf("no CA refused: %v", err)
	}
	if info, err := os.Lstat("/etc/passwd"); err == nil && info.Mode().IsRegular() && info.Mode().Perm()&0022 == 0 {
		if err := checkTrustedCA("/etc/passwd"); err != nil {
			t.Fatalf("root-owned CA refused: %v", err)
		}
	}
	link := filepath.Join(dir, "link.pem")
	if err := os.Symlink(ca, link); err != nil {
		t.Fatal(err)
	}
	if err := checkTrustedCA(link); err == nil {
		t.Fatal("symlinked CA accepted")
	}
	if err := os.Chmod(ca, 0664); err != nil {
		t.Fatal(err)
	}
	if err := checkTrustedCA(ca); err == nil || !strings.Contains(err.Error(), "must not be writable") {
		t.Fatalf("group-writable CA accepted: %v", err)
	}
	_ = os.Chmod(ca, 0644)
	if err := os.Chmod(dir, 0775); err != nil {
		t.Fatal(err)
	}
	if err := checkTrustedCA(ca); err == nil || !strings.Contains(err.Error(), "group- or other-writable") {
		t.Fatalf("CA in a group-writable directory accepted: %v", err)
	}
}

func TestConnectRefusesReplaceableCABeforeDialling(t *testing.T) {
	dir := filepath.Join(t.TempDir(), "profile")
	if err := os.Mkdir(dir, 0700); err != nil {
		t.Fatal(err)
	}
	token, ca := filepath.Join(dir, "token"), filepath.Join(dir, "tls", "ca.pem")
	if err := os.WriteFile(token, []byte("synthetic-token\n"), 0600); err != nil {
		t.Fatal(err)
	}
	if err := os.Mkdir(filepath.Dir(ca), 0700); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(ca, []byte("pem"), 0600); err != nil {
		t.Fatal(err)
	}
	if err := os.Chmod(filepath.Dir(ca), 0777); err != nil {
		t.Fatal(err)
	}
	// The endpoint is never dialled: nothing listens there.
	p := Profile{GardenEndpoint: "https://127.0.0.1:1/mcp", GardenTLSCAFile: ca, CredentialFile: token}
	if _, err := Connect(context.Background(), p); err == nil || !strings.Contains(err.Error(), "TLS CA file") {
		t.Fatalf("replaceable CA reached the dial: %v", err)
	}
}

// The parent check walks the cleaned path; the reader follows the spelling. "link/.." must not let
// a CA in a directory others can write pass as the one beside link (Sol, #200).
func TestTrustedCARefusesNonCanonicalPath(t *testing.T) {
	base := t.TempDir()
	trusted, wide := filepath.Join(base, "trusted"), filepath.Join(base, "wide")
	for _, dir := range []string{trusted, wide, filepath.Join(wide, "sub")} {
		if err := os.Mkdir(dir, 0700); err != nil {
			t.Fatal(err)
		}
	}
	if err := os.WriteFile(filepath.Join(wide, "ca.pem"), []byte("pem"), 0600); err != nil {
		t.Fatal(err)
	}
	if err := os.Chmod(wide, 0777); err != nil {
		t.Fatal(err)
	}
	if err := os.Symlink(filepath.Join(wide, "sub"), filepath.Join(trusted, "link")); err != nil {
		t.Fatal(err)
	}
	// The kernel reads wide/ca.pem; filepath.Clean says trusted/ca.pem.
	spelled := trusted + "/link/../ca.pem"
	if err := checkTrustedCA(spelled); err == nil {
		t.Fatal("CA reached through link/.. into a writable directory accepted")
	}
	for _, path := range []string{"relative/ca.pem", trusted + "/./ca.pem", trusted + "//ca.pem"} {
		if err := checkTrustedCA(path); err == nil {
			t.Fatalf("non-canonical CA path accepted: %s", path)
		}
	}
}
