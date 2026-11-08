package garden

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"slices"
	"time"
)

// Reload applies cfg to the running gateway. Only principals may change: every other field
// must equal the configuration New was given, or the reload is refused and nothing changes.
// New principals are enrolled as at startup, one transaction, then start authenticating. A
// rename is refused. Removed principals stop authenticating at once; their inbox rows remain.
func (s *Server) Reload(ctx context.Context, cfg Config) error {
	if err := cfg.validate(); err != nil {
		return err
	}
	fields, err := configFields(cfg)
	if err != nil {
		return err
	}
	// Both sides' keys: omitempty drops an emptied field from the new JSON, and removing
	// invites_dir, public_endpoint or the TLS paths must be refused like any other change.
	seen := map[string]bool{}
	keys := make([]string, 0, len(fields)+len(s.loaded))
	for _, m := range []map[string]json.RawMessage{fields, s.loaded} {
		for key := range m {
			if !seen[key] {
				seen[key] = true
				keys = append(keys, key)
			}
		}
	}
	slices.Sort(keys)
	for _, key := range keys {
		a, inNew := fields[key]
		b, inOld := s.loaded[key]
		if inNew != inOld || !bytes.Equal(a, b) {
			return fmt.Errorf("Garden configuration field %q differs from the running configuration; only principals can be reloaded", key)
		}
	}
	principals := make(map[string]string, len(cfg.Principals))
	for k, v := range cfg.Principals {
		principals[k] = v
	}

	s.mu.Lock()
	defer s.mu.Unlock()
	if s.closed {
		return failure("unavailable", "Garden service is closed")
	}
	if err = s.ensure(ctx); err != nil {
		return err
	}
	probe, cancel := context.WithTimeout(ctx, 2*time.Second)
	p, err := s.stream.Position(probe)
	cancel()
	if err != nil {
		return failure("unavailable", "Garden stream unavailable")
	}
	tx, err := s.sql.BeginTx(ctx, nil)
	if err != nil {
		return err
	}
	defer tx.Rollback()
	if err = s.enrolPrincipals(ctx, tx, principals, p.Created.UTC().Format(time.RFC3339Nano), p.Last); err != nil {
		return err
	}
	if err = tx.Commit(); err != nil {
		return err
	}
	// Publish only after the inboxes are committed, so no principal authenticates without one.
	s.principalsMu.Lock()
	s.principals = principals
	s.principalsMu.Unlock()
	return nil
}

// principalMap returns the current principal-to-name map. Callers must not mutate it.
func (s *Server) principalMap() map[string]string {
	s.principalsMu.RLock()
	defer s.principalsMu.RUnlock()
	return s.principals
}

// configFields returns the configuration as top-level JSON fields, without principals or the
// build version, which are not configuration in the sense Reload enforces. It is taken before
// New applies defaults, so a reload file that LoadConfig reads the same way compares equal.
func configFields(cfg Config) (map[string]json.RawMessage, error) {
	cfg.Principals = nil
	cfg.Version = ""
	raw, err := json.Marshal(cfg)
	if err != nil {
		return nil, err
	}
	var fields map[string]json.RawMessage
	if err = json.Unmarshal(raw, &fields); err != nil {
		return nil, err
	}
	delete(fields, "principals")
	return fields, nil
}
