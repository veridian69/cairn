package identityseal

import (
	"errors"
	"strings"
	"testing"
)

const testToken = "cairn1.abc_DEF-123.xyz"

func TestSealOpenRoundTrip(t *testing.T) {
	pass := []byte("correct horse battery staple")
	data, err := Seal("operator", testToken, pass)
	if err != nil {
		t.Fatal(err)
	}
	if strings.Contains(string(data), "cairn1") {
		t.Fatal("the sealed identity contains the plaintext token")
	}
	if err := Check(data); err != nil {
		t.Fatalf("Check refused a sealed identity: %v", err)
	}
	got, err := Open("operator", data, pass)
	if err != nil || got != testToken {
		t.Fatalf("Open = %q, %v", got, err)
	}
	if _, err := Open("operator", data, []byte("wrong horse battery staple")); !errors.Is(err, ErrWrongPassphrase) {
		t.Fatalf("wrong passphrase: %v", err)
	}
	if _, err := Open("spike", data, pass); !errors.Is(err, ErrWrongPassphrase) {
		t.Fatalf("opened under another name: %v", err)
	}
}

func TestSealRefusesWeakInputAndCheckRefusesMalformedFiles(t *testing.T) {
	if _, err := Seal("operator", testToken, []byte("short")); err == nil {
		t.Fatal("accepted a short passphrase")
	}
	if _, err := Seal("operator", "not a token", []byte("long enough passphrase")); err == nil {
		t.Fatal("accepted a non-credential")
	}
	if _, err := Seal("bad name", testToken, []byte("long enough passphrase")); err == nil {
		t.Fatal("accepted an invalid name")
	}
	for _, bad := range []string{``, `{}`, `{"version":1,"kdf":"scrypt","n":3,"r":8,"p":1,"salt":"","nonce":"","sealed":""}`, `{"version":1,"extra":1}`, testToken} {
		if Check([]byte(bad)) == nil {
			t.Fatalf("Check accepted %q", bad)
		}
	}
}
