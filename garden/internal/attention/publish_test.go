package attention

import (
	"os"
	"path/filepath"
	"strings"
	"sync/atomic"
	"syscall"
	"testing"
	"time"
)

// Throwaway fixtures only. Tokens here are invented.

// namedLegacyFixture builds a legacy source whose participants are spike<suffix>, val<suffix>
// and zen<suffix>, so two sources can disagree about identities.
func namedLegacyFixture(t *testing.T, base, suffix string) legacyFixture {
	t.Helper()
	f := legacyFixture{t: t, src: filepath.Join(base, "legacy-garden-"+suffix)}
	for _, dir := range []string{"credentials", "tls"} {
		if err := os.MkdirAll(filepath.Join(f.src, dir), 0700); err != nil {
			t.Fatal(err)
		}
	}
	f.write("tls/internal-ca.pem", fixtureCA, 0600)
	for _, name := range []string{"spike", "val", "zen"} {
		f.token(name + suffix)
		adapter := map[string]string{"spike": "claude", "val": "codex", "zen": "opencode"}[name]
		f.profile(name+suffix, adapter, nil)
	}
	return f
}

// suffix recovers the fixture's participant suffix from its source directory name.
func (f legacyFixture) suffix() string {
	return strings.TrimPrefix(filepath.Base(f.src), "legacy-garden-")
}

// retoken replaces one source token with different bytes, for a conflicting-token scenario.
func (f legacyFixture) retoken(name, value string) {
	f.write("credentials/"+name+".token", value+"\n", 0600)
}

// serialise runs first, waits until it holds the home lock, then starts second and lets it queue
// behind the lock for a moment before releasing first. Whatever the timing, second must observe
// first's completed state. It returns both results in the order given.
func serialise(t *testing.T, first, second func() error) (errFirst, errSecond error) {
	t.Helper()
	locked := make(chan struct{}, 1)
	release := make(chan struct{})
	var acquisitions int32
	afterHomeLocked = func() {
		if atomic.AddInt32(&acquisitions, 1) == 1 {
			locked <- struct{}{}
			<-release
		}
	}
	t.Cleanup(func() { afterHomeLocked = func() {} })

	type outcome struct {
		index int
		err   error
	}
	done := make(chan outcome, 2)
	go func() { done <- outcome{0, first()} }()
	select {
	case <-locked:
	case <-time.After(10 * time.Second):
		t.Fatal("first command did not take the home lock")
	}
	go func() { done <- outcome{1, second()} }()
	time.Sleep(200 * time.Millisecond)
	close(release)
	for i := 0; i < 2; i++ {
		o := <-done
		if o.index == 0 {
			errFirst = o.err
		} else {
			errSecond = o.err
		}
	}
	return errFirst, errSecond
}

func importer(from, dest string) func() error {
	return func() error {
		_, err := ImportLegacyHome(from, dest, nil)
		return err
	}
}

func TestConcurrentImportsWithDifferentIdentitiesExactlyOneWins(t *testing.T) {
	base := t.TempDir()
	a := namedLegacyFixture(t, base, "")
	b := namedLegacyFixture(t, base, "2")
	dest := newDest(t)

	errA, errB := serialise(t, importer(a.src, dest), importer(b.src, dest))
	if errA != nil || errB == nil {
		t.Fatalf("first import must win and second must refuse: %v / %v", errA, errB)
	}
	if !strings.Contains(errB.Error(), "refusing to overwrite") {
		t.Fatalf("second not refused for the conflict: %v", errB)
	}
	home, err := LoadHome(dest)
	if err != nil || home.Config.Defaults["claude"] != "spike" {
		t.Fatalf("garden.json is not the winner's: %v %v", home.Config.Defaults, err)
	}
	for _, name := range []string{"spike2", "val2", "zen2"} {
		if _, err := os.Stat(filepath.Join(dest, "identities", name+".token")); err == nil {
			t.Fatalf("loser's token %s was written", name)
		}
	}
}

// TestIdenticalConfigConflictingTokensSerialise is the first Sol ordering: one import publishes
// garden.json, the other accepts the same configuration with different token bytes. Serialised,
// the winner's home must stay intact whichever order runs.
func TestIdenticalConfigConflictingTokensSerialise(t *testing.T) {
	for _, order := range []string{"a-first", "b-first"} {
		t.Run(order, func(t *testing.T) {
			a := namedLegacyFixture(t, t.TempDir(), "")
			b := namedLegacyFixture(t, t.TempDir(), "")
			b.retoken("spike", "other-token-spike")
			dest := newDest(t)

			first, second := importer(a.src, dest), importer(b.src, dest)
			if order == "b-first" {
				first, second = second, first
			}
			errFirst, errSecond := serialise(t, first, second)
			if errFirst != nil || errSecond == nil {
				t.Fatalf("first must win and second must refuse: %v / %v", errFirst, errSecond)
			}
			if !strings.Contains(errSecond.Error(), "refusing to overwrite") {
				t.Fatalf("second not refused for the token conflict: %v", errSecond)
			}
			home, err := LoadHome(dest)
			if err != nil {
				t.Fatalf("winner's home no longer loads: %v", err)
			}
			winnerToken := "fixture-token-spike"
			if order == "b-first" {
				winnerToken = "other-token-spike"
			}
			if value, err := ReadCredential(home.TokenFile("spike")); err != nil || value != winnerToken {
				t.Fatalf("winner's token damaged: %v", err)
			}
		})
	}
}

func TestIdenticalImportsBothSucceedUnderLock(t *testing.T) {
	a := namedLegacyFixture(t, t.TempDir(), "")
	b := namedLegacyFixture(t, t.TempDir(), "")
	dest := newDest(t)

	errA, errB := serialise(t, importer(a.src, dest), importer(b.src, dest))
	if errA != nil || errB != nil {
		t.Fatalf("identical imports should both succeed: %v / %v", errA, errB)
	}
	if _, err := LoadHome(dest); err != nil {
		t.Fatalf("home does not load: %v", err)
	}
}

// TestConcurrentConflictingDefaultsSerialise is the second Sol ordering: two add-identity runs
// set different defaults for the same harness. Serialised, the second must see the first's
// default and refuse, and its token must not be installed.
func TestConcurrentConflictingDefaultsSerialise(t *testing.T) {
	for _, order := range []string{"x-first", "y-first"} {
		t.Run(order, func(t *testing.T) {
			home := privateHome(t)
			writeHome(t, home, homeWithoutDefaults)
			srcX := sourceToken(t, "fixture-token-x", 0600)
			srcY := sourceToken(t, "fixture-token-y", 0600)
			addX := func() error { _, err := AddIdentity(home, "x1", srcX, "claude"); return err }
			addY := func() error { _, err := AddIdentity(home, "y1", srcY, "claude"); return err }
			first, second := addX, addY
			winner, loser := "x1", "y1"
			if order == "y-first" {
				first, second = addY, addX
				winner, loser = "y1", "x1"
			}
			errFirst, errSecond := serialise(t, first, second)
			if errFirst != nil || errSecond == nil || !strings.Contains(errSecond.Error(), "already defaults to "+winner) {
				t.Fatalf("second must refuse the first's default: %v / %v", errFirst, errSecond)
			}
			home2, err := LoadHome(home)
			if err != nil || home2.Config.Defaults["claude"] != winner {
				t.Fatalf("default replaced: %v %v", home2.Config.Defaults, err)
			}
			if _, err := os.Stat(filepath.Join(home, "identities", loser+".token")); err == nil {
				t.Fatalf("refused add installed %s", loser)
			}
		})
	}
}

// TestHomeLockTimeoutRefusesWithoutWriting holds the home lock from outside and checks that the
// command waits, then refuses with the clear message, writing nothing.
func TestHomeLockTimeoutRefusesWithoutWriting(t *testing.T) {
	home := privateHome(t)
	writeHome(t, home, homeWithoutDefaults)
	lockFile, err := os.OpenFile(filepath.Join(home, ".home.lock"), os.O_RDWR|os.O_CREATE, 0600)
	if err != nil {
		t.Fatal(err)
	}
	defer lockFile.Close()
	if err := syscall.Flock(int(lockFile.Fd()), syscall.LOCK_EX); err != nil {
		t.Fatal(err)
	}
	oldTimeout := lockTimeout
	lockTimeout = 200 * time.Millisecond
	t.Cleanup(func() { lockTimeout = oldTimeout })

	src := sourceToken(t, identityFixtureToken, 0600)
	before := tree(t, home)
	start := time.Now()
	_, err = AddIdentity(home, "new1", src, "codex")
	if err == nil || !strings.Contains(err.Error(), "another Garden home command is running") {
		t.Fatalf("held lock not reported: %v", err)
	}
	if time.Since(start) < 150*time.Millisecond {
		t.Fatal("command did not wait for the lock")
	}
	assertTree(t, home, before)

	if err := syscall.Flock(int(lockFile.Fd()), syscall.LOCK_UN); err != nil {
		t.Fatal(err)
	}
	if _, err := AddIdentity(home, "new1", src, "codex"); err != nil {
		t.Fatalf("add after release: %v", err)
	}
}

// TestHomeLockFileMustNotBeSymlink refuses a lock path that points elsewhere, and leaves the
// target untouched, for both commands.
func TestHomeLockFileMustNotBeSymlink(t *testing.T) {
	base := t.TempDir()
	home := privateHome(t)
	writeHome(t, home, homeWithoutDefaults)
	outside := filepath.Join(base, "outside-lock")
	if err := os.WriteFile(outside, []byte("keep"), 0600); err != nil {
		t.Fatal(err)
	}
	if err := os.Symlink(outside, filepath.Join(home, ".home.lock")); err != nil {
		t.Fatal(err)
	}
	src := sourceToken(t, identityFixtureToken, 0600)
	if _, err := AddIdentity(home, "new1", src, "codex"); err == nil {
		t.Fatal("add-identity accepted a symlinked lock")
	}
	f := newLegacyFixtureIn(t, filepath.Join(base, "src"))
	if _, err := ImportLegacyHome(f.src, home, nil); err == nil {
		t.Fatal("import accepted a symlinked lock")
	}
	if data, _ := os.ReadFile(outside); string(data) != "keep" {
		t.Fatal("symlink target changed")
	}
	if _, err := os.Stat(filepath.Join(home, "identities")); err == nil {
		t.Fatal("identities written past a refused lock")
	}
}

func TestAncestorGroupWritableWithoutStickyRefused(t *testing.T) {
	base := t.TempDir()
	wide := filepath.Join(base, "wide")
	if err := os.Mkdir(wide, 0700); err != nil {
		t.Fatal(err)
	}
	if err := os.Chmod(wide, 0777); err != nil {
		t.Fatal(err)
	}
	home := filepath.Join(wide, "home")
	if err := os.Mkdir(home, 0700); err != nil {
		t.Fatal(err)
	}
	writeHome(t, home, homeWithoutDefaults)
	src := sourceToken(t, identityFixtureToken, 0600)
	before := tree(t, base)
	if _, err := AddIdentity(home, "new1", src, ""); err == nil || !strings.Contains(err.Error(), "group- or other-writable") {
		t.Fatalf("world-writable ancestor accepted: %v", err)
	}
	assertTree(t, base, before)

	f := newLegacyFixtureIn(t, filepath.Join(t.TempDir(), "src"))
	beforeImport := tree(t, base)
	if _, err := ImportLegacyHome(f.src, filepath.Join(wide, "fresh-home"), nil); err == nil {
		t.Fatal("import under a world-writable ancestor accepted")
	}
	assertTree(t, base, beforeImport)
}

func TestAncestorStickyWorldWritableAllowed(t *testing.T) {
	base := t.TempDir()
	sticky := filepath.Join(base, "sticky")
	if err := os.Mkdir(sticky, 0700); err != nil {
		t.Fatal(err)
	}
	if err := os.Chmod(sticky, 0777|os.ModeSticky); err != nil {
		t.Fatal(err)
	}
	home := filepath.Join(sticky, "home")
	if err := os.Mkdir(home, 0700); err != nil {
		t.Fatal(err)
	}
	writeHome(t, home, homeWithoutDefaults)
	src := sourceToken(t, identityFixtureToken, 0600)
	if _, err := AddIdentity(home, "new1", src, ""); err != nil {
		t.Fatalf("sticky world-writable ancestor refused: %v", err)
	}
}

func TestHomeSwappedBetweenValidationAndOpenRefused(t *testing.T) {
	base := privateHome(t)
	home := filepath.Join(base, "home")
	other := filepath.Join(base, "other")
	for _, dir := range []string{home, other} {
		if err := os.Mkdir(dir, 0700); err != nil {
			t.Fatal(err)
		}
		writeHome(t, dir, homeWithoutDefaults)
	}
	src := sourceToken(t, identityFixtureToken, 0600)
	afterHomeValidated = func(dst string) {
		// A racing rename lands a different, equally valid 0700 directory where the home was.
		if err := os.Rename(dst, dst+".orig"); err != nil {
			t.Fatal(err)
		}
		if err := os.Rename(other, dst); err != nil {
			t.Fatal(err)
		}
	}
	t.Cleanup(func() { afterHomeValidated = func(string) {} })

	if _, err := AddIdentity(home, "new1", src, "codex"); err == nil || !strings.Contains(err.Error(), "changed while it was being opened") {
		t.Fatalf("swapped home accepted: %v", err)
	}
	// Nothing was written into either directory.
	for _, dir := range []string{home + ".orig", home} {
		if _, err := os.Stat(filepath.Join(dir, "identities")); err == nil {
			t.Fatalf("identities written into %s", dir)
		}
	}
}
