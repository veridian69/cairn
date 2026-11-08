package invite

import (
	"errors"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/veridian69/cairn/garden/internal/identityseal"
)

const token = "cairn1.abc_DEF-123.xyz"

func newCode(t *testing.T) Code {
	t.Helper()
	secret, err := NewSecret()
	if err != nil {
		t.Fatal(err)
	}
	return Code{Endpoint: "https://garden.example.net:8443/mcp", CASHA256: strings.Repeat("ab", 32), Secret: secret}
}

func TestCodeRoundTrip(t *testing.T) {
	c := newCode(t)
	text, err := c.Encode()
	if err != nil {
		t.Fatal(err)
	}
	if !strings.HasPrefix(text, Prefix) {
		t.Fatalf("code %q lacks prefix", text)
	}
	got, err := Decode("  " + text + "\n")
	if err != nil {
		t.Fatal(err)
	}
	if got.Endpoint != c.Endpoint || got.CASHA256 != c.CASHA256 || string(got.Secret) != string(c.Secret) {
		t.Fatalf("round trip changed the code: %+v", got)
	}
	if got.EnrolURL() != "https://garden.example.net:8443/enrol" {
		t.Fatalf("enrol URL %q", got.EnrolURL())
	}
}

func TestDecodeRejectsMalformedCodes(t *testing.T) {
	c := newCode(t)
	for name, mutate := range map[string]func(*Code){
		"http":       func(c *Code) { c.Endpoint = "http://garden.example.net/mcp" },
		"path":       func(c *Code) { c.Endpoint = "https://garden.example.net/other" },
		"credential": func(c *Code) { c.Endpoint = "https://u:p@garden.example.net/mcp" },
		"digest":     func(c *Code) { c.CASHA256 = "AB" },
		"secret":     func(c *Code) { c.Secret = c.Secret[:16] },
	} {
		bad := c
		mutate(&bad)
		if _, err := bad.Encode(); err == nil {
			t.Errorf("%s: Encode accepted an invalid code", name)
		}
	}
	for _, text := range []string{"", "garden1:", "garden2:e30", "garden1:!!", "garden1:e30", Prefix + strings.Repeat("A", 4096)} {
		if _, err := Decode(text); !errors.Is(err, ErrInvalidCode) {
			t.Errorf("Decode(%.20q) = %v, want ErrInvalidCode", text, err)
		}
	}
}

func TestSealOpen(t *testing.T) {
	c := newCode(t)
	now := time.Date(2026, 11, 8, 12, 0, 0, 0, time.UTC)
	r, err := Seal(c.Secret, token, "operator", "opencode", "5b0c7f3e-8a2d-4c1b-9e6f-0123456789ab", now.Add(time.Hour))
	if err != nil {
		t.Fatal(err)
	}
	if strings.Contains(r.Sealed, "cairn1") || r.ID != ID(c.Secret) {
		t.Fatalf("record leaks the token or has the wrong ID: %+v", r)
	}
	got, err := r.Open(c.Secret, now)
	if err != nil || got != token {
		t.Fatalf("Open = %q, %v", got, err)
	}
	if _, err = r.Open(c.Secret, now.Add(time.Hour)); !errors.Is(err, ErrExpired) {
		t.Fatalf("expired Open = %v", err)
	}
	other, _ := NewSecret()
	if _, err = r.Open(other, now); err == nil {
		t.Fatal("Open accepted another secret")
	}
	tampered := r
	tampered.Name = "spike"
	if _, err = tampered.Open(c.Secret, now); err == nil {
		t.Fatal("Open accepted a record whose name was changed")
	}
	tampered = r
	tampered.ExpiresAt = now.Add(48 * time.Hour).Format(time.RFC3339)
	if _, err = tampered.Open(c.Secret, now.Add(2*time.Hour)); err == nil {
		t.Fatal("Open accepted a record whose expiry was extended")
	}
}

func TestSealRefusesNonCredentials(t *testing.T) {
	c := newCode(t)
	for _, bad := range []string{"", "token", "cairn1.a b", "cairn1.a\n"} {
		if _, err := Seal(c.Secret, bad, "operator", "", "p", time.Now().Add(time.Hour)); err == nil {
			t.Errorf("Seal accepted %q", bad)
		}
	}
	if _, err := Seal(c.Secret, token, "bad name", "", "p", time.Now().Add(time.Hour)); err == nil {
		t.Error("Seal accepted an invalid name")
	}
	if _, err := Seal(c.Secret, token, "operator", "vim", "p", time.Now().Add(time.Hour)); err == nil {
		t.Error("Seal accepted an unknown harness")
	}
}

func privateDir(t *testing.T) string {
	t.Helper()
	dir := filepath.Join(t.TempDir(), "invites")
	if err := os.Mkdir(dir, 0700); err != nil {
		t.Fatal(err)
	}
	return dir
}

func TestWriteClaimIsSingleUse(t *testing.T) {
	dir := privateDir(t)
	c := newCode(t)
	r, err := Seal(c.Secret, token, "zen", "opencode", "p", time.Now().Add(time.Hour))
	if err != nil {
		t.Fatal(err)
	}
	if err = Write(dir, r); err != nil {
		t.Fatal(err)
	}
	if err = Write(dir, r); err == nil {
		t.Fatal("Write replaced an existing record")
	}
	info, err := os.Stat(filepath.Join(dir, r.ID+".json"))
	if err != nil || info.Mode().Perm() != 0600 {
		t.Fatalf("record mode %v, %v", info.Mode(), err)
	}
	var wg sync.WaitGroup
	results := make(chan error, 8)
	for i := 0; i < 8; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			_, err := Claim(dir, c.Secret)
			results <- err
		}()
	}
	wg.Wait()
	close(results)
	won := 0
	for err := range results {
		if err == nil {
			won++
		} else if !errors.Is(err, ErrNotFound) {
			t.Fatalf("Claim: %v", err)
		}
	}
	if won != 1 {
		t.Fatalf("%d claims succeeded, want exactly 1", won)
	}
	entries, _ := os.ReadDir(dir)
	if len(entries) != 0 {
		t.Fatalf("claim left %d files behind", len(entries))
	}
}

func TestCheckDirAndPrune(t *testing.T) {
	dir := privateDir(t)
	if err := os.Chmod(dir, 0750); err != nil {
		t.Fatal(err)
	}
	if err := CheckDir(dir); err == nil {
		t.Fatal("CheckDir accepted a group-readable directory")
	}
	_ = os.Chmod(dir, 0700)
	now := time.Now()
	for i, ttl := range []time.Duration{-time.Minute, time.Hour} {
		c := newCode(t)
		r, err := Seal(c.Secret, token, []string{"old", "new"}[i], "", "p", now.Add(ttl))
		if err != nil {
			t.Fatal(err)
		}
		if err = Write(dir, r); err != nil {
			t.Fatal(err)
		}
	}
	removed, err := Prune(dir, now)
	if err != nil || removed != 1 {
		t.Fatalf("Prune removed %d, %v", removed, err)
	}
}

func TestCodeCarriesIdentityHints(t *testing.T) {
	c := newCode(t)
	c.Name, c.Harness = "operator", "cli"
	text, err := c.Encode()
	if err != nil {
		t.Fatal(err)
	}
	got, err := Decode(text)
	if err != nil || got.Name != "operator" || got.Harness != "cli" {
		t.Fatalf("hints lost: %+v, %v", got, err)
	}
	c.Harness = "vim"
	if _, err = c.Encode(); err == nil {
		t.Fatal("Encode accepted an unknown harness hint")
	}
}

// A cli invite carries only a passphrase-sealed identity, never a plain token.
func TestCLIInviteCarriesOnlyASealedIdentity(t *testing.T) {
	c := newCode(t)
	now := time.Now()
	if _, err := Seal(c.Secret, token, "operator", "cli", "p", now.Add(time.Hour)); err == nil {
		t.Fatal("Seal accepted a plain token for a cli identity")
	}
	sealed, err := identityseal.Seal("operator", token, []byte("correct horse battery staple"))
	if err != nil {
		t.Fatal(err)
	}
	r, err := SealIdentity(c.Secret, sealed, "operator", "p", now.Add(time.Hour))
	if err != nil {
		t.Fatal(err)
	}
	got, err := r.Open(c.Secret, now)
	if err != nil || got != string(sealed) || r.Payload != PayloadSealedIdentity || r.Harness != "cli" {
		t.Fatalf("Open = %.40q, %v, payload %q", got, err, r.Payload)
	}
	if _, err := SealIdentity(c.Secret, []byte(token), "operator", "p", now.Add(time.Hour)); err == nil {
		t.Fatal("SealIdentity accepted a plain token")
	}
	tampered := r
	tampered.Payload = PayloadToken
	if _, err := tampered.Open(c.Secret, now); err == nil {
		t.Fatal("Open accepted a record whose payload kind was changed")
	}
}
