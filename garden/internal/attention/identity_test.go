package attention

import (
	"encoding/json"
	"os"
	"path/filepath"
	"reflect"
	"strings"
	"testing"
)

// Throwaway fixture: an invented token, never a real credential.
const identityFixtureToken = "fixture-identity-token-new1"

const homeWithoutDefaults = `{"garden_endpoint":"https://garden.example/mcp","garden_tls_ca_file":"tls/internal-ca.pem","garden_tls_server_name":"garden.example","instance_id":"33333333-3333-4333-8333-333333333333","scope":{"realm":"example","segments":[{"kind":"project","identifier":"garden"}]},"classification":"internal"}`

// privateHome returns a temporary Garden home with the 0700 mode the runtime requires;
// t.TempDir alone is 0755 in this environment.
func privateHome(t *testing.T) string {
	t.Helper()
	dir := t.TempDir()
	if err := os.Chmod(dir, 0700); err != nil {
		t.Fatal(err)
	}
	return dir
}

// sourceToken writes an issued-token file in its own directory and returns its path.
func sourceToken(t *testing.T, body string, mode os.FileMode) string {
	t.Helper()
	dir := privateHome(t)
	path := filepath.Join(dir, "issued.token")
	if err := os.WriteFile(path, []byte(body), mode); err != nil {
		t.Fatal(err)
	}
	if err := os.Chmod(path, mode); err != nil {
		t.Fatal(err)
	}
	return path
}

// fieldsOf decodes garden.json into a generic map for comparison.
func fieldsOf(t *testing.T, dir string) map[string]any {
	t.Helper()
	data, err := os.ReadFile(filepath.Join(dir, "garden.json"))
	if err != nil {
		t.Fatal(err)
	}
	var out map[string]any
	if err := json.Unmarshal(data, &out); err != nil {
		t.Fatal(err)
	}
	return out
}

func TestAddIdentityInstallsTokenAndSetsDefault(t *testing.T) {
	dir := privateHome(t)
	writeHome(t, dir, homeWithoutDefaults)
	before := fieldsOf(t, dir)
	src := sourceToken(t, identityFixtureToken+"\n", 0600)

	result, err := AddIdentity(dir, "new1", src, "codex")
	if err != nil {
		t.Fatalf("add: %v", err)
	}
	if result.Present || !result.DefaultSet || len(result.Written) != 1 {
		t.Fatalf("unexpected result: %+v", result)
	}
	home, err := LoadHome(dir)
	if err != nil {
		t.Fatalf("garden.json does not load: %v", err)
	}
	if home.Config.Defaults["codex"] != "new1" {
		t.Fatalf("codex default not set: %v", home.Config.Defaults)
	}
	value, err := ReadCredential(home.TokenFile("new1"))
	if err != nil || value != identityFixtureToken {
		t.Fatalf("installed token not readable: %v", err)
	}
	if got := mode(t, home.TokenFile("new1")); got != 0600 {
		t.Fatalf("token mode %o", got)
	}
	if got := mode(t, filepath.Join(dir, "identities")); got != 0700 {
		t.Fatalf("identities mode %o", got)
	}
	if got := mode(t, filepath.Join(dir, "garden.json")); got != 0600 {
		t.Fatalf("garden.json mode %o", got)
	}

	after := fieldsOf(t, dir)
	want := map[string]any{"defaults": map[string]any{"codex": "new1"}}
	for key, value := range before {
		if key != "defaults" && !reflect.DeepEqual(after[key], value) {
			t.Fatalf("field %s changed", key)
		}
	}
	if !reflect.DeepEqual(after["defaults"], want["defaults"]) {
		t.Fatalf("defaults wrong: %v", after["defaults"])
	}
	if _, err := os.Stat(filepath.Join(dir, "garden.json.tmp")); err == nil {
		t.Fatal("temporary file left behind")
	}
}

func TestAddIdentityKeepsExistingDefaultsAndFields(t *testing.T) {
	dir := privateHome(t)
	writeHome(t, dir, validHomeConfig)
	src := sourceToken(t, identityFixtureToken, 0600)
	if _, err := AddIdentity(dir, "new1", src, "opencode"); err == nil || !strings.Contains(err.Error(), "already defaults to zen") {
		t.Fatalf("existing default changed silently: %v", err)
	}
	if _, err := os.Stat(filepath.Join(dir, "identities", "new1.token")); err == nil {
		t.Fatal("token written although the default was refused")
	}
	if _, err := AddIdentity(dir, "new1", src, ""); err != nil {
		t.Fatalf("add without default: %v", err)
	}
	home, err := LoadHome(dir)
	if err != nil || home.Config.Defaults["claude"] != "spike" || home.Config.Defaults["opencode"] != "zen" {
		t.Fatalf("defaults disturbed: %v %v", home.Config.Defaults, err)
	}
}

func TestAddIdentityPresentWhenBytesMatch(t *testing.T) {
	dir := privateHome(t)
	writeHome(t, dir, homeWithoutDefaults)
	src := sourceToken(t, identityFixtureToken, 0600)
	if _, err := AddIdentity(dir, "new1", src, ""); err != nil {
		t.Fatal(err)
	}
	again, err := AddIdentity(dir, "new1", src, "codex")
	if err != nil {
		t.Fatalf("re-run: %v", err)
	}
	if !again.Present || len(again.Written) != 0 || !again.DefaultSet {
		t.Fatalf("re-run should be already present and still set the default: %+v", again)
	}
	third, err := AddIdentity(dir, "new1", src, "codex")
	if err != nil || !third.Present || third.DefaultSet {
		t.Fatalf("second default run should change nothing: %+v %v", third, err)
	}
}

func TestAddIdentityRefusesDifferentExistingToken(t *testing.T) {
	dir := privateHome(t)
	writeHome(t, dir, homeWithoutDefaults)
	first := sourceToken(t, identityFixtureToken, 0600)
	if _, err := AddIdentity(dir, "new1", first, ""); err != nil {
		t.Fatal(err)
	}
	other := sourceToken(t, "fixture-other-token", 0600)
	if _, err := AddIdentity(dir, "new1", other, ""); err == nil || !strings.Contains(err.Error(), "refusing to overwrite") {
		t.Fatalf("different token overwrote identity: %v", err)
	}
	home, _ := LoadHome(dir)
	if value, _ := ReadCredential(home.TokenFile("new1")); value != identityFixtureToken {
		t.Fatal("existing token changed")
	}
}

func TestAddIdentityRefusesUnsafeSources(t *testing.T) {
	dir := privateHome(t)
	writeHome(t, dir, homeWithoutDefaults)
	group := sourceToken(t, identityFixtureToken, 0640)
	link := filepath.Join(t.TempDir(), "link.token")
	if err := os.Symlink(sourceToken(t, identityFixtureToken, 0600), link); err != nil {
		t.Fatal(err)
	}
	empty := sourceToken(t, "\n", 0600)
	for name, path := range map[string]string{
		"group-readable": group,
		"symlink":        link,
		"empty":          empty,
		"missing":        filepath.Join(t.TempDir(), "absent.token"),
	} {
		if _, err := AddIdentity(dir, "new1", path, ""); err == nil {
			t.Fatalf("%s source accepted", name)
		}
	}
	if _, err := os.Stat(filepath.Join(dir, "identities")); err == nil {
		t.Fatal("refused add still created identities")
	}
}

func TestAddIdentityRefusesInvalidNamesAndHarnesses(t *testing.T) {
	dir := privateHome(t)
	writeHome(t, dir, homeWithoutDefaults)
	src := sourceToken(t, identityFixtureToken, 0600)
	for _, name := range []string{"", "../x", "a/b", ".hidden", "bad name"} {
		if _, err := AddIdentity(dir, name, src, ""); err == nil {
			t.Fatalf("name %q accepted", name)
		}
	}
	if _, err := AddIdentity(dir, "new1", src, "stdio"); err == nil {
		t.Fatal("unknown harness accepted")
	}
}

func TestAddIdentityNeedsGardenJSONFirst(t *testing.T) {
	dir := privateHome(t)
	src := sourceToken(t, identityFixtureToken, 0600)
	_, err := AddIdentity(dir, "new1", src, "")
	if err == nil || !strings.Contains(err.Error(), "garden home import") {
		t.Fatalf("missing garden.json not explained: %v", err)
	}
	if _, statErr := os.Stat(filepath.Join(dir, "identities")); statErr == nil {
		t.Fatal("token written without garden.json")
	}
}

func TestAddIdentityRefusesTokenUnderWritableParent(t *testing.T) {
	dir := privateHome(t)
	writeHome(t, dir, homeWithoutDefaults)
	shared := filepath.Join(t.TempDir(), "shared")
	if err := os.Mkdir(shared, 0700); err != nil {
		t.Fatal(err)
	}
	src := filepath.Join(shared, "issued.token")
	if err := os.WriteFile(src, []byte(identityFixtureToken+"\n"), 0600); err != nil {
		t.Fatal(err)
	}
	// Another group member could swap shared's entries between the check and the open.
	if err := os.Chmod(shared, 0770); err != nil {
		t.Fatal(err)
	}
	if _, err := AddIdentity(dir, "new1", src, ""); err == nil || !strings.Contains(err.Error(), "group- or other-writable") {
		t.Fatalf("token under a group-writable parent: want refusal, got %v", err)
	}
	if _, err := os.Lstat(filepath.Join(dir, "identities", "new1.token")); !os.IsNotExist(err) {
		t.Fatalf("refused token was installed: %v", err)
	}
}
