package accessjwt

import (
	"context"
	"crypto/rsa"
	"errors"
	"fmt"
	"io"
	"net/http"
	"sync"
	"time"
)

var ErrKeysUnavailable = errors.New("keys_unavailable")

const maxJWKSBytes = 64 * 1024

type VerifierOptions struct {
	FetchTimeout       time.Duration
	RefreshEvery       time.Duration
	MaxAge             time.Duration
	MinRefetchInterval time.Duration
	Now                func() time.Time
}

func (o VerifierOptions) withDefaults() VerifierOptions {
	if o.FetchTimeout <= 0 {
		o.FetchTimeout = 5 * time.Second
	}
	if o.RefreshEvery <= 0 {
		o.RefreshEvery = time.Hour
	}
	if o.MaxAge <= 0 {
		o.MaxAge = 24 * time.Hour
	}
	if o.MinRefetchInterval <= 0 {
		o.MinRefetchInterval = time.Minute
	}
	if o.Now == nil {
		o.Now = time.Now
	}
	return o
}

type Verifier struct {
	certsURL string
	client   *http.Client
	options  VerifierOptions

	mu          sync.RWMutex
	keys        map[string]*rsa.PublicKey
	fetchedAt   time.Time
	lastRefetch time.Time
}

func NewVerifier(certsURL string, client *http.Client, options VerifierOptions) *Verifier {
	if client == nil {
		client = http.DefaultClient
	}
	return &Verifier{certsURL: certsURL, client: client, options: options.withDefaults()}
}

// Fetch replaces the key set with the published one. On any failure the
// previous set is kept untouched; freshness is judged at Verify time.
func (v *Verifier) Fetch(ctx context.Context) error {
	ctx, cancel := context.WithTimeout(ctx, v.options.FetchTimeout)
	defer cancel()
	request, err := http.NewRequestWithContext(ctx, http.MethodGet, v.certsURL, nil)
	if err != nil {
		return fmt.Errorf("jwks request: %w", err)
	}
	response, err := v.client.Do(request)
	if err != nil {
		return fmt.Errorf("jwks fetch: %w", err)
	}
	defer response.Body.Close()
	if response.StatusCode != http.StatusOK {
		return fmt.Errorf("jwks status %d", response.StatusCode)
	}
	body, err := io.ReadAll(io.LimitReader(response.Body, maxJWKSBytes+1))
	if err != nil {
		return fmt.Errorf("jwks read: %w", err)
	}
	if len(body) > maxJWKSBytes {
		return errors.New("jwks too large")
	}
	keys, err := ParseKeys(body)
	if err != nil {
		return fmt.Errorf("jwks parse: %w", err)
	}
	v.mu.Lock()
	v.keys = keys
	v.fetchedAt = v.options.Now()
	v.mu.Unlock()
	return nil
}

// Run refreshes on the configured interval until ctx ends.
func (v *Verifier) Run(ctx context.Context) {
	ticker := time.NewTicker(v.options.RefreshEvery)
	defer ticker.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
			_ = v.Fetch(ctx)
		}
	}
}

func (v *Verifier) snapshot() (map[string]*rsa.PublicKey, bool) {
	v.mu.RLock()
	defer v.mu.RUnlock()
	if v.keys == nil {
		return nil, false
	}
	if v.options.Now().Sub(v.fetchedAt) > v.options.MaxAge {
		return nil, false
	}
	return v.keys, true
}

func (v *Verifier) mayRefetch() bool {
	v.mu.Lock()
	defer v.mu.Unlock()
	now := v.options.Now()
	if !v.lastRefetch.IsZero() && now.Sub(v.lastRefetch) < v.options.MinRefetchInterval {
		return false
	}
	v.lastRefetch = now
	return true
}

// Verify fails closed on a missing or stale key set, and refetches at most
// once per interval when a token names a kid it does not hold.
func (v *Verifier) Verify(ctx context.Context, token string, expect Expectation) (Claims, error) {
	keys, fresh := v.snapshot()
	if !fresh {
		if !v.mayRefetch() {
			return Claims{}, ErrKeysUnavailable
		}
		if fetchErr := v.Fetch(ctx); fetchErr != nil {
			return Claims{}, ErrKeysUnavailable
		}
		keys, fresh = v.snapshot()
		if !fresh {
			return Claims{}, ErrKeysUnavailable
		}
	}
	claims, err := Verify(token, keys, v.options.Now(), expect)
	if !errors.Is(err, ErrUnknownKey) || !v.mayRefetch() {
		return claims, err
	}
	if fetchErr := v.Fetch(ctx); fetchErr != nil {
		return Claims{}, err
	}
	keys, fresh = v.snapshot()
	if !fresh {
		return Claims{}, ErrKeysUnavailable
	}
	return Verify(token, keys, v.options.Now(), expect)
}
