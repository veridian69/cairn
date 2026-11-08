package attention

import (
	"errors"
	"os"
	"strings"
	"testing"
)

func TestSealedIdentityRoundTrip(t *testing.T) {
	pass := []byte("correct horse battery")
	data, err := SealIdentityToken("operator", "cairn1.abc_DEF-123.xyz", pass)
	if err != nil {
		t.Fatal(err)
	}
	if strings.Contains(string(data), "cairn1") {
		t.Fatal("sealed identity contains the plaintext token")
	}
	got, err := OpenIdentityToken("operator", data, pass)
	if err != nil || got != "cairn1.abc_DEF-123.xyz" {
		t.Fatalf("Open = %q, %v", got, err)
	}
	if _, err = OpenIdentityToken("operator", data, []byte("wrong horse battery")); !errors.Is(err, ErrWrongPassphrase) {
		t.Fatalf("wrong passphrase: %v", err)
	}
	if _, err = OpenIdentityToken("spike", data, pass); !errors.Is(err, ErrWrongPassphrase) {
		t.Fatalf("a sealed identity opened under another name: %v", err)
	}
}

func TestSealedIdentityRefusesWeakInput(t *testing.T) {
	if _, err := SealIdentityToken("operator", "cairn1.abc", []byte("short")); err == nil {
		t.Fatal("accepted a short passphrase")
	}
	if _, err := SealIdentityToken("operator", "not a token", []byte("long enough passphrase")); err == nil {
		t.Fatal("accepted a non-credential")
	}
	for _, bad := range []string{`{}`, `{"version":1,"kdf":"scrypt","n":3,"r":8,"p":1,"salt":"","nonce":"","sealed":""}`, `{"version":1,"extra":1}`} {
		if _, err := OpenIdentityToken("operator", []byte(bad), []byte("long enough passphrase")); err == nil {
			t.Fatalf("opened %s", bad)
		}
	}
}

func TestReadPassphraseNeedsTerminal(t *testing.T) {
	old := openTTY
	defer func() { openTTY = old }()
	openTTY = func() (*os.File, error) { return nil, errors.New("no tty") }
	if _, err := ReadPassphrase("passphrase: ", false); err == nil || !strings.Contains(err.Error(), "terminal") {
		t.Fatalf("ReadPassphrase without a terminal = %v", err)
	}
}
