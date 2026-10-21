package attention

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"io/fs"
	"os"
	"path/filepath"
	"reflect"
	"sort"
	"strings"
	"syscall"
	"testing"
	"time"
)

// Throwaway fixtures only. Tokens here are invented.

// tree records every path under root with its mode and a content digest, so a test can
// prove that a refused command created or changed nothing.
func tree(t *testing.T, root string) []string {
	t.Helper()
	var out []string
	err := filepath.WalkDir(root, func(path string, d fs.DirEntry, err error) error {
		if err != nil {
			return err
		}
		// The home lock is the one file a refused command may create: it is taken before the
		// checks, so that they read state no other command can change meanwhile.
		if d.Name() == homeLockName {
			return nil
		}
		info, err := os.Lstat(path)
		if err != nil {
			return err
		}
		entry := path + " " + info.Mode().String()
		if info.Mode().IsRegular() {
			data, err := os.ReadFile(path)
			if err != nil {
				return err
			}
			sum := sha256.Sum256(data)
			entry += " " + hex.EncodeToString(sum[:])
		}
		out = append(out, entry)
		return nil
	})
	if err != nil {
		t.Fatal(err)
	}
	sort.Strings(out)
	return out
}

func assertTree(t *testing.T, root string, want []string) {
	t.Helper()
	if got := tree(t, root); !reflect.DeepEqual(got, want) {
		t.Fatalf("tree changed under %s:\nwant %v\n got %v", root, want, got)
	}
}

// homeWithSymlinkTarget returns a private home directory and an empty outside directory.
func symlinkFixture(t *testing.T) (base, outside string) {
	t.Helper()
	base = t.TempDir()
	outside = filepath.Join(base, "outside")
	if err := os.Mkdir(outside, 0700); err != nil {
		t.Fatal(err)
	}
	return base, outside
}

func TestParticipantTraversalRefusedBeforeAnyWrite(t *testing.T) {
	base := t.TempDir()
	f := newLegacyFixtureIn(t, base)
	f.profile("zen", "opencode", map[string]any{"participant": "../../escaped"})
	dest := filepath.Join(base, "deep", "garden-home")
	before := tree(t, base)

	if _, err := ImportLegacyHome(f.src, dest, nil); err == nil || !strings.Contains(err.Error(), "not a valid Garden identity name") {
		t.Fatalf("traversal participant accepted by import: %v", err)
	}
	assertTree(t, base, before)
}

func TestAddIdentityTraversalRefusedBeforeAnyWrite(t *testing.T) {
	base := privateHome(t)
	home := filepath.Join(base, "home")
	if err := os.Mkdir(home, 0700); err != nil {
		t.Fatal(err)
	}
	writeHome(t, home, homeWithoutDefaults)
	src := sourceToken(t, identityFixtureToken, 0600)
	before := tree(t, base)
	for _, name := range []string{"../../escaped", "../x", "a/b", "/abs"} {
		if _, err := AddIdentity(home, name, src, "claude"); err == nil {
			t.Fatalf("name %q accepted", name)
		}
	}
	assertTree(t, base, before)
}

func TestImportRefusesSymlinkedHome(t *testing.T) {
	base, outside := symlinkFixture(t)
	f := newLegacyFixtureIn(t, filepath.Join(base, "src-root"))
	link := filepath.Join(base, "garden-link")
	if err := os.Symlink(outside, link); err != nil {
		t.Fatal(err)
	}
	before := tree(t, outside)
	if _, err := ImportLegacyHome(f.src, link, nil); err == nil {
		t.Fatal("symlinked home accepted")
	}
	assertTree(t, outside, before)
}

func TestImportRefusesSymlinkedAncestor(t *testing.T) {
	base, outside := symlinkFixture(t)
	f := newLegacyFixtureIn(t, filepath.Join(base, "src-root"))
	link := filepath.Join(base, "via-link")
	if err := os.Symlink(outside, link); err != nil {
		t.Fatal(err)
	}
	before := tree(t, outside)
	if _, err := ImportLegacyHome(f.src, filepath.Join(link, "garden-home"), nil); err == nil {
		t.Fatal("home under a symlinked ancestor accepted")
	}
	assertTree(t, outside, before)
}

func TestImportRefusesSymlinkedIdentitiesDirectory(t *testing.T) {
	base, outside := symlinkFixture(t)
	f := newLegacyFixtureIn(t, filepath.Join(base, "src-root"))
	dest := filepath.Join(base, "home")
	if err := os.Mkdir(dest, 0700); err != nil {
		t.Fatal(err)
	}
	if err := os.Symlink(outside, filepath.Join(dest, "identities")); err != nil {
		t.Fatal(err)
	}
	before := tree(t, outside)
	if _, err := ImportLegacyHome(f.src, dest, nil); err == nil {
		t.Fatal("symlinked identities directory accepted")
	}
	assertTree(t, outside, before)
	if _, err := os.Stat(filepath.Join(dest, "garden.json")); err == nil {
		t.Fatal("garden.json written beside a refused identities link")
	}
}

func TestAddIdentityRefusesSymlinkedHomeAndAncestor(t *testing.T) {
	base, outside := symlinkFixture(t)
	writeHome(t, outside, homeWithoutDefaults)
	src := sourceToken(t, identityFixtureToken, 0600)
	before := tree(t, outside)

	homeLink := filepath.Join(base, "home-link")
	if err := os.Symlink(outside, homeLink); err != nil {
		t.Fatal(err)
	}
	if _, err := AddIdentity(homeLink, "new1", src, ""); err == nil {
		t.Fatal("symlinked home accepted")
	}
	ancestorLink := filepath.Join(base, "ancestor-link")
	if err := os.Symlink(base, ancestorLink); err != nil {
		t.Fatal(err)
	}
	if _, err := AddIdentity(filepath.Join(ancestorLink, "outside"), "new1", src, ""); err == nil {
		t.Fatal("home under a symlinked ancestor accepted")
	}
	assertTree(t, outside, before)
}

func TestAddIdentityRefusesSymlinkedIdentitiesDirectory(t *testing.T) {
	base, outside := symlinkFixture(t)
	home := privateHome(t)
	writeHome(t, home, homeWithoutDefaults)
	if err := os.Symlink(outside, filepath.Join(home, "identities")); err != nil {
		t.Fatal(err)
	}
	src := sourceToken(t, identityFixtureToken, 0600)
	before := tree(t, outside)
	if _, err := AddIdentity(home, "new1", src, "codex"); err == nil {
		t.Fatal("symlinked identities directory accepted")
	}
	assertTree(t, outside, before)
	_ = base
}

func TestImportAndAddRefusePreexistingWideDirectories(t *testing.T) {
	for _, dirName := range []string{"identities", "tls"} {
		t.Run(dirName, func(t *testing.T) {
			f := newLegacyFixture(t)
			dest := newDest(t)
			if err := os.Mkdir(dest, 0700); err != nil {
				t.Fatal(err)
			}
			if err := os.Mkdir(filepath.Join(dest, dirName), 0755); err != nil {
				t.Fatal(err)
			}
			if err := os.Chmod(filepath.Join(dest, dirName), 0755); err != nil {
				t.Fatal(err)
			}
			before := tree(t, dest)
			if _, err := ImportLegacyHome(f.src, dest, nil); err == nil {
				t.Fatalf("import accepted a 0755 %s directory", dirName)
			}
			assertTree(t, dest, before)
		})
	}

	home := privateHome(t)
	writeHome(t, home, homeWithoutDefaults)
	if err := os.Mkdir(filepath.Join(home, "identities"), 0755); err != nil {
		t.Fatal(err)
	}
	if err := os.Chmod(filepath.Join(home, "identities"), 0755); err != nil {
		t.Fatal(err)
	}
	src := sourceToken(t, identityFixtureToken, 0600)
	before := tree(t, home)
	if _, err := AddIdentity(home, "new1", src, ""); err == nil {
		t.Fatal("add accepted a 0755 identities directory")
	}
	assertTree(t, home, before)
}

func TestHomeWritableByOthersRefusedByBothCommands(t *testing.T) {
	for _, mode := range []os.FileMode{0775, 0757, 0777} {
		f := newLegacyFixture(t)
		dest := newDest(t)
		if err := os.Mkdir(dest, 0700); err != nil {
			t.Fatal(err)
		}
		if err := os.Chmod(dest, mode); err != nil {
			t.Fatal(err)
		}
		before := tree(t, dest)
		if _, err := ImportLegacyHome(f.src, dest, nil); err == nil {
			t.Fatalf("import accepted a %o home", mode)
		}
		assertTree(t, dest, before)
		src := sourceToken(t, identityFixtureToken, 0600)
		if _, err := AddIdentity(dest, "new1", src, ""); err == nil {
			t.Fatalf("add accepted a %o home", mode)
		}
		assertTree(t, dest, before)
	}
}

func TestLegacyReadableHomeIsAcceptedAndSecretsStayPrivate(t *testing.T) {
	// An older daemon created ~/.garden as 0755; that home is accepted, identities/ stays 0700.
	f := newLegacyFixture(t)
	dest := newDest(t)
	if err := os.Mkdir(dest, 0700); err != nil {
		t.Fatal(err)
	}
	if err := os.Chmod(dest, 0755); err != nil {
		t.Fatal(err)
	}
	if _, err := ImportLegacyHome(f.src, dest, nil); err != nil {
		t.Fatalf("import into a 0755 home: %v", err)
	}
	info, err := os.Lstat(filepath.Join(dest, "identities"))
	if err != nil || info.Mode().Perm() != 0700 {
		t.Fatalf("identities/ = %v, %v; want 0700", info, err)
	}
	src := sourceToken(t, identityFixtureToken, 0600)
	if _, err := AddIdentity(dest, "new1", src, ""); err != nil {
		t.Fatalf("add into a 0755 home: %v", err)
	}
}

func TestImportCreatesHomeAt0700(t *testing.T) {
	f := newLegacyFixture(t)
	dest := newDest(t)
	if _, err := ImportLegacyHome(f.src, dest, nil); err != nil {
		t.Fatalf("import: %v", err)
	}
	for _, dir := range []string{dest, filepath.Join(dest, "tls"), filepath.Join(dest, "identities")} {
		if got := mode(t, dir); got != 0700 {
			t.Fatalf("%s mode %o", dir, got)
		}
	}
}

func TestExistingTargetSymlinkRefusedWithoutWriting(t *testing.T) {
	base, outside := symlinkFixture(t)
	f := newLegacyFixtureIn(t, filepath.Join(base, "src-root"))
	dest := filepath.Join(base, "home")
	if err := os.Mkdir(dest, 0700); err != nil {
		t.Fatal(err)
	}
	if err := os.Mkdir(filepath.Join(dest, "identities"), 0700); err != nil {
		t.Fatal(err)
	}
	victim := filepath.Join(outside, "victim")
	if err := os.WriteFile(victim, []byte("keep"), 0600); err != nil {
		t.Fatal(err)
	}
	if err := os.Symlink(victim, filepath.Join(dest, "identities", "spike.token")); err != nil {
		t.Fatal(err)
	}
	before := tree(t, dest)
	if _, err := ImportLegacyHome(f.src, dest, nil); err == nil {
		t.Fatal("symlink at a token target accepted")
	}
	assertTree(t, dest, before)
	if data, _ := os.ReadFile(victim); string(data) != "keep" {
		t.Fatal("symlink target changed")
	}
}

func TestSourceTokenFIFORefusedWithoutBlocking(t *testing.T) {
	base := t.TempDir()
	fifo := filepath.Join(base, "issued.token")
	if err := syscall.Mkfifo(fifo, 0600); err != nil {
		t.Fatal(err)
	}
	done := make(chan error, 1)
	go func() {
		_, err := privateSourceBytes(fifo, 4096)
		done <- err
	}()
	select {
	case err := <-done:
		if err == nil {
			t.Fatal("FIFO accepted as a token")
		}
	case <-time.After(5 * time.Second):
		t.Fatal("FIFO open blocked")
	}
}

func TestSourceTokenGroupReadableRefusedForBothCommands(t *testing.T) {
	f := newLegacyFixture(t)
	if err := os.Chmod(filepath.Join(f.src, "credentials", "spike.token"), 0640); err != nil {
		t.Fatal(err)
	}
	dest := newDest(t)
	if _, err := ImportLegacyHome(f.src, dest, nil); err == nil {
		t.Fatal("import accepted a group-readable token")
	}
	if _, err := os.Stat(dest); err == nil {
		t.Fatal("refused import created the home")
	}
	home := privateHome(t)
	writeHome(t, home, homeWithoutDefaults)
	wide := sourceToken(t, identityFixtureToken, 0600)
	if err := os.Chmod(wide, 0604); err != nil {
		t.Fatal(err)
	}
	if _, err := AddIdentity(home, "new1", wide, ""); err == nil {
		t.Fatal("add accepted an other-readable token")
	}
}

func TestNullDefaultsInitialisedBeforeWrite(t *testing.T) {
	home := privateHome(t)
	body := strings.Replace(homeWithoutDefaults, `"classification":"internal"}`, `"classification":"internal","defaults":null}`, 1)
	writeHome(t, home, body)
	src := sourceToken(t, identityFixtureToken, 0600)
	result, err := AddIdentity(home, "new1", src, "claude")
	if err != nil {
		t.Fatalf("null defaults: %v", err)
	}
	if !result.DefaultSet {
		t.Fatalf("default not set: %+v", result)
	}
	loaded, err := LoadHome(home)
	if err != nil || loaded.Config.Defaults["claude"] != "new1" {
		t.Fatalf("default not persisted: %v %v", loaded.Config.Defaults, err)
	}
	var fields map[string]any
	data, _ := os.ReadFile(filepath.Join(home, "garden.json"))
	if err := json.Unmarshal(data, &fields); err != nil || fields["classification"] != "internal" {
		t.Fatalf("other fields not preserved: %v", err)
	}
}
