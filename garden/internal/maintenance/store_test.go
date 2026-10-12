package maintenance

import (
	"golang.org/x/sys/unix"
	"os"
	"path/filepath"
	"testing"
)

func TestStoreLeaseExcludesStartupAcrossExchangeAndInterruptedCleanup(t *testing.T) {
	root := t.TempDir()
	live := filepath.Join(root, "runtime")
	stage := filepath.Join(root, "stage")
	lease, err := AcquireStore(live)
	if err != nil {
		t.Fatal(err)
	}
	defer lease.Close()
	if err := os.Mkdir(stage, 0700); err != nil {
		t.Fatal(err)
	}
	if err := lease.PreserveStoreLock(stage); err != nil {
		t.Fatal(err)
	}
	if err := unix.Renameat2(unix.AT_FDCWD, live, unix.AT_FDCWD, stage, unix.RENAME_EXCHANGE); err != nil {
		t.Fatal(err)
	}
	for _, path := range []string{live, stage} {
		next, err := AcquireStore(path)
		if err == nil {
			next.Close()
			t.Fatal("exchange bypassed held store lease")
		}
	}
	// A failed/interrupted cleanup may retain both links. Releasing the old lease
	// must still allow only one daemon on the newly published runtime.
	if err := lease.Close(); err != nil {
		t.Fatal(err)
	}
	next, err := AcquireStore(live)
	if err != nil {
		t.Fatal(err)
	}
	defer next.Close()
	stale, err := AcquireStore(stage)
	if err == nil {
		stale.Close()
		t.Fatal("retained old path bypassed new runtime lock")
	}
	if err := os.RemoveAll(stage); err != nil {
		t.Fatal(err)
	}
	other, err := AcquireStore(live)
	if err == nil {
		other.Close()
		t.Fatal("old-directory cleanup bypassed active lock")
	}
}
