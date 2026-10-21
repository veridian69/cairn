package attention

import (
	"errors"
	"os"
	"path/filepath"
	"testing"
)

func TestClaimIsExclusiveUntilReleased(t *testing.T) {
	home := Home{Dir: t.TempDir()}
	first, err := home.ClaimIdentity("spike")
	if err != nil {
		t.Fatal(err)
	}
	if _, err := home.ClaimIdentity("spike"); !errors.Is(err, ErrIdentityClaimed) {
		t.Fatalf("second claim not refused: %v", err)
	}
	other, err := home.ClaimIdentity("val")
	if err != nil {
		t.Fatalf("distinct identity refused: %v", err)
	}
	_ = other.Close()
	if err := first.Close(); err != nil {
		t.Fatal(err)
	}
	again, err := home.ClaimIdentity("spike")
	if err != nil {
		t.Fatalf("claim not released on close: %v", err)
	}
	_ = again.Close()
}

func TestClaimUsesPrivateRunDirectoryAndLockFile(t *testing.T) {
	home := Home{Dir: filepath.Join(t.TempDir(), "garden")}
	claim, err := home.ClaimIdentity("spike")
	if err != nil {
		t.Fatal(err)
	}
	defer claim.Close()
	dirInfo, err := os.Lstat(filepath.Join(home.Dir, "run"))
	if err != nil || !dirInfo.IsDir() || dirInfo.Mode().Perm() != 0700 {
		t.Fatalf("run directory not private: %v %v", dirInfo, err)
	}
	fileInfo, err := os.Lstat(filepath.Join(home.Dir, "run", "spike.lock"))
	if err != nil || !fileInfo.Mode().IsRegular() || fileInfo.Mode().Perm() != 0600 {
		t.Fatalf("lock file not private: %v %v", fileInfo, err)
	}
}

func TestClaimRefusesSymlinkedLockFile(t *testing.T) {
	home := Home{Dir: t.TempDir()}
	if err := os.MkdirAll(filepath.Join(home.Dir, "run"), 0700); err != nil {
		t.Fatal(err)
	}
	target := filepath.Join(home.Dir, "elsewhere")
	if err := os.WriteFile(target, nil, 0600); err != nil {
		t.Fatal(err)
	}
	if err := os.Symlink(target, filepath.Join(home.Dir, "run", "spike.lock")); err != nil {
		t.Fatal(err)
	}
	if _, err := home.ClaimIdentity("spike"); err == nil || errors.Is(err, ErrIdentityClaimed) {
		t.Fatalf("symlinked lock accepted or misreported: %v", err)
	}
}

func TestClaimRefusesSymlinkedRunDirectory(t *testing.T) {
	home := Home{Dir: t.TempDir()}
	target := t.TempDir()
	if err := os.Symlink(target, filepath.Join(home.Dir, "run")); err != nil {
		t.Fatal(err)
	}
	if _, err := home.ClaimIdentity("spike"); err == nil {
		t.Fatal("symlinked run directory accepted")
	}
	if _, err := os.Lstat(filepath.Join(target, "spike.lock")); err == nil {
		t.Fatal("lock created through symlinked run directory")
	}
}
