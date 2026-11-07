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

// LoadHome feeds the endpoint that receives the bearer token, so it applies the writing
// path's ownership rules before reading garden.json (issue #199).
func TestLoadHomeRefusesHomeOthersCouldReplace(t *testing.T) {
	t.Run("group-writable parent", func(t *testing.T) {
		wide := filepath.Join(t.TempDir(), "wide")
		writeHome(t, filepath.Join(wide, "home"), validHomeConfig)
		if err := os.Chmod(wide, 0775); err != nil {
			t.Fatal(err)
		}
		if _, err := LoadHome(filepath.Join(wide, "home")); err == nil || !strings.Contains(err.Error(), "group- or other-writable") {
			t.Fatalf("home under a group-writable parent accepted: %v", err)
		}
	})
	t.Run("group-writable home", func(t *testing.T) {
		dir := t.TempDir()
		writeHome(t, dir, validHomeConfig)
		if err := os.Chmod(dir, 0770); err != nil {
			t.Fatal(err)
		}
		if _, err := LoadHome(dir); err == nil || !strings.Contains(err.Error(), "must not be writable") {
			t.Fatalf("group-writable home accepted: %v", err)
		}
	})
	t.Run("group-writable garden.json", func(t *testing.T) {
		dir := t.TempDir()
		writeHome(t, dir, validHomeConfig)
		if err := os.Chmod(filepath.Join(dir, "garden.json"), 0660); err != nil {
			t.Fatal(err)
		}
		if _, err := LoadHome(dir); err == nil {
			t.Fatal("group-writable garden.json accepted")
		}
	})
	t.Run("home owned by another user", func(t *testing.T) {
		if os.Getuid() == 0 {
			t.Skip("root owns /")
		}
		if _, err := LoadHome("/"); err == nil || !strings.Contains(err.Error(), "owned by this user") {
			t.Fatalf("home owned by root accepted: %v", err)
		}
	})
	t.Run("symlinked home", func(t *testing.T) {
		base := t.TempDir()
		writeHome(t, filepath.Join(base, "real"), validHomeConfig)
		if err := os.Symlink(filepath.Join(base, "real"), filepath.Join(base, "linked")); err != nil {
			t.Fatal(err)
		}
		if _, err := LoadHome(filepath.Join(base, "linked")); err == nil {
			t.Fatal("symlinked home accepted")
		}
	})
}

func TestLoadHomeRefusesHomeSwappedBeforeRead(t *testing.T) {
	base := privateHome(t)
	home := filepath.Join(base, "home")
	other := filepath.Join(base, "other")
	writeHome(t, home, validHomeConfig)
	writeHome(t, other, strings.Replace(validHomeConfig, "https://garden.example/mcp", "https://evil.example/mcp", 1))
	afterHomeValidated = func(dst string) {
		if err := os.Rename(dst, dst+".orig"); err != nil {
			t.Fatal(err)
		}
		if err := os.Rename(other, dst); err != nil {
			t.Fatal(err)
		}
	}
	t.Cleanup(func() { afterHomeValidated = func(string) {} })
	if loaded, err := LoadHome(home); err == nil || !strings.Contains(err.Error(), "changed while it was being opened") {
		t.Fatalf("swapped home accepted: %v %q", err, loaded.Config.GardenEndpoint)
	}
}

func TestLoadHomeAcceptsDefaultAndLegacyReadableHome(t *testing.T) {
	user := t.TempDir()
	t.Setenv("HOME", user)
	t.Setenv("GARDEN_HOME", "")
	dir, err := ResolveHomeDir()
	if err != nil {
		t.Fatal(err)
	}
	writeHome(t, dir, validHomeConfig)
	for _, mode := range []os.FileMode{0700, 0755} {
		if err := os.Chmod(dir, mode); err != nil {
			t.Fatal(err)
		}
		if _, err := LoadHome(dir); err != nil {
			t.Fatalf("default home at %o refused: %v", mode, err)
		}
	}
	if err := os.Chmod(filepath.Join(dir, "garden.json"), 0644); err != nil {
		t.Fatal(err)
	}
	if _, err := LoadHome(dir); err != nil {
		t.Fatalf("readable garden.json refused: %v", err)
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
