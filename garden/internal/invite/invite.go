// Package invite holds the format shared by `garden invite`, the gateway's /enrol endpoint and
// `garden join`. An invite is a single-use secret. The operator's command stores the newly issued
// Cairn token sealed under a key derived from that secret, so the stored record is useless to
// anyone who reads it without the code. The code carries the public endpoint, the SHA-256 of the
// CA certificate the client must find in the gateway's TLS chain, and the secret.
package invite

import (
	"crypto/aes"
	"crypto/cipher"
	"crypto/hkdf"
	"crypto/rand"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"net/url"
	"regexp"
	"strings"
	"time"

	"github.com/veridian69/cairn/garden/internal/gardenauth"
	"github.com/veridian69/cairn/garden/internal/identityseal"
)

// Prefix starts every invite code; the digit is the format version.
const Prefix = "garden1:"

// Harnesses an invite may make an identity the default for. "cli" is the human command line:
// say, history, watch and chat with --remote.
var Harnesses = []string{"claude", "codex", "opencode", "cli"}

// MaxTTL bounds how long an unredeemed invite stays valid.
const MaxTTL = 7 * 24 * time.Hour

const (
	secretBytes   = 32
	maxCodeBytes  = 2048
	maxTokenBytes = 16384
	recordVersion = 1
)

var (
	namePattern   = regexp.MustCompile(`^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,63}$`)
	digestPattern = regexp.MustCompile(`^[0-9a-f]{64}$`)
	idPattern     = regexp.MustCompile(`^[0-9a-f]{64}$`)
)

// ErrInvalidCode reports a code that is not a well-formed invite. It never echoes the code.
var ErrInvalidCode = errors.New("not a valid Garden invite code")

// Code is the decoded content of an invite code.
type Code struct {
	Endpoint string `json:"e"`
	CASHA256 string `json:"c"`
	Secret   []byte `json:"-"`
	// Name and Harness repeat the invite's identity so that join can check the home and ask a
	// human identity's passphrase before it spends the invite. They are hints: the gateway's
	// sealed record is authoritative, and join refuses a response that disagrees with them.
	Name    string `json:"-"`
	Harness string `json:"-"`
}

type wireCode struct {
	Endpoint string `json:"e"`
	CASHA256 string `json:"c"`
	Secret   string `json:"s"`
	Name     string `json:"n,omitempty"`
	Harness  string `json:"h,omitempty"`
}

// NewSecret returns a fresh random invite secret.
func NewSecret() ([]byte, error) {
	secret := make([]byte, secretBytes)
	if _, err := rand.Read(secret); err != nil {
		return nil, err
	}
	return secret, nil
}

// Encode renders the code the operator hands to the client.
func (c Code) Encode() (string, error) {
	if err := c.validate(); err != nil {
		return "", err
	}
	raw, err := json.Marshal(wireCode{c.Endpoint, c.CASHA256, base64.RawURLEncoding.EncodeToString(c.Secret), c.Name, c.Harness})
	if err != nil {
		return "", err
	}
	return Prefix + base64.RawURLEncoding.EncodeToString(raw), nil
}

// Decode parses and validates a code. Surrounding whitespace is ignored.
func Decode(text string) (Code, error) {
	text = strings.TrimSpace(text)
	if len(text) > maxCodeBytes || !strings.HasPrefix(text, Prefix) {
		return Code{}, ErrInvalidCode
	}
	raw, err := base64.RawURLEncoding.DecodeString(strings.TrimPrefix(text, Prefix))
	if err != nil {
		return Code{}, ErrInvalidCode
	}
	var w wireCode
	dec := json.NewDecoder(strings.NewReader(string(raw)))
	dec.DisallowUnknownFields()
	if dec.Decode(&w) != nil || dec.More() {
		return Code{}, ErrInvalidCode
	}
	secret, err := base64.RawURLEncoding.DecodeString(w.Secret)
	if err != nil {
		return Code{}, ErrInvalidCode
	}
	c := Code{Endpoint: w.Endpoint, CASHA256: w.CASHA256, Secret: secret, Name: w.Name, Harness: w.Harness}
	if c.validate() != nil {
		return Code{}, ErrInvalidCode
	}
	return c, nil
}

func (c Code) validate() error {
	if len(c.Secret) != secretBytes {
		return errors.New("invite secret must be 32 bytes")
	}
	if (c.Name != "" && !ValidName(c.Name)) || !ValidHarness(c.Harness) {
		return errors.New("invalid invite name or harness")
	}
	if !digestPattern.MatchString(c.CASHA256) {
		return errors.New("invite CA digest must be 64 lowercase hex digits")
	}
	u, err := url.Parse(c.Endpoint)
	if err != nil || u.Scheme != "https" || u.Host == "" || u.User != nil || u.RawQuery != "" || u.Fragment != "" || u.Path != "/mcp" {
		return errors.New("invite endpoint must be an https URL ending in /mcp")
	}
	return nil
}

// EnrolURL is the redemption URL beside the MCP endpoint.
func (c Code) EnrolURL() string { return strings.TrimSuffix(c.Endpoint, "/mcp") + "/enrol" }

// ID names the stored record. It is derived from the secret, so a record can be found only by
// someone who holds the code, and the name reveals nothing about the key.
func ID(secret []byte) string {
	sum := sha256.Sum256(append([]byte("garden-invite-id\x00"), secret...))
	return hex.EncodeToString(sum[:])
}

func key(secret []byte) ([]byte, error) {
	return hkdf.Key(sha256.New, secret, nil, "garden invite v1 seal", 32)
}

// Record is the stored, sealed invite. It contains no plaintext credential.
type Record struct {
	Version      int    `json:"version"`
	ID           string `json:"id"`
	Name         string `json:"name"`
	Harness      string `json:"harness,omitempty"`
	PrincipalID  string `json:"principal_id"`
	GrantID      string `json:"grant_id,omitempty"`
	CredentialID string `json:"credential_id,omitempty"`
	ExpiresAt    string `json:"expires_at"`
	Nonce        string `json:"nonce"`
	Sealed       string `json:"sealed"`
	// Payload is PayloadToken (empty) for a Cairn token, or PayloadSealedIdentity for a human
	// identity that the operator already sealed under its passphrase at invite time, so the
	// plain token never leaves the gateway host.
	Payload string `json:"payload,omitempty"`
}

// Payload kinds for Record.Payload.
const (
	PayloadToken          = ""
	PayloadSealedIdentity = "sealed-identity"
)

// associated binds the ciphertext to the record fields that redemption relies on.
func (r Record) associated() []byte {
	return []byte(fmt.Sprintf("garden-invite-v%d\x00%s\x00%s\x00%s\x00%s\x00%s\x00%s", r.Version, r.ID, r.Name, r.Harness, r.PrincipalID, r.ExpiresAt, r.Payload))
}

// ValidName reports whether name is a valid Garden participant name.
func ValidName(name string) bool { return namePattern.MatchString(name) }

// ValidHarness reports whether harness is empty or one of Harnesses.
func ValidHarness(harness string) bool {
	if harness == "" {
		return true
	}
	for _, h := range Harnesses {
		if h == harness {
			return true
		}
	}
	return false
}

// Seal stores token under secret. expires is truncated to whole seconds, in UTC.
func Seal(secret []byte, token, name, harness, principalID string, expires time.Time) (Record, error) {
	if harness == "cli" {
		return Record{}, errors.New("a cli identity's token must be passphrase-sealed first; use SealIdentity")
	}
	return seal(secret, PayloadToken, []byte(token), name, harness, principalID, expires)
}

// SealIdentity stores a human identity that is already sealed under its passphrase
// (identityseal.Seal). Only a cli invite carries one.
func SealIdentity(secret []byte, sealedIdentity []byte, name, principalID string, expires time.Time) (Record, error) {
	return seal(secret, PayloadSealedIdentity, sealedIdentity, name, "cli", principalID, expires)
}

func checkPayload(kind string, payload []byte, harness string) error {
	switch kind {
	case PayloadToken:
		token := string(payload)
		if harness == "cli" || len(token) == 0 || len(token) > maxTokenBytes || !strings.HasPrefix(token, "cairn1.") || strings.ContainsAny(token, " \t\r\n") {
			return errors.New("invite token is not a Cairn credential")
		}
	case PayloadSealedIdentity:
		if harness != "cli" || identityseal.Check(payload) != nil {
			return errors.New("invite payload is not a sealed cli identity")
		}
	default:
		return errors.New("unknown invite payload")
	}
	return nil
}

func seal(secret []byte, kind string, payload []byte, name, harness, principalID string, expires time.Time) (Record, error) {
	if len(secret) != secretBytes {
		return Record{}, errors.New("invite secret must be 32 bytes")
	}
	if !ValidName(name) || !ValidHarness(harness) {
		return Record{}, errors.New("invalid invite name or harness")
	}
	if err := checkPayload(kind, payload, harness); err != nil {
		return Record{}, err
	}
	token := payload
	r := Record{Version: recordVersion, ID: ID(secret), Name: name, Harness: harness, PrincipalID: principalID, ExpiresAt: expires.UTC().Truncate(time.Second).Format(time.RFC3339), Payload: kind}
	aead, err := newAEAD(secret)
	if err != nil {
		return Record{}, err
	}
	nonce := make([]byte, aead.NonceSize())
	if _, err = rand.Read(nonce); err != nil {
		return Record{}, err
	}
	r.Nonce = base64.RawStdEncoding.EncodeToString(nonce)
	r.Sealed = base64.RawStdEncoding.EncodeToString(aead.Seal(nil, nonce, token, r.associated()))
	return r, nil
}

// ErrExpired reports an invite past its expiry.
var ErrExpired = errors.New("Garden invite has expired")

// Open checks the record against secret and now and returns the token.
func (r Record) Open(secret []byte, now time.Time) (string, error) {
	if r.Version != recordVersion || !idPattern.MatchString(r.ID) || r.ID != ID(secret) || !ValidName(r.Name) || !ValidHarness(r.Harness) {
		return "", errors.New("invalid Garden invite record")
	}
	expires, err := time.Parse(time.RFC3339, r.ExpiresAt)
	if err != nil {
		return "", errors.New("invalid Garden invite record")
	}
	if !now.Before(expires) {
		return "", ErrExpired
	}
	nonce, err1 := base64.RawStdEncoding.DecodeString(r.Nonce)
	sealed, err2 := base64.RawStdEncoding.DecodeString(r.Sealed)
	if err1 != nil || err2 != nil {
		return "", errors.New("invalid Garden invite record")
	}
	aead, err := newAEAD(secret)
	if err != nil {
		return "", err
	}
	if len(nonce) != aead.NonceSize() {
		return "", errors.New("invalid Garden invite record")
	}
	token, err := aead.Open(nil, nonce, sealed, r.associated())
	if err != nil {
		return "", errors.New("Garden invite does not match its record")
	}
	if err = checkPayload(r.Payload, token, r.Harness); err != nil {
		return "", err
	}
	return string(token), nil
}

func newAEAD(secret []byte) (cipher.AEAD, error) {
	k, err := key(secret)
	if err != nil {
		return nil, err
	}
	block, err := aes.NewCipher(k)
	if err != nil {
		return nil, err
	}
	return cipher.NewGCM(block)
}

// EnrolRequest is the body of POST /enrol.
type EnrolRequest struct {
	Secret string `json:"secret"`
}

// EnrolResponse is what a successful redemption returns. Token is the only secret field.
type EnrolResponse struct {
	Name    string `json:"name"`
	Harness string `json:"harness,omitempty"`
	Token   string `json:"token,omitempty"`
	// SealedIdentity replaces Token for a cli identity: the passphrase-sealed identity file,
	// written as identities/<name>.sealed unchanged.
	SealedIdentity string           `json:"sealed_identity,omitempty"`
	InstanceID     string           `json:"instance_id"`
	Scope          gardenauth.Scope `json:"scope"`
	Classification string           `json:"classification"`
}
