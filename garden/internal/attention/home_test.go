package attention

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
)

const validHomeConfig = `{"garden_endpoint":"https://garden.example/mcp","garden_tls_ca_file":"tls/internal-ca.pem","garden_tls_server_name":"garden.example","instance_id":"33333333-3333-4333-8333-333333333333","scope":{"realm":"example","segments":[{"kind":"project","identifier":"garden"}]},"classification":"internal","defaults":{"claude":"spike","codex":"val","opencode":"zen"}}`

func writeHome(t *testing.T, dir, body string) {
	t.Helper()
	if err := os.MkdirAll(dir, 0700); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(dir, "garden.json"), []byte(body), 0600); err != nil {
		t.Fatal(err)
	}
}

func TestResolveHomeDirPrefersGardenHomeThenDotGarden(t *testing.T) {
	userHome := t.TempDir()
	t.Setenv("HOME", userHome)

	t.Setenv("GARDEN_HOME", "/srv/garden-home")
	t.Setenv("XDG_CONFIG_HOME", "/srv/xdg")
	if got, err := ResolveHomeDir(); err != nil || got != "/srv/garden-home" {
		t.Fatalf("GARDEN_HOME not used: %q %v", got, err)
	}

	t.Setenv("GARDEN_HOME", "")
	if got, err := ResolveHomeDir(); err != nil || got != filepath.Join(userHome, ".garden") {
		t.Fatalf("default home wrong, XDG_CONFIG_HOME must be ignored: %q %v", got, err)
	}
}

func TestResolveHomeDirRefusesRelativeGardenHome(t *testing.T) {
	t.Setenv("GARDEN_HOME", "relative/garden")
	if _, err := ResolveHomeDir(); err == nil {
		t.Fatal("relative GARDEN_HOME accepted")
	}
}

func TestLoadHomeResolvesRelativeTLSPathAgainstHome(t *testing.T) {
	dir := t.TempDir()
	writeHome(t, dir, validHomeConfig)
	home, err := LoadHome(dir)
	if err != nil {
		t.Fatal(err)
	}
	if home.Config.GardenTLSCAFile != filepath.Join(dir, "tls", "internal-ca.pem") {
		t.Fatalf("CA file not resolved against home: %q", home.Config.GardenTLSCAFile)
	}
	if home.Config.Defaults["claude"] != "spike" {
		t.Fatalf("defaults not loaded: %#v", home.Config.Defaults)
	}
	if got := home.TokenFile("spike"); got != filepath.Join(dir, "identities", "spike.token") {
		t.Fatalf("token path wrong: %q", got)
	}
}

func TestLoadHomeRefusesUnsafeOrAmbiguousConfiguration(t *testing.T) {
	cases := map[string]string{
		"unknown field":       strings.Replace(validHomeConfig, `"classification":"internal"`, `"classification":"internal","surprise":1`, 1),
		"duplicate field":     strings.Replace(validHomeConfig, `"classification":"internal"`, `"classification":"internal","classification":"public"`, 1),
		"trailing JSON":       validHomeConfig + " {}",
		"plain http endpoint": strings.Replace(validHomeConfig, "https://garden.example/mcp", "http://garden.example/mcp", 1),
		"bad instance":        strings.Replace(validHomeConfig, "33333333-3333-4333-8333-333333333333", "not-a-uuid", 1),
		"bad default name":    strings.Replace(validHomeConfig, `"claude":"spike"`, `"claude":"../spike"`, 1),
		"oversize":            `{"garden_endpoint":"https://garden.example/mcp","pad":"` + strings.Repeat("x", 33*1024) + `"}`,
		"top-level alias":     strings.Replace(validHomeConfig, `"instance_id"`, `"INSTANCE_ID"`, 1),
		"alias beside exact":  strings.Replace(validHomeConfig, `"classification"`, `"GARDEN_ENDPOINT":"https://evil.example/mcp","classification"`, 1),
		"scope realm alias":   strings.Replace(validHomeConfig, `"realm"`, `"Realm"`, 1),
		"segment kind alias":  strings.Replace(validHomeConfig, `"kind"`, `"Kind"`, 1),
		"defaults bad key":    strings.Replace(validHomeConfig, `"claude":"spike"`, `"Bad Harness":"spike"`, 1),
	}
	for name, body := range cases {
		t.Run(name, func(t *testing.T) {
			dir := t.TempDir()
			writeHome(t, dir, body)
			if _, err := LoadHome(dir); err == nil {
				t.Fatal("unsafe home configuration accepted")
			}
		})
	}
}

func TestLoadHomeRefusesSymlinkedConfig(t *testing.T) {
	dir := t.TempDir()
	writeHome(t, filepath.Join(dir, "real"), validHomeConfig)
	if err := os.Mkdir(filepath.Join(dir, "linked"), 0700); err != nil {
		t.Fatal(err)
	}
	if err := os.Symlink(filepath.Join(dir, "real", "garden.json"), filepath.Join(dir, "linked", "garden.json")); err != nil {
		t.Fatal(err)
	}
	if _, err := LoadHome(filepath.Join(dir, "linked")); err == nil {
		t.Fatal("symlinked garden.json accepted")
	}
}

func TestChooseIdentityPrefersExplicitThenHarnessDefault(t *testing.T) {
	defaults := map[string]string{"claude": "spike"}
	got, err := ChooseIdentity("spike2", defaults, "claude")
	if err != nil || got.Name != "spike2" || !got.Explicit {
		t.Fatalf("explicit identity lost: %#v %v", got, err)
	}
	got, err = ChooseIdentity("", defaults, "claude")
	if err != nil || got.Name != "spike" || got.Explicit {
		t.Fatalf("default identity wrong: %#v %v", got, err)
	}
}

func TestChooseIdentityRejectsMissingAndInvalidNames(t *testing.T) {
	defaults := map[string]string{"claude": "spike"}
	if _, err := ChooseIdentity("", defaults, "codex"); err == nil || err.Error() != "no default identity for codex" {
		t.Fatalf("missing default not reported: %v", err)
	}
	for _, name := range []string{"../spike", ".spike", "-spike", "spi ke", "spike/2", "spike\n", strings.Repeat("a", 65)} {
		if _, err := ChooseIdentity(name, defaults, "claude"); err == nil {
			t.Fatalf("invalid explicit name %q accepted", name)
		}
	}
	if _, err := ChooseIdentity("", map[string]string{"claude": "bad name"}, "claude"); err == nil {
		t.Fatal("invalid default name accepted")
	}
	if _, err := ChooseIdentity(strings.Repeat("a", 64), defaults, "claude"); err != nil {
		t.Fatalf("64-character name refused: %v", err)
	}
}
