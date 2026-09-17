//go:build !windows

package credentials

import (
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"syscall"
	"testing"
)

func TestReadSecretFileRejectsSymlink(t *testing.T) {
	t.Helper()
	base := t.TempDir()
	target := writeSecret(t, base, "target", []byte("relay-token"), 0600)
	link := filepath.Join(base, "link")
	if err := os.Symlink(target, link); err != nil {
		t.Fatalf("os.Symlink() unexpected error: %v", err)
	}

	if _, err := ReadSecretFile(link, "local token", os.Geteuid(), 4096); err == nil {
		t.Fatal("expected error for symlink path")
	}
}

func TestReadSecretFileRejectsBadMode(t *testing.T) {
	t.Helper()
	path := writeSecret(t, t.TempDir(), "badmode", []byte("relay-token"), 0644)
	_, err := ReadSecretFile(path, "local token", os.Geteuid(), 4096)
	if err == nil {
		t.Fatal("expected error for non-0600 secret mode")
	}
	if got, want := err.Error(), "must have mode 0600"; !strings.Contains(got, want) {
		t.Fatalf("error %q, want containing %q", got, want)
	}
}

func TestReadSecretFileDoesNotCloseReusedDescriptorDuringFinalization(t *testing.T) {
	path := writeSecret(t, t.TempDir(), "secret", []byte("value"), 0600)
	if _, err := ReadSecretFile(path, "secret", os.Geteuid(), 4096); err != nil {
		t.Fatalf("ReadSecretFile() unexpected error: %v", err)
	}

	reusedFD, err := syscall.Open(os.DevNull, syscall.O_RDONLY|syscall.O_CLOEXEC, 0)
	if err != nil {
		t.Fatalf("open replacement descriptor: %v", err)
	}
	defer syscall.Close(reusedFD)

	for range 10 {
		runtime.GC()
		runtime.Gosched()
	}

	var stat syscall.Stat_t
	if err := syscall.Fstat(reusedFD, &stat); err != nil {
		t.Fatalf("credential file finalizer closed reused descriptor: %v", err)
	}
}

func writeSecret(t *testing.T, base, name string, value []byte, mode os.FileMode) string {
	t.Helper()
	path := filepath.Join(base, name)
	if err := os.WriteFile(path, value, mode); err != nil {
		t.Fatalf("os.WriteFile() unexpected error: %v", err)
	}
	return path
}
