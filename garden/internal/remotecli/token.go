package remotecli

import (
	"errors"
	"fmt"
	"io"
	"io/fs"
	"os"
	"path/filepath"
	"syscall"

	"github.com/veridian69/cairn/garden/internal/attention"
)

// passphraseAttempts is how many passphrases a sealed identity accepts in one command.
const passphraseAttempts = 3

// maxSealedBytes matches the attention package's limit on a sealed identity file.
const maxSealedBytes = 16 * 1024

// identityToken returns the bearer token for name. A name has a .token file or a .sealed file,
// never both. A sealed identity asks for its passphrase on the terminal, once per command, and
// the passphrase is never taken from arguments, the environment or standard input.
func identityToken(home attention.Home, name string) (string, error) {
	dir := filepath.Join(home.Dir, "identities")
	tokenPath := filepath.Join(dir, name+".token")
	sealedPath := filepath.Join(dir, name+".sealed")
	hasToken, err := entryExists(tokenPath)
	if err != nil {
		return "", err
	}
	hasSealed, err := entryExists(sealedPath)
	if err != nil {
		return "", err
	}
	switch {
	case hasToken && hasSealed:
		return "", fmt.Errorf("identity %s has both a .token and a .sealed file; keep only one", name)
	case hasSealed:
		return unlockSealed(name, dir, sealedPath)
	case hasToken:
		token, err := attention.ReadCredential(tokenPath)
		if err != nil {
			return "", fmt.Errorf("identity %s: %w", name, err)
		}
		return token, nil
	default:
		return "", fmt.Errorf("no identity file for %s in %s; run `garden join` or `garden home add-identity` first", name, dir)
	}
}

// entryExists reports whether path exists as a directory entry, so a dangling symlink counts and
// is then refused by the reader.
func entryExists(path string) (bool, error) {
	_, err := os.Lstat(path)
	if err == nil {
		return true, nil
	}
	if errors.Is(err, fs.ErrNotExist) {
		return false, nil
	}
	return false, errors.New("cannot inspect the Garden identity directory")
}

// unlockSealed reads the sealed file and asks for the passphrase up to passphraseAttempts times.
// Only a wrong passphrase is retried; any other failure is returned at once.
func unlockSealed(name, dir, path string) (string, error) {
	data, err := readSealedFile(dir, path)
	if err != nil {
		return "", err
	}
	prompt := fmt.Sprintf("passphrase for %s: ", name)
	for attempt := 1; attempt <= passphraseAttempts; attempt++ {
		passphrase, err := readPassphrase(prompt, false)
		if err != nil {
			return "", err
		}
		token, err := attention.OpenIdentityToken(name, data, passphrase)
		clear(passphrase)
		if err == nil {
			return token, nil
		}
		if !errors.Is(err, attention.ErrWrongPassphrase) {
			return "", fmt.Errorf("identity %s: %w", name, err)
		}
		if attempt < passphraseAttempts {
			fmt.Fprintln(os.Stderr, "wrong passphrase, try again")
		}
	}
	return "", fmt.Errorf("wrong passphrase for %s after %d attempts", name, passphraseAttempts)
}

// readSealedFile reads identities/<name>.sealed through one no-follow descriptor. The file must be
// a regular file owned by this user with mode 0600 or stricter, and its directory must be owned by
// this user or root and not writable by group or others. The Garden home itself was checked by
// attention.LoadHome.
func readSealedFile(dir, path string) ([]byte, error) {
	if err := checkIdentityDir(dir); err != nil {
		return nil, err
	}
	file, err := os.OpenFile(path, os.O_RDONLY|syscall.O_NOFOLLOW|syscall.O_NONBLOCK, 0)
	if err != nil {
		return nil, errors.New("cannot open the sealed identity (a symlink is refused)")
	}
	defer file.Close()
	info, err := file.Stat()
	if err != nil {
		return nil, errors.New("cannot inspect the sealed identity")
	}
	if !info.Mode().IsRegular() {
		return nil, errors.New("the sealed identity must be a regular file")
	}
	if !ownedByCaller(info) {
		return nil, errors.New("the sealed identity must be owned by this user")
	}
	if info.Mode().Perm()&0077 != 0 {
		return nil, errors.New("the sealed identity must be private (mode 0600)")
	}
	data, err := io.ReadAll(io.LimitReader(file, maxSealedBytes+1))
	if err != nil || len(data) > maxSealedBytes {
		return nil, errors.New("cannot read the sealed identity or it is oversized")
	}
	return data, nil
}

// checkIdentityDir requires identities/ to be a real directory owned by this user or root and not
// writable by group or others, unless sticky.
func checkIdentityDir(dir string) error {
	info, err := os.Lstat(dir)
	if err != nil || info.Mode()&os.ModeSymlink != 0 || !info.IsDir() {
		return errors.New("the Garden identities directory must be a real directory")
	}
	if !ownedByCallerOrRoot(info) {
		return errors.New("the Garden identities directory must be owned by this user or root")
	}
	if info.Mode().Perm()&0022 != 0 && info.Mode()&os.ModeSticky == 0 {
		return errors.New("the Garden identities directory must not be writable by group or others")
	}
	return nil
}

// checkCAFile applies the harness rule for a pinned TLS CA: an absolute, clean regular file owned by
// root or this user, not writable by group or others, with every parent directory owned by root or
// this user and likewise not writable (unless sticky). An empty path is no pin.
func checkCAFile(path string) error {
	if path == "" {
		return nil
	}
	if !filepath.IsAbs(path) || filepath.Clean(path) != path {
		return errors.New("the Garden TLS CA file path must be absolute, without . or .. components")
	}
	for current := filepath.Dir(path); ; current = filepath.Dir(current) {
		info, err := os.Lstat(current)
		if err != nil || info.Mode()&os.ModeSymlink != 0 || !info.IsDir() {
			return errors.New("a parent of the Garden TLS CA file is not a plain directory")
		}
		if !ownedByCallerOrRoot(info) {
			return errors.New("a parent of the Garden TLS CA file is owned by another user")
		}
		if info.Mode().Perm()&0022 != 0 && info.Mode()&os.ModeSticky == 0 {
			return errors.New("a parent of the Garden TLS CA file is group- or other-writable")
		}
		if current == filepath.Dir(current) {
			break
		}
	}
	info, err := os.Lstat(path)
	if err != nil || !info.Mode().IsRegular() {
		return errors.New("the Garden TLS CA file must be a regular file, not a symlink")
	}
	if !ownedByCallerOrRoot(info) {
		return errors.New("the Garden TLS CA file must be owned by root or this user")
	}
	if info.Mode().Perm()&0022 != 0 {
		return errors.New("the Garden TLS CA file must not be writable by group or others")
	}
	return nil
}

func ownedByCaller(info fs.FileInfo) bool {
	stat, ok := info.Sys().(*syscall.Stat_t)
	return ok && stat.Uid == uint32(os.Getuid())
}

func ownedByCallerOrRoot(info fs.FileInfo) bool {
	stat, ok := info.Sys().(*syscall.Stat_t)
	return ok && (stat.Uid == 0 || stat.Uid == uint32(os.Getuid()))
}
