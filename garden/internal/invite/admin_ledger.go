package invite

import (
	"bufio"
	"encoding/json"
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

// The issuance ledger records the principal, grant and credential of every invite and reissue, so
// that --reissue can revoke the earlier ones. It is not secret, but it decides what gets revoked,
// so it is root's file, not the gateway's: it lives beside the gateway configuration, at
// <config dir>/garden-issued.jsonl, never in the invites directory the gateway writes. Every read
// and write checks the descriptor and the directory: a regular file owned by the effective uid with
// no group or other permission bits, in a directory that is not writable by group or others and is
// owned by the effective uid or root. Callers hold the configuration directory lock. The ledger
// holds no token and no secret.

// LedgerFile is the ledger's name beside the gateway configuration.
const LedgerFile = "garden-issued.jsonl"

// The two halves of an entry that revocation marks separately.
const (
	LedgerCredential = "credential"
	LedgerGrant      = "grant"
)

const maxLedgerBytes = 8 << 20

// effectiveUID is the owner that the ledger and its directory must have. It is a variable so that
// tests can exercise the ownership refusals without root.
var effectiveUID = os.Geteuid

// LedgerEntry is one issuance: the credential and grant that one invite or reissue created. Each
// half is marked revoked separately, after its own successful revocation. RevokedAt is the
// format before the halves were tracked separately; it marks both halves.
type LedgerEntry struct {
	Name                string `json:"name"`
	PrincipalID         string `json:"principal_id"`
	GrantID             string `json:"grant_id"`
	CredentialID        string `json:"credential_id"`
	IssuedAt            string `json:"issued_at"`
	ExpiresAt           string `json:"expires_at"`
	CredentialRevokedAt string `json:"credential_revoked_at,omitempty"`
	GrantRevokedAt      string `json:"grant_revoked_at,omitempty"`
	RevokedAt           string `json:"revoked_at,omitempty"`
}

// LedgerPath returns where the ledger lives for a gateway configuration at gatewayConfig.
func LedgerPath(gatewayConfig string) string {
	return filepath.Join(filepath.Dir(gatewayConfig), LedgerFile)
}

// CredentialPending reports whether the entry's credential is not yet recorded as revoked.
func (e LedgerEntry) CredentialPending() bool {
	return e.CredentialRevokedAt == "" && e.RevokedAt == ""
}

// GrantPending reports whether the entry's grant is not yet recorded as revoked.
func (e LedgerEntry) GrantPending() bool {
	return e.GrantRevokedAt == "" && e.RevokedAt == ""
}

// NewLedgerEntry returns an entry for an issuance at issued, with the credential's expiry.
func NewLedgerEntry(name, principalID, grantID, credentialID string, issued, expires time.Time) LedgerEntry {
	return LedgerEntry{
		Name:         name,
		PrincipalID:  principalID,
		GrantID:      grantID,
		CredentialID: credentialID,
		IssuedAt:     issued.UTC().Truncate(time.Second).Format(time.RFC3339),
		ExpiresAt:    expires.UTC().Truncate(time.Second).Format(time.RFC3339),
	}
}

func (e LedgerEntry) validate() error {
	if !ValidName(e.Name) {
		return errors.New("invalid issuance ledger entry: name")
	}
	for _, id := range []string{e.PrincipalID, e.GrantID, e.CredentialID} {
		if u, err := uuid.Parse(id); err != nil || u.String() != id {
			return errors.New("invalid issuance ledger entry: identifier")
		}
	}
	for _, ts := range []string{e.IssuedAt, e.ExpiresAt, e.CredentialRevokedAt, e.GrantRevokedAt, e.RevokedAt} {
		if ts == "" {
			continue
		}
		if _, err := time.Parse(time.RFC3339, ts); err != nil {
			return errors.New("invalid issuance ledger entry: time")
		}
	}
	if e.IssuedAt == "" || e.ExpiresAt == "" {
		return errors.New("invalid issuance ledger entry: time")
	}
	return nil
}

// ReadLedger returns every entry in the ledger at path, in file order. A missing ledger is empty.
// A malformed line, or a ledger or directory that fails the ownership checks, is an error: the
// revocation trail would be incomplete or not trustworthy.
func ReadLedger(path string) ([]LedgerEntry, error) {
	if err := checkLedgerDir(filepath.Dir(path)); err != nil {
		return nil, err
	}
	f, err := os.OpenFile(path, os.O_RDONLY|syscall.O_NOFOLLOW, 0)
	if errors.Is(err, fs.ErrNotExist) {
		return nil, nil
	}
	if err != nil {
		return nil, ledgerOpenError(path, err)
	}
	defer f.Close()
	if err := checkLedgerFile(f); err != nil {
		return nil, err
	}
	data, err := io.ReadAll(io.LimitReader(f, maxLedgerBytes+1))
	if err != nil {
		return nil, err
	}
	if len(data) > maxLedgerBytes {
		return nil, errors.New("the issuance ledger is too large")
	}
	return parseLedger(data)
}

func parseLedger(data []byte) ([]LedgerEntry, error) {
	var entries []LedgerEntry
	scanner := bufio.NewScanner(strings.NewReader(string(data)))
	scanner.Buffer(make([]byte, 0, 4096), 64*1024)
	for scanner.Scan() {
		line := scanner.Text()
		if line == "" {
			continue
		}
		dec := json.NewDecoder(strings.NewReader(line))
		dec.DisallowUnknownFields()
		var e LedgerEntry
		if err := dec.Decode(&e); err != nil || dec.More() {
			return nil, errors.New("the issuance ledger has a malformed line")
		}
		if err := e.validate(); err != nil {
			return nil, err
		}
		entries = append(entries, e)
	}
	if err := scanner.Err(); err != nil {
		return nil, errors.New("the issuance ledger has a malformed line")
	}
	return entries, nil
}

// AppendLedger adds one entry to the ledger at path. A new file is created with mode 0600, owned by
// the effective uid, and the directory is synced so that the new name survives a crash.
func AppendLedger(path string, e LedgerEntry) error {
	if err := e.validate(); err != nil {
		return err
	}
	dir := filepath.Dir(path)
	if err := checkLedgerDir(dir); err != nil {
		return err
	}
	line, err := json.Marshal(e)
	if err != nil {
		return err
	}
	_, statErr := os.Lstat(path)
	created := errors.Is(statErr, fs.ErrNotExist)
	f, err := os.OpenFile(path, os.O_RDWR|os.O_APPEND|os.O_CREATE|syscall.O_NOFOLLOW, 0600)
	if err != nil {
		return ledgerOpenError(path, err)
	}
	defer f.Close()
	if created {
		if err := f.Chmod(0600); err != nil {
			return err
		}
		if err := f.Sync(); err != nil {
			return err
		}
		if err := syncDir(dir); err != nil {
			return err
		}
	}
	if err := checkLedgerFile(f); err != nil {
		return err
	}
	// A valid ledger may end without a newline (written by hand); appending straight after its
	// closing brace would join two records on one line and corrupt it.
	if info, err := f.Stat(); err == nil && info.Size() > 0 {
		last := make([]byte, 1)
		if _, err := f.ReadAt(last, info.Size()-1); err != nil {
			return err
		}
		if last[0] != '\n' {
			line = append([]byte{'\n'}, line...)
		}
	}
	if _, err := f.Write(append(line, '\n')); err != nil {
		return err
	}
	return f.Sync()
}

// MarkLedger records that the credential or grant identified by id, of the given half, has been
// revoked in Cairn. It reports false when no entry has that half pending, and otherwise rewrites
// the ledger atomically. The caller holds the directory lock.
func MarkLedger(path, half, id string, at time.Time) (bool, error) {
	if half != LedgerCredential && half != LedgerGrant {
		return false, errors.New("unknown ledger half")
	}
	entries, err := ReadLedger(path)
	if err != nil {
		return false, err
	}
	stamp := at.UTC().Truncate(time.Second).Format(time.RFC3339)
	marked := false
	for i := range entries {
		e := &entries[i]
		if half == LedgerCredential && e.CredentialID == id && e.CredentialPending() {
			e.CredentialRevokedAt = stamp
			marked = true
		}
		if half == LedgerGrant && e.GrantID == id && e.GrantPending() {
			e.GrantRevokedAt = stamp
			marked = true
		}
	}
	if !marked {
		return false, nil
	}
	return true, rewriteLedger(path, entries)
}

// PendingFor returns the entries for principalID that still have a credential or a grant not
// recorded as revoked, in file order.
func PendingFor(entries []LedgerEntry, principalID string) []LedgerEntry {
	var out []LedgerEntry
	for _, e := range entries {
		if e.PrincipalID == principalID && (e.CredentialPending() || e.GrantPending()) {
			out = append(out, e)
		}
	}
	return out
}

func rewriteLedger(path string, entries []LedgerEntry) error {
	var buf []byte
	for _, e := range entries {
		line, err := json.Marshal(e)
		if err != nil {
			return err
		}
		buf = append(append(buf, line...), '\n')
	}
	dir := filepath.Dir(path)
	tmp, err := os.CreateTemp(dir, ".garden-issued-*.tmp")
	if err != nil {
		return err
	}
	tmpPath := tmp.Name()
	renamed := false
	defer func() {
		if !renamed {
			_ = os.Remove(tmpPath)
		}
	}()
	err = tmp.Chmod(0600)
	if err == nil {
		_, err = tmp.Write(buf)
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
	if err := os.Rename(tmpPath, path); err != nil {
		return err
	}
	renamed = true
	return syncDir(dir)
}

// checkLedgerDir requires a real directory that group and others cannot write, owned by the
// effective uid or root.
func checkLedgerDir(dir string) error {
	info, err := os.Lstat(dir)
	if err != nil {
		return fmt.Errorf("the gateway configuration directory: %w", err)
	}
	stat, ok := info.Sys().(*syscall.Stat_t)
	if !ok || !info.IsDir() {
		return errors.New("the gateway configuration directory is not a directory")
	}
	if info.Mode().Perm()&0022 != 0 {
		return errors.New("the gateway configuration directory is writable by group or others; refusing to trust the issuance ledger")
	}
	if stat.Uid != 0 && int(stat.Uid) != effectiveUID() {
		return errors.New("the gateway configuration directory is owned by neither root nor the invoking user; refusing to trust the issuance ledger")
	}
	return nil
}

// checkLedgerFile checks the open descriptor, so that the file checked is the file read.
func checkLedgerFile(f *os.File) error {
	info, err := f.Stat()
	if err != nil || !info.Mode().IsRegular() {
		return errors.New("the issuance ledger must be a regular file")
	}
	stat, ok := info.Sys().(*syscall.Stat_t)
	if !ok || int(stat.Uid) != effectiveUID() {
		return errors.New("the issuance ledger is not owned by the invoking user; refusing to trust it")
	}
	if info.Mode().Perm()&0077 != 0 {
		return errors.New("the issuance ledger has group or other permission bits; it must be mode 0600")
	}
	return nil
}

func ledgerOpenError(path string, err error) error {
	if errors.Is(err, syscall.ELOOP) {
		return fmt.Errorf("the issuance ledger %s is a symbolic link; refusing to follow it", path)
	}
	return fmt.Errorf("the issuance ledger %s cannot be opened: %w", path, err)
}
