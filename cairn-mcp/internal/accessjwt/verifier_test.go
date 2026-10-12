package accessjwt

import (
	"context"
	"errors"
	"net/http"
	"net/http/httptest"
	"sync/atomic"
	"testing"
	"time"
)

type jwksServer struct {
	server *httptest.Server
	body   atomic.Pointer[[]byte]
	fails  atomic.Bool
	hits   atomic.Int64
}

func newJWKSServer(t *testing.T, initial []byte) *jwksServer {
	t.Helper()
	s := &jwksServer{}
	s.body.Store(&initial)
	s.server = httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		s.hits.Add(1)
		if s.fails.Load() {
			http.Error(w, "down", http.StatusServiceUnavailable)
			return
		}
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write(*s.body.Load())
	}))
	t.Cleanup(s.server.Close)
	return s
}

func TestVerifierFetchesThenVerifies(t *testing.T) {
	key := newKey(t)
	jwks := newJWKSServer(t, jwksFor(t, "k1", &key.PublicKey))
	v := NewVerifier(jwks.server.URL, jwks.server.Client(), VerifierOptions{Now: func() time.Time { return testNow }})
	if _, err := v.Verify(context.Background(), signToken(t, key, "k1", "RS256", userClaims()), testExpectation()); err != nil {
		t.Fatalf("first verify with no keys must fetch and succeed: %v", err)
	}
	if jwks.hits.Load() != 1 {
		t.Fatalf("expected exactly one fetch, got %d", jwks.hits.Load())
	}
	if _, err := v.Verify(context.Background(), signToken(t, key, "k1", "RS256", userClaims()), testExpectation()); err != nil {
		t.Fatalf("second verify: %v", err)
	}
	if jwks.hits.Load() != 1 {
		t.Fatalf("a fresh set must not refetch, got %d hits", jwks.hits.Load())
	}
}

func TestVerifierRecoversFromOutageWithThrottledRefetch(t *testing.T) {
	key := newKey(t)
	jwks := newJWKSServer(t, jwksFor(t, "k1", &key.PublicKey))
	jwks.fails.Store(true)
	now := testNow
	v := NewVerifier(jwks.server.URL, jwks.server.Client(), VerifierOptions{Now: func() time.Time { return now }, MinRefetchInterval: time.Minute})
	token := func() string { return signToken(t, key, "k1", "RS256", userClaims()) }
	if _, err := v.Verify(context.Background(), token(), testExpectation()); !errors.Is(err, ErrKeysUnavailable) {
		t.Fatalf("during outage: %v", err)
	}
	hits := jwks.hits.Load()
	jwks.fails.Store(false)
	if _, err := v.Verify(context.Background(), token(), testExpectation()); !errors.Is(err, ErrKeysUnavailable) {
		t.Fatalf("inside interval: %v", err)
	}
	if jwks.hits.Load() != hits {
		t.Fatal("no fetch allowed inside MinRefetchInterval")
	}
	now = testNow.Add(2 * time.Minute)
	if _, err := v.Verify(context.Background(), token(), testExpectation()); err != nil {
		t.Fatalf("after interval: %v", err)
	}
	if jwks.hits.Load() != hits+1 {
		t.Fatalf("expected exactly one more fetch, got %d", jwks.hits.Load()-hits)
	}
}

func TestVerifierRefetchesOnceForUnknownKid(t *testing.T) {
	oldKey := newKey(t)
	newKeyPair := newKey(t)
	jwks := newJWKSServer(t, jwksFor(t, "k1", &oldKey.PublicKey))
	v := NewVerifier(jwks.server.URL, jwks.server.Client(), VerifierOptions{Now: func() time.Time { return testNow }, MinRefetchInterval: time.Minute})
	if err := v.Fetch(context.Background()); err != nil {
		t.Fatalf("fetch: %v", err)
	}
	rotated := jwksFor(t, "k2", &newKeyPair.PublicKey)
	jwks.body.Store(&rotated)
	hitsBefore := jwks.hits.Load()
	if _, err := v.Verify(context.Background(), signToken(t, newKeyPair, "k2", "RS256", userClaims()), testExpectation()); err != nil {
		t.Fatalf("after rotation: %v", err)
	}
	if jwks.hits.Load() != hitsBefore+1 {
		t.Fatalf("expected exactly one refetch, got %d", jwks.hits.Load()-hitsBefore)
	}
	third := newKey(t)
	if _, err := v.Verify(context.Background(), signToken(t, third, "k3", "RS256", userClaims()), testExpectation()); !errors.Is(err, ErrUnknownKey) {
		t.Fatalf("second unknown kid: %v", err)
	}
	if jwks.hits.Load() != hitsBefore+1 {
		t.Fatal("a second unknown kid inside the interval must not fetch again")
	}
}

func TestVerifierKeepsLastGoodSetThenFailsClosed(t *testing.T) {
	key := newKey(t)
	jwks := newJWKSServer(t, jwksFor(t, "k1", &key.PublicKey))
	now := testNow
	v := NewVerifier(jwks.server.URL, jwks.server.Client(), VerifierOptions{Now: func() time.Time { return now }, MaxAge: time.Hour})
	if err := v.Fetch(context.Background()); err != nil {
		t.Fatalf("fetch: %v", err)
	}
	jwks.fails.Store(true)
	if err := v.Fetch(context.Background()); err == nil {
		t.Fatal("expected fetch failure")
	}
	if _, err := v.Verify(context.Background(), signToken(t, key, "k1", "RS256", userClaims()), testExpectation()); err != nil {
		t.Fatalf("last good set must still serve: %v", err)
	}
	now = testNow.Add(2 * time.Hour)
	claims := userClaims()
	claims["exp"] = now.Add(time.Hour).Unix()
	if _, err := v.Verify(context.Background(), signToken(t, key, "k1", "RS256", claims), testExpectation()); !errors.Is(err, ErrKeysUnavailable) {
		t.Fatalf("stale set must fail closed: %v", err)
	}
}

func TestVerifierFetchIsBounded(t *testing.T) {
	slow := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		<-r.Context().Done()
	}))
	t.Cleanup(slow.Close)
	v := NewVerifier(slow.URL, slow.Client(), VerifierOptions{FetchTimeout: 50 * time.Millisecond})
	start := time.Now()
	if err := v.Fetch(context.Background()); err == nil {
		t.Fatal("expected timeout")
	}
	if time.Since(start) > time.Second {
		t.Fatal("fetch did not honour its timeout")
	}
}
