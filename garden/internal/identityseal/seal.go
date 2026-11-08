// Package identityseal seals a Garden identity's token under a passphrase (scrypt, AES-256-GCM),
// for human identities that agents running as the same account must not use. It has no
// dependencies on the rest of Garden, so both the client and `garden invite` can use it.
package identityseal

import (
	"crypto/aes"
	"crypto/cipher"
	"crypto/rand"
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"regexp"
	"strings"
	"unicode/utf8"

	"golang.org/x/crypto/scrypt"
)

// participantName mirrors the gateway's participant rule.
var participantName = regexp.MustCompile(`^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,63}$`)

const (
	sealedVersion       = 1
	scryptN, scryptR    = 1 << 17, 8 // 128 MiB and about half a second per guess
	scryptP             = 1
	minPassphraseLength = 12
	MaxSealedBytes      = 16 * 1024
)

// SealedIdentity is the content of identities/<name>.sealed.
type SealedIdentity struct {
	Version int    `json:"version"`
	KDF     string `json:"kdf"`
	N       int    `json:"n"`
	R       int    `json:"r"`
	P       int    `json:"p"`
	Salt    string `json:"salt"`
	Nonce   string `json:"nonce"`
	Sealed  string `json:"sealed"`
}

func sealedAAD(name string) []byte { return []byte("garden-identity-v1\x00" + name) }

func identityAEAD(passphrase []byte, salt []byte, n, r, p int) (cipher.AEAD, error) {
	key, err := scrypt.Key(passphrase, salt, n, r, p, 32)
	if err != nil {
		return nil, err
	}
	block, err := aes.NewCipher(key)
	if err != nil {
		return nil, err
	}
	return cipher.NewGCM(block)
}

// SealIdentityToken encrypts token for name under passphrase and returns the file content.
func Seal(name, token string, passphrase []byte) ([]byte, error) {
	if !participantName.MatchString(name) {
		return nil, errors.New("invalid Garden identity name")
	}
	if err := CheckPassphrase(passphrase); err != nil {
		return nil, err
	}
	if !ValidToken(token) {
		return nil, errors.New("invalid credential")
	}
	salt := make([]byte, 16)
	if _, err := rand.Read(salt); err != nil {
		return nil, err
	}
	aead, err := identityAEAD(passphrase, salt, scryptN, scryptR, scryptP)
	if err != nil {
		return nil, err
	}
	nonce := make([]byte, aead.NonceSize())
	if _, err = rand.Read(nonce); err != nil {
		return nil, err
	}
	s := SealedIdentity{Version: sealedVersion, KDF: "scrypt", N: scryptN, R: scryptR, P: scryptP,
		Salt: base64.RawStdEncoding.EncodeToString(salt), Nonce: base64.RawStdEncoding.EncodeToString(nonce),
		Sealed: base64.RawStdEncoding.EncodeToString(aead.Seal(nil, nonce, []byte(token), sealedAAD(name)))}
	out, err := json.MarshalIndent(s, "", "  ")
	if err != nil {
		return nil, err
	}
	return append(out, '\n'), nil
}

// ErrWrongPassphrase reports a passphrase that does not open the sealed identity.
var ErrWrongPassphrase = errors.New("wrong passphrase for this Garden identity")

// OpenIdentityToken decrypts a sealed identity file for name.
func Open(name string, data, passphrase []byte) (string, error) {
	s, err := parse(data)
	if err != nil {
		return "", err
	}
	salt, _ := base64.RawStdEncoding.DecodeString(s.Salt)
	nonce, _ := base64.RawStdEncoding.DecodeString(s.Nonce)
	sealed, _ := base64.RawStdEncoding.DecodeString(s.Sealed)
	aead, err := identityAEAD(passphrase, salt, s.N, s.R, s.P)
	if err != nil {
		return "", err
	}
	if len(nonce) != aead.NonceSize() {
		return "", errors.New("invalid sealed Garden identity")
	}
	token, err := aead.Open(nil, nonce, sealed, sealedAAD(name))
	if err != nil {
		return "", ErrWrongPassphrase
	}
	return string(token), nil
}

// parse checks a sealed identity's structure and parameters.
func parse(data []byte) (SealedIdentity, error) {
	bad := errors.New("invalid sealed Garden identity")
	if len(data) > MaxSealedBytes {
		return SealedIdentity{}, bad
	}
	var s SealedIdentity
	dec := json.NewDecoder(strings.NewReader(string(data)))
	dec.DisallowUnknownFields()
	if dec.Decode(&s) != nil || dec.More() || s.Version != sealedVersion || s.KDF != "scrypt" ||
		s.N < 16384 || s.N > 1<<20 || s.N&(s.N-1) != 0 || s.R < 1 || s.R > 32 || s.P < 1 || s.P > 16 {
		return SealedIdentity{}, bad
	}
	salt, e1 := base64.RawStdEncoding.DecodeString(s.Salt)
	nonce, e2 := base64.RawStdEncoding.DecodeString(s.Nonce)
	sealed, e3 := base64.RawStdEncoding.DecodeString(s.Sealed)
	if e1 != nil || e2 != nil || e3 != nil || len(salt) < 16 || len(nonce) != 12 || len(sealed) < 17 {
		return SealedIdentity{}, bad
	}
	return s, nil
}

// validToken mirrors the bearer-token rule garden.Dial enforces.
func ValidToken(token string) bool {
	if token == "" || len(token) > 4096 {
		return false
	}
	for _, b := range []byte(token) {
		if b < 33 || b > 126 || b == ',' {
			return false
		}
	}
	return true
}

func CheckPassphrase(p []byte) error {
	if !utf8.Valid(p) || utf8.RuneCount(p) < minPassphraseLength {
		return fmt.Errorf("the passphrase must be at least %d characters", minPassphraseLength)
	}
	return nil
}

// Check reports whether data is a well-formed sealed identity, without opening it.
func Check(data []byte) error {
	_, err := parse(data)
	return err
}
