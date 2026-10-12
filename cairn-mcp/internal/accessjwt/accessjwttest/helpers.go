// Package accessjwttest mints Cloudflare-Access-shaped tokens for tests.
package accessjwttest

import (
	"crypto"
	"crypto/rand"
	"crypto/rsa"
	"crypto/sha256"
	"encoding/base64"
	"encoding/json"
	"math/big"
	"testing"
)

func Key(t testing.TB) *rsa.PrivateKey {
	t.Helper()
	key, err := rsa.GenerateKey(rand.Reader, 2048)
	if err != nil {
		t.Fatalf("generate key: %v", err)
	}
	return key
}

func JWKS(t testing.TB, kid string, key *rsa.PublicKey) []byte {
	t.Helper()
	data, err := json.Marshal(map[string]any{"keys": []map[string]string{{
		"kid": kid, "kty": "RSA", "alg": "RS256", "use": "sig",
		"n": base64.RawURLEncoding.EncodeToString(key.N.Bytes()),
		"e": base64.RawURLEncoding.EncodeToString(big.NewInt(int64(key.E)).Bytes()),
	}}})
	if err != nil {
		t.Fatalf("marshal jwks: %v", err)
	}
	return data
}

// TokenWithAlg signs RS256 whatever the header says, so a wrong-alg token
// still carries a valid signature and the alg check is what refuses it.
func TokenWithAlg(t testing.TB, key *rsa.PrivateKey, kid, alg string, claims map[string]any) string {
	t.Helper()
	header, _ := json.Marshal(map[string]string{"alg": alg, "kid": kid, "typ": "JWT"})
	payload, _ := json.Marshal(claims)
	signingInput := base64.RawURLEncoding.EncodeToString(header) + "." + base64.RawURLEncoding.EncodeToString(payload)
	digest := sha256.Sum256([]byte(signingInput))
	signature, err := rsa.SignPKCS1v15(rand.Reader, key, crypto.SHA256, digest[:])
	if err != nil {
		t.Fatalf("sign: %v", err)
	}
	return signingInput + "." + base64.RawURLEncoding.EncodeToString(signature)
}

func Token(t testing.TB, key *rsa.PrivateKey, kid string, claims map[string]any) string {
	return TokenWithAlg(t, key, kid, "RS256", claims)
}
