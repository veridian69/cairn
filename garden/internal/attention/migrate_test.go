package attention

import (
	"bytes"
	"encoding/json"
	"errors"
	"os"
	"path/filepath"
	"reflect"
	"strings"
	"testing"
)

// Throwaway fixtures only: every token and CA here is invented for the test.
const fixtureInstance = "44444444-4444-4444-8444-444444444444"
const fixtureCA = "-----BEGIN CERTIFICATE-----\nZml4dHVyZQ==\n-----END CERTIFICATE-----\n"

type legacyFixture struct {
	t   *testing.T
	src string
}

func newLegacyFixture(t *testing.T) legacyFixture {
	t.Helper()
	return newLegacyFixtureIn(t, t.TempDir())
}

// newLegacyFixtureIn builds the legacy layout under base/legacy-garden.
func newLegacyFixtureIn(t *testing.T, base string) legacyFixture {
	t.Helper()
	src := filepath.Join(base, "legacy-garden")
	for _, dir := range []string{"credentials", "tls"} {
		if err := os.MkdirAll(filepath.Join(src, dir), 0700); err != nil {
			t.Fatal(err)
		}
	}
	f := legacyFixture{t: t, src: src}
	f.write("tls/internal-ca.pem", fixtureCA, 0600)
	f.token("spike")
	f.token("val")
	f.token("zen")
	f.profile("spike", "claude", nil)
	f.profile("val", "codex", nil)
	f.profile("zen", "opencode", nil)
	return f
}

func (f legacyFixture) write(rel, body string, mode os.FileMode) {
	f.t.Helper()
	if err := os.WriteFile(filepath.Join(f.src, rel), []byte(body), mode); err != nil {
		f.t.Fatal(err)
	}
}

func (f legacyFixture) token(name string) {
	f.write("credentials/"+name+".token", "fixture-token-"+name+"\n", 0600)
}

// profile writes <name>.json; overrides replace top-level fields for mismatch tests.
func (f legacyFixture) profile(name, adapter string, overrides map[string]any) {
	f.t.Helper()
	p := map[string]any{
		"garden_endpoint":        "https://garden.example/mcp",
		"garden_tls_ca_file":     "tls/internal-ca.pem",
		"garden_tls_server_name": "garden.example",
		"credential_file":        "credentials/" + name + ".token",
		"instance_id":            fixtureInstance,
		"scope":                  map[string]any{"realm": "example", "segments": []any{map[string]any{"kind": "project", "identifier": "garden"}}},
		"classification":         "internal",
		"participant":            name,
		"adapter":                adapter,
	}
	switch adapter {
	case "codex":
		p["session_id"] = "task-1"
		p["codex_socket"] = "/run/codex/control.sock"
	case "opencode":
		p["session_id"] = "session-1"
		p["host_endpoint"] = "http://127.0.0.1:4096"
	}
	for k, v := range overrides {
		p[k] = v
	}
	data, err := json.Marshal(p)
	if err != nil {
		f.t.Fatal(err)
	}
	f.write(name+".json", string(data), 0644)
}

func (f legacyFixture) snapshot() map[string]string {
	f.t.Helper()
	files := map[string]string{}
	err := filepath.WalkDir(f.src, func(path string, d os.DirEntry, err error) error {
		if err != nil || d.IsDir() {
			return err
		}
		data, err := os.ReadFile(path)
		files[path] = string(data)
		return err
	})
	if err != nil {
		f.t.Fatal(err)
	}
	return files
}

func newDest(t *testing.T) string {
	t.Helper()
	return filepath.Join(t.TempDir(), "garden-home")
}

func mode(t *testing.T, path string) os.FileMode {
	t.Helper()
	info, err := os.Lstat(path)
	if err != nil {
		t.Fatal(err)
	}
	return info.Mode().Perm()
}

func TestImportLegacyHomeMigratesProfilesOnce(t *testing.T) {
	f := newLegacyFixture(t)
	f.write("credentials/opencode-server.password", "fixture-password\n", 0600)
	f.write("spike.json.bak-20261018", "{}", 0644)
	before := f.snapshot()
	dest := newDest(t)

	result, err := ImportLegacyHome(f.src, dest, nil)
	if err != nil {
		t.Fatalf("import: %v", err)
	}
	if result.Already {
		t.Fatal("first import reported already imported")
	}
	home, err := LoadHome(dest)
	if err != nil {
		t.Fatalf("imported garden.json does not load: %v", err)
	}
	if home.Config.Defaults["claude"] != "spike" || home.Config.Defaults["codex"] != "val" || home.Config.Defaults["opencode"] != "zen" {
		t.Fatalf("defaults wrong: %v", home.Config.Defaults)
	}
	if home.Config.GardenTLSCAFile != filepath.Join(dest, "tls", "internal-ca.pem") {
		t.Fatalf("CA not resolved into the home: %q", home.Config.GardenTLSCAFile)
	}
	for _, name := range []string{"spike", "val", "zen"} {
		value, err := ReadCredential(home.TokenFile(name))
		if err != nil || value != "fixture-token-"+name {
			t.Fatalf("token %s not readable: %v", name, err)
		}
		if got := mode(t, home.TokenFile(name)); got != 0600 {
			t.Fatalf("token %s mode %o", name, got)
		}
	}
	if got := mode(t, filepath.Join(dest, "identities")); got != 0700 {
		t.Fatalf("identities dir mode %o", got)
	}
	if !result.hasNotMigrated("opencode-server.password") {
		t.Fatalf("obsolete password not reported: %v", result.NotMigrated)
	}
	if !slicesContains(result.Ignored, "spike.json.bak-20261018") {
		t.Fatalf("backup file not ignored: %v", result.Ignored)
	}
	after := f.snapshot()
	for path, data := range before {
		if after[path] != data {
			t.Fatalf("source changed: %s", path)
		}
	}

	again, err := ImportLegacyHome(f.src, dest, nil)
	if err != nil {
		t.Fatalf("re-run: %v", err)
	}
	if !again.Already || len(again.Written) != 0 {
		t.Fatalf("re-run not idempotent: %+v", again)
	}
}

func TestImportLegacyHomeRefusesMismatchedSharedFields(t *testing.T) {
	cases := map[string]map[string]any{
		"garden_endpoint":        {"garden_endpoint": "https://other.example/mcp"},
		"instance_id":            {"instance_id": "55555555-5555-4555-8555-555555555555"},
		"classification":         {"classification": "public"},
		"scope":                  {"scope": map[string]any{"realm": "other", "segments": []any{}}},
		"garden_tls_server_name": {"garden_tls_server_name": "other.example"},
	}
	for field, override := range cases {
		t.Run(field, func(t *testing.T) {
			f := newLegacyFixture(t)
			f.profile("zen", "opencode", override)
			dest := newDest(t)
			_, err := ImportLegacyHome(f.src, dest, nil)
			if err == nil || !strings.Contains(err.Error(), field) {
				t.Fatalf("mismatch in %s not named: %v", field, err)
			}
			if _, statErr := os.Stat(dest); statErr == nil {
				t.Fatal("refused import still wrote the home")
			}
		})
	}
}

func TestImportLegacyHomeRefusesSharedAdapterWithoutDefault(t *testing.T) {
	f := newLegacyFixture(t)
	f.token("spike2")
	f.profile("spike2", "claude", nil)
	dest := newDest(t)
	if _, err := ImportLegacyHome(f.src, dest, nil); err == nil || !strings.Contains(err.Error(), "--default claude=") {
		t.Fatalf("shared adapter accepted without --default: %v", err)
	}
	if _, err := ImportLegacyHome(f.src, dest, map[string]string{"claude": "nobody"}); err == nil {
		t.Fatal("--default naming no profile accepted")
	}
	result, err := ImportLegacyHome(f.src, dest, map[string]string{"claude": "spike2"})
	if err != nil {
		t.Fatalf("--default did not settle the choice: %v", err)
	}
	home, err := LoadHome(dest)
	if err != nil || home.Config.Defaults["claude"] != "spike2" {
		t.Fatalf("default not set: %v %v", home.Config.Defaults, err)
	}
	if len(result.Identities) != 4 {
		t.Fatalf("expected four identities: %+v", result.Identities)
	}
}

func TestImportLegacyHomeRefusesOverwritingDifferentFiles(t *testing.T) {
	f := newLegacyFixture(t)
	dest := newDest(t)
	if err := os.MkdirAll(dest, 0700); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(dest, "garden.json"), []byte(`{"garden_endpoint":"https://garden.example/mcp"}`), 0600); err != nil {
		t.Fatal(err)
	}
	if _, err := ImportLegacyHome(f.src, dest, nil); err == nil || !strings.Contains(err.Error(), "refusing to overwrite") {
		t.Fatalf("different garden.json overwritten: %v", err)
	}
	if _, err := os.Stat(filepath.Join(dest, "identities")); err == nil {
		t.Fatal("tokens written beside a refused garden.json")
	}
}

func TestImportLegacyHomeCompletesInterruptedImport(t *testing.T) {
	f := newLegacyFixture(t)
	dest := newDest(t)
	if _, err := ImportLegacyHome(f.src, dest, nil); err != nil {
		t.Fatalf("import: %v", err)
	}
	// Simulate an interruption after the tokens but before garden.json.
	if err := os.Remove(filepath.Join(dest, "garden.json")); err != nil {
		t.Fatal(err)
	}
	result, err := ImportLegacyHome(f.src, dest, nil)
	if err != nil {
		t.Fatalf("resume: %v", err)
	}
	if result.Already || len(result.Written) != 1 || !strings.HasSuffix(result.Written[0], "garden.json") {
		t.Fatalf("resume should write only garden.json: %+v", result.Written)
	}
}

func TestImportLegacyHomeRefusesSymlinkedToken(t *testing.T) {
	f := newLegacyFixture(t)
	real := filepath.Join(f.src, "credentials", "real.token")
	f.write("credentials/real.token", "fixture-token-spike\n", 0600)
	if err := os.Remove(filepath.Join(f.src, "credentials", "spike.token")); err != nil {
		t.Fatal(err)
	}
	if err := os.Symlink(real, filepath.Join(f.src, "credentials", "spike.token")); err != nil {
		t.Fatal(err)
	}
	dest := newDest(t)
	if _, err := ImportLegacyHome(f.src, dest, nil); err == nil {
		t.Fatal("symlinked token accepted")
	}
	if _, err := os.Stat(dest); err == nil {
		t.Fatal("refused import still wrote the home")
	}
}

func TestImportLegacyHomeRefusesSourceTokenWithWrongPermissions(t *testing.T) {
	f := newLegacyFixture(t)
	if err := os.Chmod(filepath.Join(f.src, "credentials", "val.token"), 0644); err != nil {
		t.Fatal(err)
	}
	dest := newDest(t)
	if _, err := ImportLegacyHome(f.src, dest, nil); err == nil {
		t.Fatal("group-readable source token accepted")
	}
	if _, err := os.Stat(dest); err == nil {
		t.Fatal("refused import still wrote the home")
	}
}

func TestImportLegacyHomeAcceptsRelativeAndAbsoluteCredentialPaths(t *testing.T) {
	f := newLegacyFixture(t)
	absolute := filepath.Join(f.src, "credentials", "val.token")
	f.profile("val", "codex", map[string]any{"credential_file": absolute})
	dest := newDest(t)
	if _, err := ImportLegacyHome(f.src, dest, nil); err != nil {
		t.Fatalf("mixed credential paths: %v", err)
	}
	home, err := LoadHome(dest)
	if err != nil {
		t.Fatal(err)
	}
	if value, err := ReadCredential(home.TokenFile("val")); err != nil || value != "fixture-token-val" {
		t.Fatalf("absolute credential not copied: %v", err)
	}
}

func TestImportLegacyHomeRefusesSourceEqualToDestination(t *testing.T) {
	f := newLegacyFixture(t)
	if _, err := ImportLegacyHome(f.src, f.src, nil); err == nil {
		t.Fatal("source equal to destination accepted")
	}
}

func TestImportLegacyHomeRefusesNestedDirectories(t *testing.T) {
	f := newLegacyFixture(t)
	before := f.snapshot()
	link := filepath.Join(t.TempDir(), "legacy-link")
	if err := os.Symlink(f.src, link); err != nil {
		t.Fatal(err)
	}
	for name, dest := range map[string]string{
		"home inside source":            filepath.Join(f.src, "home"),
		"home inside source via a link": filepath.Join(link, "home"),
		"source inside home":            filepath.Dir(f.src),
	} {
		if _, err := ImportLegacyHome(f.src, dest, nil); err == nil {
			t.Errorf("%s: import accepted", name)
		}
	}
	if after := f.snapshot(); !reflect.DeepEqual(before, after) {
		t.Fatal("refused import changed the source")
	}
	if _, err := os.Lstat(filepath.Join(f.src, "home")); !errors.Is(err, os.ErrNotExist) {
		t.Fatalf("refused import created the home inside the source: %v", err)
	}
}

func TestImportLegacyHomeRefusesWritableProfileOrCA(t *testing.T) {
	for _, rel := range []string{"spike.json", "tls/internal-ca.pem"} {
		f := newLegacyFixture(t)
		if err := os.Chmod(filepath.Join(f.src, rel), 0664); err != nil {
			t.Fatal(err)
		}
		dest := newDest(t)
		_, err := ImportLegacyHome(f.src, dest, nil)
		if err == nil || !strings.Contains(err.Error(), "writable by group or others") {
			t.Fatalf("%s group-writable: want refusal, got %v", rel, err)
		}
		if _, err := os.Lstat(filepath.Join(dest, "garden.json")); !errors.Is(err, os.ErrNotExist) {
			t.Fatalf("%s group-writable: garden.json written: %v", rel, err)
		}
	}
}

func TestImportLegacyHomeRefusesSourceUnderWritableParent(t *testing.T) {
	base := filepath.Join(t.TempDir(), "shared")
	if err := os.Mkdir(base, 0700); err != nil {
		t.Fatal(err)
	}
	f := newLegacyFixtureIn(t, base)
	// Another group member could replace legacy-garden or its files between the check and the open.
	if err := os.Chmod(base, 0770); err != nil {
		t.Fatal(err)
	}
	dest := newDest(t)
	if _, err := ImportLegacyHome(f.src, dest, nil); err == nil || !strings.Contains(err.Error(), "group- or other-writable") {
		t.Fatalf("source under a group-writable parent: want refusal, got %v", err)
	}
	if _, err := os.Lstat(filepath.Join(dest, "garden.json")); !errors.Is(err, os.ErrNotExist) {
		t.Fatalf("refused import wrote garden.json: %v", err)
	}
}

func TestImportLegacyHomeRefusesSymlinkedProfile(t *testing.T) {
	f := newLegacyFixture(t)
	real := filepath.Join(t.TempDir(), "spike.json")
	data, err := os.ReadFile(filepath.Join(f.src, "spike.json"))
	if err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(real, data, 0600); err != nil {
		t.Fatal(err)
	}
	if err := os.Remove(filepath.Join(f.src, "spike.json")); err != nil {
		t.Fatal(err)
	}
	if err := os.Symlink(real, filepath.Join(f.src, "spike.json")); err != nil {
		t.Fatal(err)
	}
	if _, err := ImportLegacyHome(f.src, newDest(t), nil); err == nil {
		t.Fatal("symlinked profile accepted")
	}
}

func TestImportLegacyHomeAcceptsSiblingWithSharedPrefix(t *testing.T) {
	f := newLegacyFixture(t)
	if _, err := ImportLegacyHome(f.src, f.src+"-home", nil); err != nil {
		t.Fatalf("sibling home refused: %v", err)
	}
}

func TestImportLegacyHomeOutputNeverContainsTokens(t *testing.T) {
	f := newLegacyFixture(t)
	result, err := ImportLegacyHome(f.src, newDest(t), nil)
	if err != nil {
		t.Fatal(err)
	}
	var rendered bytes.Buffer
	for _, id := range result.Identities {
		rendered.WriteString(id.Name + " " + id.Harness + "\n")
	}
	rendered.WriteString(strings.Join(result.Written, "\n"))
	if strings.Contains(rendered.String(), "fixture-token") {
		t.Fatal("token contents leaked into the result")
	}
}

func (r ImportResult) hasNotMigrated(fragment string) bool {
	for _, name := range r.NotMigrated {
		if strings.Contains(name, fragment) {
			return true
		}
	}
	return false
}

func slicesContains(values []string, want string) bool {
	for _, value := range values {
		if value == want {
			return true
		}
	}
	return false
}

func TestImportPublishesGardenJSONOnlyAfterItsDependencies(t *testing.T) {
	f := newLegacyFixture(t)
	dest := newDest(t)
	checked := false
	publishHook = func(rel string) {
		if rel != "garden.json" {
			return
		}
		checked = true
		for _, dep := range []string{"tls/internal-ca.pem", "identities/spike.token", "identities/val.token", "identities/zen.token"} {
			if _, err := os.Lstat(filepath.Join(dest, dep)); err != nil {
				t.Errorf("garden.json published before %s: %v", dep, err)
			}
		}
		if _, err := os.Lstat(filepath.Join(dest, "garden.json")); err == nil {
			t.Error("garden.json existed before its publication")
		}
	}
	defer func() { publishHook = func(string) {} }()
	if _, err := ImportLegacyHome(f.src, dest, nil); err != nil {
		t.Fatalf("import: %v", err)
	}
	if !checked {
		t.Fatal("garden.json was never published")
	}
}
