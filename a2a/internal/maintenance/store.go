package maintenance

import (
	"fmt"
	"os"
	"path/filepath"
	"syscall"
)

// StoreLockName is persistent runtime metadata, never an independently removable lock.
const StoreLockName = ".a2a-nats.lock"

// ValidateStoreLock accepts only an empty, owner-private regular lock file.
// A missing lock is valid for older or not-yet-started runtimes.
func ValidateStoreLock(directory string) error {
	info, err := os.Lstat(filepath.Join(directory, StoreLockName))
	if os.IsNotExist(err) {
		return nil
	}
	if err != nil {
		return err
	}
	return validateStoreInfo(info)
}
func validateStoreInfo(info os.FileInfo) error {
	st, ok := info.Sys().(*syscall.Stat_t)
	if !ok || !info.Mode().IsRegular() || info.Mode().Perm() != 0600 || info.Size() != 0 || st.Uid != uint32(os.Geteuid()) {
		return fmt.Errorf("invalid runtime store lock: expected owned empty mode 0600 regular file")
	}
	return nil
}

// AcquireStore excludes daemon startup and maintenance on the same physical store.
// Unlike an installation lease, this also covers callers using another config directory.
func AcquireStore(directory string) (*Lease, error) {
	if err := os.MkdirAll(directory, 0700); err != nil {
		return nil, err
	}
	path := filepath.Join(directory, StoreLockName)
	fd, err := syscall.Open(path, syscall.O_CREAT|syscall.O_RDWR|syscall.O_NOFOLLOW|syscall.O_NONBLOCK|syscall.O_CLOEXEC, 0600)
	if err != nil {
		return nil, err
	}
	file := os.NewFile(uintptr(fd), path)
	fail := func(err error) (*Lease, error) { _ = file.Close(); return nil, err }
	info, err := file.Stat()
	if err != nil {
		return fail(err)
	}
	if err = validateStoreInfo(info); err != nil {
		return fail(err)
	}
	if err = syscall.Flock(fd, syscall.LOCK_EX|syscall.LOCK_NB); err != nil {
		return fail(&InUseError{Path: path, Mode: Exclusive})
	}
	current, err := os.Lstat(path)
	if err != nil {
		return fail(err)
	}
	if !os.SameFile(info, current) {
		return fail(fmt.Errorf("runtime store lock was replaced"))
	}
	return &Lease{file: file}, nil
}

// PreserveStoreLock links the held store lock into an owned staging directory.
// Atomic directory exchange then preserves its inode, so competing daemon starts
// cannot acquire a fresh lock during reset/restore or their cleanup window.
func (l *Lease) PreserveStoreLock(stage string) error {
	if l == nil || l.file == nil || filepath.Base(l.file.Name()) != StoreLockName {
		return fmt.Errorf("store lease required")
	}
	source, err := l.file.Stat()
	if err != nil {
		return err
	}
	current, err := os.Lstat(l.file.Name())
	if err != nil {
		return err
	}
	if !os.SameFile(source, current) {
		return fmt.Errorf("runtime store lock was replaced")
	}
	target := filepath.Join(stage, StoreLockName)
	return os.Link(l.file.Name(), target)
}
