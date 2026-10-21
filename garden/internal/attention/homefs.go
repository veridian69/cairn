package attention

import (
	"bytes"
	"errors"
	"fmt"
	"io"
	"io/fs"
	"os"
	"path/filepath"
	"strings"
	"syscall"
	"time"

	"github.com/google/uuid"
)

// This file holds the confinement standard for installing material into a Garden home.
// Every write goes through an *os.Root, so no component can resolve outside the home.
// Every source is opened once with O_NOFOLLOW, checked on that descriptor, and read from
// the same descriptor. Nothing here changes the claim or runtime lookup semantics.

// ownedByUser reports whether the file is owned by the user running this process.
func ownedByUser(info fs.FileInfo) bool {
	stat, ok := info.Sys().(*syscall.Stat_t)
	return ok && stat.Uid == uint32(os.Getuid())
}

// checkHomeDir requires the Garden home to be a real directory owned by this user that no one
// else can write. It may be readable by others (an older daemon created ~/.garden as 0755): the
// secrets live in identities/, which checkPrivateDir keeps at exactly 0700.
func checkHomeDir(info fs.FileInfo) error {
	if info.Mode()&os.ModeSymlink != 0 || !info.IsDir() {
		return errors.New("the Garden home must be a directory, not a symlink or file")
	}
	if !ownedByUser(info) {
		return errors.New("the Garden home must be owned by this user")
	}
	if info.Mode().Perm()&0022 != 0 {
		return errors.New("the Garden home must not be writable by group or others")
	}
	return nil
}

// checkPrivateDir requires a real directory, mode exactly 0700, owned by this user.
func checkPrivateDir(info fs.FileInfo, what string) error {
	if info.Mode()&os.ModeSymlink != 0 || !info.IsDir() {
		return fmt.Errorf("%s must be a directory, not a symlink or file", what)
	}
	if info.Mode().Perm() != 0700 {
		return fmt.Errorf("%s must have mode 0700", what)
	}
	if !ownedByUser(info) {
		return fmt.Errorf("%s must be owned by this user", what)
	}
	return nil
}

// afterHomeValidated is a test seam. It runs after the home has been validated by path and
// before it is opened, which is exactly where a racing rename would have to land.
var afterHomeValidated = func(string) {}

// checkAncestors applies the StrictModes-style rule to every parent of dst, up to /. Each must
// be a real directory owned by root or by this user, and must not be group- or other-writable
// unless it has the sticky bit. Then no other user can replace a path component.
func checkAncestors(dst string) error {
	return checkTrustedParents(dst, "the Garden home")
}

// checkTrustedParents applies checkAncestors' rule to every parent of path; what names the path
// in errors.
func checkTrustedParents(path, what string) error {
	uid := uint32(os.Getuid())
	for current := filepath.Dir(path); ; current = filepath.Dir(current) {
		info, err := os.Lstat(current)
		if err != nil {
			return fmt.Errorf("cannot inspect a parent of %s", what)
		}
		if info.Mode()&os.ModeSymlink != 0 || !info.IsDir() {
			return fmt.Errorf("a parent of %s is not a plain directory: %s", what, current)
		}
		stat, ok := info.Sys().(*syscall.Stat_t)
		if !ok || (stat.Uid != 0 && stat.Uid != uid) {
			return fmt.Errorf("a parent of %s is owned by another user: %s", what, current)
		}
		if info.Mode().Perm()&0022 != 0 && info.Mode()&os.ModeSticky == 0 {
			return fmt.Errorf("a parent of %s is group- or other-writable: %s", what, current)
		}
		if current == filepath.Dir(current) {
			return nil
		}
	}
}

// existingHome opens the Garden home as a confined root. A missing home returns nil, nil.
// The home is validated by path, opened, and then pinned: the open root must be the same
// directory that validation saw, so a swap in between is refused.
func existingHome(dst string) (*os.Root, error) {
	if err := checkAncestors(dst); err != nil {
		return nil, err
	}
	info, err := os.Lstat(dst)
	if errors.Is(err, fs.ErrNotExist) {
		return nil, nil
	}
	if err != nil {
		return nil, errors.New("cannot inspect the Garden home")
	}
	if err := checkHomeDir(info); err != nil {
		return nil, err
	}
	afterHomeValidated(dst)
	root, err := os.OpenRoot(dst)
	if err != nil {
		return nil, errors.New("cannot open the Garden home")
	}
	pinned, err := root.Stat(".")
	if err != nil || !os.SameFile(info, pinned) || checkHomeDir(pinned) != nil {
		_ = root.Close()
		return nil, errors.New("the Garden home changed while it was being opened; nothing was written")
	}
	return root, nil
}

// ensureHome creates the Garden home as a 0700 directory when it is missing, then opens
// it confined. The parent must already exist; the home itself is created with Mkdir alone.
func ensureHome(dst string) (*os.Root, error) {
	root, err := existingHome(dst)
	if err != nil || root != nil {
		return root, err
	}
	// A concurrent creator may win the Mkdir; then the directory is verified like any other.
	if err := os.Mkdir(dst, 0700); err != nil && !errors.Is(err, fs.ErrExist) {
		return nil, errors.New("cannot create the Garden home; its parent must exist")
	}
	root, err = existingHome(dst)
	if err != nil {
		return nil, err
	}
	if root == nil {
		return nil, errors.New("the Garden home disappeared while it was being created")
	}
	return root, nil
}

// rootPrivateDir creates name as a 0700 directory inside root, or verifies an existing one.
func rootPrivateDir(root *os.Root, name string) error {
	info, err := root.Lstat(name)
	if errors.Is(err, fs.ErrNotExist) {
		if err := root.Mkdir(name, 0700); err != nil {
			return fmt.Errorf("cannot create %s", name)
		}
		info, err = root.Lstat(name)
	}
	if err != nil {
		return fmt.Errorf("cannot inspect %s", name)
	}
	return checkPrivateDir(info, name)
}

// rootExistingPrivateDir verifies name only if it already exists, without creating it.
func rootExistingPrivateDir(root *os.Root, name string) error {
	if root == nil {
		return nil
	}
	info, err := root.Lstat(name)
	if errors.Is(err, fs.ErrNotExist) {
		return nil
	}
	if err != nil {
		return fmt.Errorf("cannot inspect %s", name)
	}
	return checkPrivateDir(info, name)
}

// rootReadFile reads rel through root with one O_NOFOLLOW descriptor, checked by fstat.
// A missing file returns exists=false. private requires mode with no group or other bits.
func rootReadFile(root *os.Root, rel string, limit int64, private bool) ([]byte, bool, error) {
	if root == nil {
		return nil, false, nil
	}
	info, err := root.Lstat(rel)
	if errors.Is(err, fs.ErrNotExist) {
		return nil, false, nil
	}
	if err != nil {
		return nil, false, fmt.Errorf("cannot inspect %s", rel)
	}
	if !info.Mode().IsRegular() {
		return nil, false, fmt.Errorf("existing path is not a regular file: %s", rel)
	}
	file, err := root.OpenFile(rel, os.O_RDONLY|syscall.O_NOFOLLOW|syscall.O_NONBLOCK, 0)
	if err != nil {
		return nil, false, fmt.Errorf("cannot open %s safely", rel)
	}
	defer file.Close()
	opened, err := file.Stat()
	if err != nil || !opened.Mode().IsRegular() || !os.SameFile(info, opened) || !ownedByUser(opened) || (private && opened.Mode().Perm()&0077 != 0) {
		return nil, false, fmt.Errorf("existing file is unsafe: %s", rel)
	}
	data, err := io.ReadAll(io.LimitReader(file, limit+1))
	if err != nil {
		return nil, false, fmt.Errorf("cannot read %s", rel)
	}
	if int64(len(data)) > limit {
		return nil, false, fmt.Errorf("existing file is too large: %s", rel)
	}
	return data, true, nil
}

// rootCreateNew writes a new 0600 file inside root. O_EXCL refuses anything already present,
// including a symlink at that name, so nothing is followed or replaced.
func rootCreateNew(root *os.Root, rel string, data []byte) error {
	file, err := root.OpenFile(rel, os.O_WRONLY|os.O_CREATE|os.O_EXCL, 0600)
	if err != nil {
		return fmt.Errorf("cannot create %s: %w", rel, err)
	}
	_, err = file.Write(data)
	if err == nil {
		err = file.Sync()
	}
	if closeErr := file.Close(); err == nil {
		err = closeErr
	}
	if err != nil {
		_ = root.Remove(rel)
		return fmt.Errorf("cannot write %s", rel)
	}
	return nil
}

// acceptEqual handles a publication that found rel already present: it is accepted only
// when the existing bytes are identical, so a concurrent identical writer is harmless.
func acceptEqual(root *os.Root, rel string, data []byte, private bool) error {
	current, exists, err := rootReadFile(root, rel, max(int64(len(data)), 64*1024), private)
	if err != nil || !exists || !sameBytes(current, data) {
		return fmt.Errorf("existing file differs; refusing to overwrite: %s", rel)
	}
	return nil
}

// rootPublishNew creates rel with data, or accepts an identical existing file. It reports
// whether this call created the file.
func rootPublishNew(root *os.Root, rel string, data []byte, private bool) (bool, error) {
	err := rootCreateNew(root, rel, data)
	if err == nil {
		return true, nil
	}
	if !errors.Is(err, fs.ErrExist) {
		return false, err
	}
	return false, acceptEqual(root, rel, data, private)
}

// rootPublishLink publishes data at rel without replacing anything. It writes a private
// temporary file, hard-links it to rel (which fails if rel exists), and removes the temporary
// either way. It reports whether this call created rel.
func rootPublishLink(root *os.Root, rel string, data []byte, private bool) (bool, error) {
	tmp := "." + rel + "." + uuid.NewString() + ".tmp"
	if err := rootCreateNew(root, tmp, data); err != nil {
		return false, err
	}
	defer func() { _ = root.Remove(tmp) }()
	err := root.Link(tmp, rel)
	if err == nil {
		return true, nil
	}
	if !errors.Is(err, fs.ErrExist) {
		return false, fmt.Errorf("cannot publish %s", rel)
	}
	return false, acceptEqual(root, rel, data, private)
}

// rootReplace writes rel through a new temporary name inside root, then renames it over rel.
// The caller has already verified that rel is a regular file or absent.
func rootReplace(root *os.Root, rel string, data []byte) error {
	tmp := "." + rel + "." + uuid.NewString() + ".tmp"
	if err := rootCreateNew(root, tmp, data); err != nil {
		return err
	}
	if err := root.Rename(tmp, rel); err != nil {
		_ = root.Remove(tmp)
		return fmt.Errorf("cannot replace %s", rel)
	}
	return nil
}

// privateSourceBytes opens a source token once, with O_NOFOLLOW, checks that same descriptor
// (regular, owned by this user, no group or other bits) and returns exactly the bytes read
// from it. The bytes are never printed.
func privateSourceBytes(path string, limit int64) ([]byte, error) {
	data, err := ownedSourceBytes(path, limit, "token file", true)
	if err != nil {
		return nil, err
	}
	if err := checkCredentialBytes(data); err != nil {
		return nil, err
	}
	return data, nil
}

// ownedSourceBytes reads one import source through a single O_NOFOLLOW descriptor and checks
// that descriptor: a regular file owned by this user, with no group or other bits when
// private, otherwise with no group or other write bit. Its parents must pass checkAncestors'
// rule first, so no other user can swap a path component between that check and the open;
// checking the descriptor that is read covers the file itself.
func ownedSourceBytes(path string, limit int64, what string, private bool) ([]byte, error) {
	abs, err := filepath.Abs(path)
	if err != nil {
		return nil, fmt.Errorf("invalid %s path", what)
	}
	if err := checkTrustedParents(abs, "the "+what); err != nil {
		return nil, err
	}
	file, err := os.OpenFile(abs, os.O_RDONLY|syscall.O_NOFOLLOW|syscall.O_NONBLOCK, 0)
	if err != nil {
		return nil, fmt.Errorf("cannot open the %s (a symlink is refused)", what)
	}
	defer file.Close()
	info, err := file.Stat()
	if err != nil {
		return nil, fmt.Errorf("cannot inspect the %s", what)
	}
	if !info.Mode().IsRegular() {
		return nil, fmt.Errorf("the %s must be a regular file", what)
	}
	if !ownedByUser(info) {
		return nil, fmt.Errorf("the %s must be owned by this user", what)
	}
	if private && info.Mode().Perm()&0077 != 0 {
		return nil, fmt.Errorf("the %s must be private (mode 0600)", what)
	}
	if !private && info.Mode().Perm()&0022 != 0 {
		return nil, fmt.Errorf("the %s must not be writable by group or others", what)
	}
	data, err := io.ReadAll(io.LimitReader(file, limit+1))
	if err != nil {
		return nil, fmt.Errorf("cannot read the %s", what)
	}
	if int64(len(data)) > limit {
		return nil, fmt.Errorf("the %s is too large", what)
	}
	return data, nil
}

// checkCredentialBytes applies ReadCredential's rules to bytes already read.
func checkCredentialBytes(data []byte) error {
	value := strings.TrimSpace(string(data))
	if value == "" || strings.ContainsAny(value, "\r\n\x00") {
		return errors.New("invalid credential file")
	}
	return nil
}

// sameBytes reports whether two byte slices hold the same content.
func sameBytes(a, b []byte) bool { return bytes.Equal(a, b) }

// homeLockName is the lock file inside the Garden home. Its dot prefix and name keep it apart
// from the harness files (garden.json, tls/, identities/, run/) and from the daemon's files.
const homeLockName = ".home.lock"

// lockTimeout bounds how long a command waits for another Garden home command to finish.
var lockTimeout = 10 * time.Second

// afterHomeLocked is a test seam that runs while the home lock is held.
var afterHomeLocked = func() {}

// lockHome takes the exclusive home lock for one whole transaction: planning reads, every
// write, rollback and verification. The lock file is opened through the pinned root with
// O_NOFOLLOW, so a symlink in its place is refused. Waiting is bounded by lockTimeout.
func lockHome(root *os.Root) (func(), error) {
	file, err := root.OpenFile(homeLockName, os.O_RDWR|os.O_CREATE|syscall.O_NOFOLLOW, 0600)
	if err != nil {
		return nil, errors.New("cannot open the Garden home lock (a symlink is refused)")
	}
	info, err := file.Stat()
	if err != nil || !info.Mode().IsRegular() || !ownedByUser(info) || info.Mode().Perm()&0077 != 0 {
		_ = file.Close()
		return nil, errors.New("the Garden home lock must be a private regular file owned by this user")
	}
	deadline := time.Now().Add(lockTimeout)
	for {
		err := syscall.Flock(int(file.Fd()), syscall.LOCK_EX|syscall.LOCK_NB)
		if err == nil {
			break
		}
		if !errors.Is(err, syscall.EWOULDBLOCK) && !errors.Is(err, syscall.EAGAIN) {
			_ = file.Close()
			return nil, errors.New("cannot lock the Garden home")
		}
		if time.Now().After(deadline) {
			_ = file.Close()
			return nil, errors.New("another Garden home command is running; wait for it to finish and try again")
		}
		time.Sleep(25 * time.Millisecond)
	}
	afterHomeLocked()
	return func() {
		_ = syscall.Flock(int(file.Fd()), syscall.LOCK_UN)
		_ = file.Close()
	}, nil
}
