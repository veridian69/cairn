// Package accessjwt verifies Cloudflare Access application tokens with the
// standard library. Cloudflare signs them RS256 and publishes the keys at
// https://<team>.cloudflareaccess.com/cdn-cgi/access/certs.
package accessjwt

import (
	"crypto"
	"crypto/rsa"
	"crypto/sha256"
	"encoding/base64"
	"encoding/json"
	"errors"
	"math"
	"math/big"
	"slices"
	"strings"
	"time"
)

var (
	ErrMalformed    = errors.New("malformed")
	ErrAlgorithm    = errors.New("algorithm")
	ErrUnknownKey   = errors.New("unknown_key")
	ErrSignature    = errors.New("signature")
	ErrExpired      = errors.New("expired")
	ErrNotYetValid  = errors.New("not_yet_valid")
	ErrIssuer       = errors.New("issuer")
	ErrAudience     = errors.New("audience")
	ErrIdentity     = errors.New("identity")
	ErrServiceToken = errors.New("service_token")
)

// Audience accepts the JWT "aud" claim as a string or an array of strings.
type Audience []string

func (a *Audience) UnmarshalJSON(data []byte) error {
	var single string
	if err := json.Unmarshal(data, &single); err == nil {
		*a = Audience{single}
		return nil
	}
	var many []string
	if err := json.Unmarshal(data, &many); err != nil {
		return err
	}
	*a = Audience(many)
	return nil
}

// Claims are the subset of Access claims the edge decides on.
type Claims struct {
	Issuer     string   `json:"iss"`
	Audience   Audience `json:"aud"`
	Email      string   `json:"email"`
	CommonName string   `json:"common_name"`
	Subject    string   `json:"sub"`
	ExpiresAt  int64    `json:"exp"`
	NotBefore  int64    `json:"nbf"`
	IssuedAt   int64    `json:"iat"`
	Type       string   `json:"type"`
}

// Expectation is what a valid token must assert.
type Expectation struct {
	Issuer   string
	Audience string
	Email    string
}

type jwksDocument struct {
	Keys []struct {
		Kid string `json:"kid"`
		Kty string `json:"kty"`
		Alg string `json:"alg"`
		N   string `json:"n"`
		E   string `json:"e"`
	} `json:"keys"`
}

// ParseKeys reads a JWKS document and returns the usable RS256 keys by kid.
func ParseKeys(data []byte) (map[string]*rsa.PublicKey, error) {
	var document jwksDocument
	if err := json.Unmarshal(data, &document); err != nil {
		return nil, ErrMalformed
	}
	keys := make(map[string]*rsa.PublicKey, len(document.Keys))
	for _, key := range document.Keys {
		if key.Kty != "RSA" || key.Kid == "" || (key.Alg != "" && key.Alg != "RS256") {
			continue
		}
		modulus, err := base64.RawURLEncoding.DecodeString(key.N)
		if err != nil {
			continue
		}
		exponentBytes, err := base64.RawURLEncoding.DecodeString(key.E)
		if err != nil {
			continue
		}
		exponent := new(big.Int).SetBytes(exponentBytes)
		if !exponent.IsInt64() || exponent.Int64() < 3 || exponent.Int64() > math.MaxInt32 {
			continue
		}
		keys[key.Kid] = &rsa.PublicKey{N: new(big.Int).SetBytes(modulus), E: int(exponent.Int64())}
	}
	if len(keys) == 0 {
		return nil, ErrMalformed
	}
	return keys, nil
}

// Verify checks signature first, then time, then issuer, audience and
// identity, so a forged token never reaches a claim comparison.
func Verify(token string, keys map[string]*rsa.PublicKey, now time.Time, expect Expectation) (Claims, error) {
	parts := strings.Split(token, ".")
	if len(parts) != 3 {
		return Claims{}, ErrMalformed
	}
	headerBytes, err := base64.RawURLEncoding.DecodeString(parts[0])
	if err != nil {
		return Claims{}, ErrMalformed
	}
	var header struct {
		Alg string `json:"alg"`
		Kid string `json:"kid"`
	}
	if err := json.Unmarshal(headerBytes, &header); err != nil {
		return Claims{}, ErrMalformed
	}
	if header.Alg != "RS256" {
		return Claims{}, ErrAlgorithm
	}
	key, ok := keys[header.Kid]
	if !ok {
		return Claims{}, ErrUnknownKey
	}
	signature, err := base64.RawURLEncoding.DecodeString(parts[2])
	if err != nil {
		return Claims{}, ErrMalformed
	}
	digest := sha256.Sum256([]byte(parts[0] + "." + parts[1]))
	if err := rsa.VerifyPKCS1v15(key, crypto.SHA256, digest[:], signature); err != nil {
		return Claims{}, ErrSignature
	}
	payload, err := base64.RawURLEncoding.DecodeString(parts[1])
	if err != nil {
		return Claims{}, ErrMalformed
	}
	var claims Claims
	if err := json.Unmarshal(payload, &claims); err != nil {
		return Claims{}, ErrMalformed
	}
	unix := now.Unix()
	if claims.ExpiresAt == 0 || unix >= claims.ExpiresAt {
		return Claims{}, ErrExpired
	}
	if claims.NotBefore != 0 && unix < claims.NotBefore {
		return Claims{}, ErrNotYetValid
	}
	if claims.Issuer != expect.Issuer {
		return Claims{}, ErrIssuer
	}
	if !slices.Contains(claims.Audience, expect.Audience) {
		return Claims{}, ErrAudience
	}
	if claims.CommonName != "" || claims.Email == "" {
		return Claims{}, ErrServiceToken
	}
	if !strings.EqualFold(claims.Email, expect.Email) {
		return Claims{}, ErrIdentity
	}
	return claims, nil
}
