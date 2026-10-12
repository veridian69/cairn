package edge

import (
	"sync"

	"github.com/veridian69/cairn/cairn-mcp/internal/credentials"
)

// RouteTokens holds one Cairn bearer per inbound path, reloadable on SIGHUP.
type RouteTokens struct {
	routes      map[string]string
	expectedUID int
	mu          sync.RWMutex
	tokens      map[string][]byte
}

func NewRouteTokens(routes map[string]string, expectedUID int) (*RouteTokens, error) {
	r := &RouteTokens{routes: routes, expectedUID: expectedUID}
	if err := r.Reload(); err != nil {
		return nil, err
	}
	return r, nil
}

func (r *RouteTokens) Reload() error {
	loaded := make(map[string][]byte, len(r.routes))
	for inbound, credentialPath := range r.routes {
		token, err := credentials.ReadSecretFile(credentialPath, "route credential", r.expectedUID, credentials.MaxSecretBytes)
		if err != nil {
			return err
		}
		loaded[inbound] = token
	}
	r.mu.Lock()
	r.tokens = loaded
	r.mu.Unlock()
	return nil
}

func (r *RouteTokens) Lookup(inbound string) ([]byte, bool) {
	r.mu.RLock()
	defer r.mu.RUnlock()
	token, ok := r.tokens[inbound]
	if !ok {
		return nil, false
	}
	return append([]byte(nil), token...), true
}
