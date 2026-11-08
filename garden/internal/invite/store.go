package invite

import (
	"encoding/json"
	"errors"
	"io"
	"io/fs"
	"os"
	"path/filepath"
	"strings"
	"syscall"
	"time"
)

// The invites directory holds one <id>.json per unredeemed invite. The gateway's service account
// owns it with mode 0700; `garden invite` runs as root and gives each record to the directory's
// owner, so the gateway can claim it. Claiming renames the record out of its name first, which
// makes redemption single-use even when two requests race.

const maxRecordBytes = 64 * 1024

// ErrNotFound reports that no unredeemed invite matches the secret.
var ErrNotFound = errors.New("no such Garden invite")

// CheckDir requires a real directory that no one but its owner can write or read.
func CheckDir(dir string) error {
	if !filepath.IsAbs(dir) {
		return errors.New("the invites directory must be absolute")
	}
	info, err := os.Lstat(dir)
	if err != nil {
		return errors.New("the invites directory does not exist")
	}
	if info.Mode()&fs.ModeSymlink != 0 || !info.IsDir() || info.Mode().Perm()&0077 != 0 {
		return errors.New("the invites directory must be a directory with mode 0700")
	}
	return nil
}

// Write stores r as a new record, owned by the directory's owner, mode 0600.
func Write(dir string, r Record) error {
	if err := CheckDir(dir); err != nil {
		return err
	}
	if !idPattern.MatchString(r.ID) {
		return errors.New("invalid Garden invite record")
	}
	data, err := json.MarshalIndent(r, "", "  ")
	if err != nil {
		return err
	}
	info, err := os.Lstat(dir)
	if err != nil {
		return err
	}
	stat, _ := info.Sys().(*syscall.Stat_t)
	tmp, err := os.CreateTemp(dir, ".invite-*")
	if err != nil {
		return err
	}
	defer os.Remove(tmp.Name())
	if err = tmp.Chmod(0600); err == nil && stat != nil && int(stat.Uid) != os.Getuid() {
		err = tmp.Chown(int(stat.Uid), int(stat.Gid))
	}
	if err == nil {
		_, err = tmp.Write(append(data, '\n'))
	}
	if err == nil {
		err = tmp.Sync()
	}
	if closeErr := tmp.Close(); err == nil {
		err = closeErr
	}
	if err != nil {
		return err
	}
	// Link, not rename: an existing record with the same ID is never replaced.
	if err = os.Link(tmp.Name(), filepath.Join(dir, r.ID+".json")); err != nil {
		return errors.New("a Garden invite with this ID already exists")
	}
	return syncDir(dir)
}

// Claim removes the record for secret from the directory and returns it. Only one caller can
// claim a record. The caller decides what to do with an expired or mismatching record; it is
// gone either way, so a failed redemption cannot be retried against the same code.
func Claim(dir string, secret []byte) (Record, error) {
	if err := CheckDir(dir); err != nil {
		return Record{}, err
	}
	id := ID(secret)
	claimed := filepath.Join(dir, "."+id+".claimed")
	if err := os.Rename(filepath.Join(dir, id+".json"), claimed); err != nil {
		if errors.Is(err, fs.ErrNotExist) {
			return Record{}, ErrNotFound
		}
		return Record{}, err
	}
	defer os.Remove(claimed)
	// The rename is the single-use guarantee, so it must survive a crash before the token is
	// handed out; otherwise the original entry could come back and be redeemed again.
	if err := syncDir(dir); err != nil {
		return Record{}, errors.New("cannot make the Garden invite claim durable")
	}
	return readRecord(claimed)
}

func readRecord(path string) (Record, error) {
	f, err := os.OpenFile(path, os.O_RDONLY|syscall.O_NOFOLLOW, 0)
	if err != nil {
		return Record{}, err
	}
	defer f.Close()
	info, err := f.Stat()
	if err != nil || !info.Mode().IsRegular() {
		return Record{}, errors.New("invalid Garden invite record")
	}
	data, err := io.ReadAll(io.LimitReader(f, maxRecordBytes+1))
	if err != nil || len(data) > maxRecordBytes {
		return Record{}, errors.New("invalid Garden invite record")
	}
	var r Record
	dec := json.NewDecoder(strings.NewReader(string(data)))
	dec.DisallowUnknownFields()
	if dec.Decode(&r) != nil {
		return Record{}, errors.New("invalid Garden invite record")
	}
	return r, nil
}

// Prune deletes records that expired before now and returns how many it removed. Records it
// cannot read are left for the operator.
func Prune(dir string, now time.Time) (int, error) {
	if err := CheckDir(dir); err != nil {
		return 0, err
	}
	entries, err := os.ReadDir(dir)
	if err != nil {
		return 0, err
	}
	removed := 0
	for _, e := range entries {
		name := e.Name()
		if !strings.HasSuffix(name, ".json") || !idPattern.MatchString(strings.TrimSuffix(name, ".json")) {
			continue
		}
		r, err := readRecord(filepath.Join(dir, name))
		if err != nil {
			continue
		}
		expires, err := time.Parse(time.RFC3339, r.ExpiresAt)
		if err == nil && !now.Before(expires) && os.Remove(filepath.Join(dir, name)) == nil {
			removed++
		}
	}
	return removed, nil
}

func syncDir(dir string) error {
	d, err := os.Open(dir)
	if err != nil {
		return err
	}
	defer d.Close()
	return d.Sync()
}
