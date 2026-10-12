package accessjwt

import (
	"crypto/rsa"
	"errors"
	"testing"
	"time"

	"github.com/veridian69/cairn/cairn-mcp/internal/accessjwt/accessjwttest"
)

var testNow = time.Date(2026, 10, 8, 12, 0, 0, 0, time.UTC)

const (
	testIssuer   = "https://example.cloudflareaccess.com"
	testAudience = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
	testEmail    = "jon@jclk.ch"
)

func testExpectation() Expectation {
	return Expectation{Issuer: testIssuer, Audience: testAudience, Email: testEmail}
}

func newKey(t *testing.T) *rsa.PrivateKey { return accessjwttest.Key(t) }

func jwksFor(t *testing.T, kid string, key *rsa.PublicKey) []byte {
	return accessjwttest.JWKS(t, kid, key)
}

func signToken(t *testing.T, key *rsa.PrivateKey, kid string, alg string, claims map[string]any) string {
	return accessjwttest.TokenWithAlg(t, key, kid, alg, claims)
}

func userClaims() map[string]any {
	return map[string]any{
		"iss": testIssuer, "aud": []string{testAudience}, "email": testEmail, "sub": "user-1",
		"iat": testNow.Add(-time.Minute).Unix(), "exp": testNow.Add(time.Hour).Unix(), "type": "app",
	}
}

func TestVerifyAcceptsUserToken(t *testing.T) {
	key := newKey(t)
	keys, err := ParseKeys(jwksFor(t, "k1", &key.PublicKey))
	if err != nil {
		t.Fatalf("parse keys: %v", err)
	}
	claims, err := Verify(signToken(t, key, "k1", "RS256", userClaims()), keys, testNow, testExpectation())
	if err != nil {
		t.Fatalf("verify: %v", err)
	}
	if claims.Email != testEmail || claims.Subject != "user-1" {
		t.Fatalf("claims = %+v", claims)
	}
}

func TestVerifyAcceptsBareStringAudience(t *testing.T) {
	key := newKey(t)
	keys, _ := ParseKeys(jwksFor(t, "k1", &key.PublicKey))
	claims := userClaims()
	claims["aud"] = testAudience
	if _, err := Verify(signToken(t, key, "k1", "RS256", claims), keys, testNow, testExpectation()); err != nil {
		t.Fatalf("bare audience: %v", err)
	}
}

func TestVerifyRefusals(t *testing.T) {
	key := newKey(t)
	other := newKey(t)
	keys, _ := ParseKeys(jwksFor(t, "k1", &key.PublicKey))
	cases := []struct {
		name  string
		token string
		want  error
	}{
		{"forged", signToken(t, other, "k1", "RS256", userClaims()), ErrSignature},
		{"unknown kid", signToken(t, key, "k2", "RS256", userClaims()), ErrUnknownKey},
		{"wrong alg", signToken(t, key, "k1", "HS256", userClaims()), ErrAlgorithm},
		{"malformed", "not.a.jwt.at.all", ErrMalformed},
		{"expired", signToken(t, key, "k1", "RS256", with(userClaims(), "exp", testNow.Add(-time.Second).Unix())), ErrExpired},
		{"not yet valid", signToken(t, key, "k1", "RS256", with(userClaims(), "nbf", testNow.Add(time.Minute).Unix())), ErrNotYetValid},
		{"wrong issuer", signToken(t, key, "k1", "RS256", with(userClaims(), "iss", "https://other.cloudflareaccess.com")), ErrIssuer},
		{"wrong audience", signToken(t, key, "k1", "RS256", with(userClaims(), "aud", []string{"ffff"})), ErrAudience},
		{"wrong identity", signToken(t, key, "k1", "RS256", with(userClaims(), "email", "someone@example.com")), ErrIdentity},
		{"service token", signToken(t, key, "k1", "RS256", with(with(userClaims(), "email", ""), "common_name", "cairn-cli-clients.access")), ErrServiceToken},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			_, err := Verify(tc.token, keys, testNow, testExpectation())
			if !errors.Is(err, tc.want) {
				t.Fatalf("err = %v, want %v", err, tc.want)
			}
		})
	}
}

func with(claims map[string]any, key string, value any) map[string]any {
	claims[key] = value
	return claims
}

func TestParseKeysRejectsEmptyOrNonRSA(t *testing.T) {
	if _, err := ParseKeys([]byte(`{"keys":[]}`)); !errors.Is(err, ErrMalformed) {
		t.Fatalf("empty: %v", err)
	}
	if _, err := ParseKeys([]byte(`{"keys":[{"kid":"x","kty":"EC"}]}`)); !errors.Is(err, ErrMalformed) {
		t.Fatalf("non-RSA: %v", err)
	}
	if _, err := ParseKeys([]byte(`{`)); !errors.Is(err, ErrMalformed) {
		t.Fatalf("invalid json: %v", err)
	}
}
