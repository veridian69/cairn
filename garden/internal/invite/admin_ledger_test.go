package invite

import (
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"syscall"
	"testing"
	"time"

	"github.com/google/uuid"
)

// ledgerTestDir returns a fresh directory with the owner-only mode a gateway configuration
// directory has, and the ledger path inside it.
func ledgerTestDir(t *testing.T) (string, string) {
	t.Helper()
	dir := t.TempDir()
	if err := os.Chmod(dir, 0700); err != nil {
		t.Fatal(err)
	}
	return dir, filepath.Join(dir, LedgerFile)
}

func ledgerTestEntry(name string) LedgerEntry {
	now := time.Now()
	return NewLedgerEntry(name, uuid.NewString(), uuid.NewString(), uuid.NewString(), now, now.Add(time.Hour))
}

func TestAdminLedgerRoundTripAndHalfMarks(t *testing.T) {
	_, path := ledgerTestDir(t)
	principal := uuid.NewString()
	a := NewLedgerEntry("kay", principal, uuid.NewString(), uuid.NewString(), time.Now(), time.Now().Add(time.Hour))
	b := NewLedgerEntry("kay", principal, uuid.NewString(), uuid.NewString(), time.Now(), time.Now().Add(time.Hour))
	other := ledgerTestEntry("zen")
	for _, e := range []LedgerEntry{a, b, other} {
		if err := AppendLedger(path, e); err != nil {
			t.Fatal(err)
		}
	}
	entries, err := ReadLedger(path)
	if err != nil || len(entries) != 3 {
		t.Fatalf("ReadLedger = %d entries, %v", len(entries), err)
	}
	if info, err := os.Stat(path); err != nil || info.Mode().Perm() != 0600 {
		t.Fatalf("ledger mode: %v, %v", info, err)
	}
	if got := PendingFor(entries, principal); len(got) != 2 {
		t.Fatalf("PendingFor = %d, want 2", len(got))
	}

	// Marking a credential leaves its grant pending, and only that entry changes.
	if ok, err := MarkLedger(path, LedgerCredential, a.CredentialID, time.Now()); err != nil || !ok {
		t.Fatalf("mark credential: %v, %v", ok, err)
	}
	entries, _ = ReadLedger(path)
	if entries[0].CredentialPending() || !entries[0].GrantPending() {
		t.Fatalf("credential mark: %+v", entries[0])
	}
	if !entries[1].CredentialPending() || !entries[1].GrantPending() || !entries[2].CredentialPending() {
		t.Fatalf("other entries changed: %+v", entries)
	}
	if ok, err := MarkLedger(path, LedgerCredential, a.CredentialID, time.Now()); err != nil || ok {
		t.Fatalf("a second credential mark reported %v, %v", ok, err)
	}
	if ok, err := MarkLedger(path, LedgerGrant, a.GrantID, time.Now()); err != nil || !ok {
		t.Fatalf("mark grant: %v, %v", ok, err)
	}
	entries, _ = ReadLedger(path)
	if entries[0].CredentialPending() || entries[0].GrantPending() {
		t.Fatalf("both halves should be marked: %+v", entries[0])
	}
	if got := PendingFor(entries, principal); len(got) != 1 || got[0].CredentialID != b.CredentialID {
		t.Fatalf("PendingFor after marks: %+v", got)
	}
	if _, err := MarkLedger(path, "bogus", a.GrantID, time.Now()); err == nil {
		t.Fatal("an unknown half was accepted")
	}
}

func TestAdminLedgerLegacyRevokedAtMarksBothHalves(t *testing.T) {
	_, path := ledgerTestDir(t)
	line := `{"name":"kay","principal_id":"` + uuid.NewString() + `","grant_id":"` + uuid.NewString() +
		`","credential_id":"` + uuid.NewString() + `","issued_at":"2026-11-01T10:00:00Z","expires_at":"2027-11-01T10:00:00Z","revoked_at":"2026-11-02T10:00:00Z"}` + "\n"
	if err := os.WriteFile(path, []byte(line), 0600); err != nil {
		t.Fatal(err)
	}
	entries, err := ReadLedger(path)
	if err != nil || len(entries) != 1 {
		t.Fatalf("legacy line: %d entries, %v", len(entries), err)
	}
	if entries[0].CredentialPending() || entries[0].GrantPending() || len(PendingFor(entries, entries[0].PrincipalID)) != 0 {
		t.Fatalf("a revoked_at line must count as both halves revoked: %+v", entries[0])
	}
}

func TestAdminLedgerRefusesMalformedLines(t *testing.T) {
	for name, content := range map[string]string{
		"not json":      "{\n",
		"unknown field": `{"name":"kay","principal_id":"x","token":"cairn1.leak"}` + "\n",
		"bad uuid":      `{"name":"kay","principal_id":"nope","grant_id":"nope","credential_id":"nope","issued_at":"2026-11-08T10:00:00Z","expires_at":"2027-11-08T10:00:00Z"}` + "\n",
		"bad time":      `{"name":"kay","principal_id":"` + uuid.NewString() + `","grant_id":"` + uuid.NewString() + `","credential_id":"` + uuid.NewString() + `","issued_at":"yesterday","expires_at":"2027-11-08T10:00:00Z"}` + "\n",
	} {
		_, path := ledgerTestDir(t)
		if err := os.WriteFile(path, []byte(content), 0600); err != nil {
			t.Fatal(err)
		}
		if _, err := ReadLedger(path); err == nil {
			t.Errorf("%s: malformed ledger was read", name)
		}
	}
}

// The ledger decides what gets revoked, so a file or directory that the gateway or another user
// could have written must be refused, on read and on write.
func TestAdminLedgerRefusesUnsafeFilesAndDirectories(t *testing.T) {
	cases := map[string]func(t *testing.T, dir, path string){
		"group-writable file": func(t *testing.T, dir, path string) {
			writeEntryFile(t, path, 0660)
		},
		"world-readable file": func(t *testing.T, dir, path string) {
			writeEntryFile(t, path, 0644)
		},
		"symlink": func(t *testing.T, dir, path string) {
			target := filepath.Join(dir, "real.jsonl")
			writeEntryFile(t, target, 0600)
			if err := os.Symlink(target, path); err != nil {
				t.Fatal(err)
			}
		},
		"group-writable directory": func(t *testing.T, dir, path string) {
			writeEntryFile(t, path, 0600)
			if err := os.Chmod(dir, 0770); err != nil {
				t.Fatal(err)
			}
		},
		"file owned by another uid": func(t *testing.T, dir, path string) {
			writeEntryFile(t, path, 0600)
			withOtherUID(t)
		},
		"directory owned by another uid": func(t *testing.T, dir, path string) {
			withOtherUID(t)
		},
	}
	for name, setup := range cases {
		t.Run(name, func(t *testing.T) {
			dir, path := ledgerTestDir(t)
			setup(t, dir, path)
			if _, err := ReadLedger(path); err == nil {
				t.Fatal("ReadLedger accepted an unsafe ledger")
			}
			if err := AppendLedger(path, ledgerTestEntry("kay")); err == nil {
				t.Fatal("AppendLedger wrote to an unsafe ledger")
			}
		})
	}
}

// A new ledger is created owned by the effective uid, never handed to another owner, and the
// directory is synced after the first creation (exercised, not observable without a crash).
func TestAdminLedgerCreatedFileIsOwnedByEffectiveUID(t *testing.T) {
	_, path := ledgerTestDir(t)
	if err := AppendLedger(path, ledgerTestEntry("kay")); err != nil {
		t.Fatal(err)
	}
	info, err := os.Stat(path)
	if err != nil {
		t.Fatal(err)
	}
	if int(info.Sys().(*syscall.Stat_t).Uid) != os.Geteuid() || info.Mode().Perm() != 0600 {
		t.Fatalf("ledger owner %d mode %v", info.Sys().(*syscall.Stat_t).Uid, info.Mode())
	}
}

func writeEntryFile(t *testing.T, path string, mode os.FileMode) {
	t.Helper()
	line := `{"name":"kay","principal_id":"` + uuid.NewString() + `","grant_id":"` + uuid.NewString() +
		`","credential_id":"` + uuid.NewString() + `","issued_at":"2026-11-01T10:00:00Z","expires_at":"2027-11-01T10:00:00Z"}` + "\n"
	if err := os.WriteFile(path, []byte(line), mode); err != nil {
		t.Fatal(err)
	}
	if err := os.Chmod(path, mode); err != nil {
		t.Fatal(err)
	}
}

// withOtherUID makes the effective uid differ from the owner of the files and directory, which
// simulates a ledger that some other user created.
func withOtherUID(t *testing.T) {
	t.Helper()
	orig := effectiveUID
	effectiveUID = func() int { return os.Geteuid() + 1 }
	t.Cleanup(func() { effectiveUID = orig })
}

func TestAdminLedgerPathIsBesideTheConfiguration(t *testing.T) {
	if got := LedgerPath("/etc/garden/garden.json"); got != "/etc/garden/garden-issued.jsonl" {
		t.Fatalf("LedgerPath = %q", got)
	}
	if strings.Contains(LedgerPath("/x/y/garden.json"), "invites") {
		t.Fatal("the ledger must not live in the invites directory")
	}
}

// A valid ledger whose last record has no newline (edited by hand) must stay valid after the
// next append (review finding, 8 November 2026).
func TestAdminLedgerAppendAfterUnterminatedLine(t *testing.T) {
	_, path := ledgerTestDir(t)
	first, err := json.Marshal(ledgerTestEntry("ann"))
	if err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(path, first, 0600); err != nil {
		t.Fatal(err)
	}
	if _, err := ReadLedger(path); err != nil {
		t.Fatalf("unterminated ledger refused: %v", err)
	}
	if err := AppendLedger(path, ledgerTestEntry("ben")); err != nil {
		t.Fatal(err)
	}
	entries, err := ReadLedger(path)
	if err != nil || len(entries) != 2 || entries[0].Name != "ann" || entries[1].Name != "ben" {
		t.Fatalf("after append: %+v, %v", entries, err)
	}
}
