package attention

import (
	"errors"
	"io"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"syscall"

	"github.com/google/uuid"
)

// ErrIdentityClaimed reports that another live session holds the identity.
var ErrIdentityClaimed = errors.New("identity is already claimed")

// Claim is an exclusive local lease on one Garden identity. The kernel drops
// the flock when the process exits, so a crash cannot leave a stale claim.
type Claim struct {
	file *os.File
	// consumerFile is run/<identity>.consumer, the gateway consumer record for this identity.
	consumerFile string
}

// ClaimIdentity takes a non-blocking exclusive flock on run/<identity>.lock.
// It never waits and never takes over an existing claim.
func (h Home) ClaimIdentity(identity string) (*Claim, error) {
	if !participantName.MatchString(identity) {
		return nil, errInvalidIdentity
	}
	run := filepath.Join(h.Dir, "run")
	if err := os.MkdirAll(run, 0700); err != nil {
		return nil, errors.New("cannot create the Garden run directory")
	}
	info, err := os.Lstat(run)
	if err != nil || !info.IsDir() || info.Mode().Perm()&0077 != 0 {
		return nil, errors.New("the Garden run directory must be a private directory")
	}
	// O_NOFOLLOW makes a planted symlink fail to open rather than redirect the lock.
	file, err := os.OpenFile(filepath.Join(run, identity+".lock"), os.O_RDWR|os.O_CREATE|syscall.O_NOFOLLOW, 0600)
	if err != nil {
		return nil, errors.New("cannot open the identity claim file")
	}
	info, err = file.Stat()
	if err != nil || !info.Mode().IsRegular() || info.Mode().Perm()&0077 != 0 {
		_ = file.Close()
		return nil, errors.New("the identity claim file must be a private regular file")
	}
	if err := syscall.Flock(int(file.Fd()), syscall.LOCK_EX|syscall.LOCK_NB); err != nil {
		_ = file.Close()
		if errors.Is(err, syscall.EWOULDBLOCK) || errors.Is(err, syscall.EAGAIN) {
			return nil, ErrIdentityClaimed
		}
		return nil, errors.New("cannot lock the identity claim file")
	}
	// The holder's PID lets a sibling Codex core ask it to yield (see CodexCore). It is advisory:
	// a reader checks the process before signalling it, and the flock alone decides the claim.
	if file.Truncate(0) != nil {
		_ = file.Close()
		return nil, errors.New("cannot write the identity claim file")
	}
	if _, err := file.WriteAt([]byte(strconv.Itoa(os.Getpid())), 0); err != nil {
		_ = file.Close()
		return nil, errors.New("cannot write the identity claim file")
	}
	return &Claim{file: file, consumerFile: filepath.Join(run, identity+".consumer")}, nil
}

// ClaimHolder returns the PID the current or last holder of the identity recorded. The process
// may have exited or the record may be stale, so the caller must check the process itself.
func (h Home) ClaimHolder(identity string) (int, bool) {
	if !participantName.MatchString(identity) {
		return 0, false
	}
	file, err := os.OpenFile(filepath.Join(h.Dir, "run", identity+".lock"), os.O_RDONLY|syscall.O_NOFOLLOW|syscall.O_NONBLOCK, 0)
	if err != nil {
		return 0, false
	}
	defer file.Close()
	info, err := file.Stat()
	if err != nil || !info.Mode().IsRegular() || info.Mode().Perm()&0077 != 0 {
		return 0, false
	}
	raw, err := io.ReadAll(io.LimitReader(file, 16))
	if err != nil {
		return 0, false
	}
	pid, err := strconv.Atoi(strings.TrimSpace(string(raw)))
	if err != nil || pid <= 0 {
		return 0, false
	}
	return pid, true
}

// ConsumerID returns the gateway consumer UUID for this identity, creating it on first
// use. The record is kept after the claim closes, so a restart on this machine presents
// the same consumer and renews its own lease at once; another machine has a different
// home and is refused until the lease expires. Only the flock holder reaches this code,
// so there is one writer at a time. A record that is not a private regular file owned by
// this user, or does not hold a canonical UUID, is removed and replaced. Removing a
// symlink removes the link, never its target.
func (c *Claim) ConsumerID() (string, error) {
	if c == nil || c.file == nil {
		return "", errors.New("no identity claim")
	}
	if id, ok := readConsumerRecord(c.consumerFile); ok {
		return id, nil
	}
	if err := os.Remove(c.consumerFile); err != nil && !errors.Is(err, os.ErrNotExist) {
		return "", errors.New("cannot replace the Garden consumer record")
	}
	// O_EXCL with O_NOFOLLOW refuses to follow anything that appears in the gap.
	file, err := os.OpenFile(c.consumerFile, os.O_RDWR|os.O_CREATE|os.O_EXCL|syscall.O_NOFOLLOW, 0600)
	if err != nil {
		return "", errors.New("cannot create the Garden consumer record")
	}
	id := uuid.NewString()
	_, err = file.WriteString(id)
	if err == nil {
		err = file.Sync()
	}
	if closeErr := file.Close(); err == nil {
		err = closeErr
	}
	if err != nil {
		return "", errors.New("cannot write the Garden consumer record")
	}
	return id, nil
}

// readConsumerRecord returns the stored UUID when the record is a private regular file
// owned by this user that holds exactly one canonical UUID.
func readConsumerRecord(path string) (string, bool) {
	// O_NONBLOCK keeps a FIFO or device planted as the record from blocking the open; regular files ignore it.
	file, err := os.OpenFile(path, os.O_RDONLY|syscall.O_NOFOLLOW|syscall.O_NONBLOCK, 0)
	if err != nil {
		return "", false
	}
	defer file.Close()
	info, err := file.Stat()
	if err != nil || !info.Mode().IsRegular() || info.Mode().Perm()&0077 != 0 {
		return "", false
	}
	if stat, ok := info.Sys().(*syscall.Stat_t); !ok || stat.Uid != uint32(os.Getuid()) {
		return "", false
	}
	raw, err := io.ReadAll(io.LimitReader(file, 64))
	if err != nil {
		return "", false
	}
	id := strings.TrimSpace(string(raw))
	u, err := uuid.Parse(id)
	if err != nil || u == uuid.Nil || u.String() != id {
		return "", false
	}
	return id, true
}

// Close releases the claim. It is safe to call on a nil Claim.
func (c *Claim) Close() error {
	if c == nil || c.file == nil {
		return nil
	}
	_ = syscall.Flock(int(c.file.Fd()), syscall.LOCK_UN)
	err := c.file.Close()
	c.file = nil
	return err
}
