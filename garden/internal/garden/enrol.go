package garden

import (
	"bytes"
	"encoding/base64"
	"encoding/json"
	"errors"
	"io"
	"io/fs"
	"mime"
	"net/http"
	"os"
	"path/filepath"
	"reflect"
	"sync"
	"syscall"
	"time"

	"github.com/veridian69/cairn/garden/internal/invite"
)

// Enrolment limits. The bucket is global, not per address: several machines share one tailnet
// address, so an address limit would throttle them together, and a client could evade it by
// changing address.
const (
	enrolBodyBytes       = 4096
	enrolBurst           = 5
	enrolRefillPerMinute = 10
	// maxInviteBytes matches invite's own bound on a stored record.
	maxInviteBytes = 64 * 1024
)

var errInvalidInvite = errors.New("invalid Garden invite record")

// enrolBucket is a global token bucket that spends one token per redemption attempt.
type enrolBucket struct {
	mu     sync.Mutex
	tokens float64
	at     time.Time
}

// allow spends a token at now, refilling at enrolRefillPerMinute up to enrolBurst. A clock that
// moves backwards refills nothing.
func (b *enrolBucket) allow(now time.Time) bool {
	b.mu.Lock()
	defer b.mu.Unlock()
	if b.at.IsZero() {
		b.tokens, b.at = enrolBurst, now
	}
	if elapsed := now.Sub(b.at); elapsed > 0 {
		b.tokens = min(float64(enrolBurst), b.tokens+elapsed.Minutes()*enrolRefillPerMinute)
		b.at = now
	}
	if b.tokens < 1 {
		return false
	}
	b.tokens--
	return true
}

// enrol redeems an invite for the identity it names. It is reached only when invites_dir is set.
// Responses never carry a secret or token in an error, and nothing here is logged.
func (s *Server) enrol(w http.ResponseWriter, r *http.Request) {
	if s.cfg.PublicEndpoint != "" && !matchesPublicAuthority(s.cfg.PublicEndpoint, r.Host) {
		enrolError(w, http.StatusForbidden, "forbidden", "invalid Host header")
		return
	}
	if len(r.Header.Values("Origin")) != 0 {
		enrolError(w, http.StatusForbidden, "forbidden", "origin forbidden")
		return
	}
	if r.Method != http.MethodPost {
		w.Header().Set("Allow", http.MethodPost)
		enrolError(w, http.StatusMethodNotAllowed, "method_not_allowed", "use POST")
		return
	}
	if !isJSONContentType(r.Header.Values("Content-Type")) {
		enrolError(w, http.StatusBadRequest, "invalid_request", "Content-Type must be application/json")
		return
	}
	data, err := io.ReadAll(io.LimitReader(r.Body, enrolBodyBytes+1))
	if err != nil || len(data) > enrolBodyBytes {
		enrolError(w, http.StatusBadRequest, "invalid_request", "request body must be at most 4 KiB")
		return
	}
	secret, ok := decodeEnrolSecret(data)
	if !ok {
		enrolError(w, http.StatusBadRequest, "invalid_request", "secret must be 32 bytes, base64url without padding")
		return
	}
	// Only well-formed attempts spend tokens, and the bucket is checked before any filesystem work.
	if !s.enrolBucket.allow(s.now()) {
		w.Header().Set("Retry-After", "6")
		enrolError(w, http.StatusTooManyRequests, "rate_limited", "too many enrolment attempts; try again shortly")
		return
	}
	if invite.CheckDir(s.cfg.InvitesDir) != nil {
		enrolError(w, http.StatusServiceUnavailable, "unavailable", "Garden invites are unavailable")
		return
	}
	// Read without claiming: a join that arrives before the operator's reload must stay redeemable.
	record, err := readInvite(s.cfg.InvitesDir, invite.ID(secret))
	if errors.Is(err, fs.ErrNotExist) || errors.Is(err, errInvalidInvite) {
		enrolError(w, http.StatusNotFound, "not_found", "no such unredeemed Garden invite")
		return
	}
	if err != nil {
		enrolError(w, http.StatusServiceUnavailable, "unavailable", "Garden invites are unavailable")
		return
	}
	if s.principalMap()[record.PrincipalID] != record.Name {
		enrolError(w, http.StatusConflict, "not_ready", "this gateway does not admit the invite's identity yet; retry after the operator reloads it")
		return
	}
	// Claiming is single-use. The record is gone after this, whatever Open decides.
	claimed, err := invite.Claim(s.cfg.InvitesDir, secret)
	if errors.Is(err, invite.ErrNotFound) {
		enrolError(w, http.StatusNotFound, "not_found", "no such unredeemed Garden invite")
		return
	}
	if err != nil {
		enrolError(w, http.StatusServiceUnavailable, "unavailable", "Garden invites are unavailable")
		return
	}
	token, err := claimed.Open(secret, s.now())
	if errors.Is(err, invite.ErrExpired) {
		enrolError(w, http.StatusGone, "expired", "Garden invite has expired")
		return
	}
	if err != nil {
		enrolError(w, http.StatusNotFound, "not_found", "no such unredeemed Garden invite")
		return
	}
	response := invite.EnrolResponse{
		Name:           claimed.Name,
		Harness:        claimed.Harness,
		InstanceID:     s.binding.InstanceID,
		Scope:          s.binding.Scope,
		Classification: s.binding.Classification,
	}
	if claimed.Payload == invite.PayloadSealedIdentity {
		response.SealedIdentity = token
	} else {
		response.Token = token
	}
	writeEnrolJSON(w, http.StatusOK, response)
}

// decodeEnrolSecret applies invite.EnrolRequest's strict decode: no case aliases or duplicate
// keys, no unknown fields, no trailing data, then the canonical 32-byte secret.
func decodeEnrolSecret(data []byte) ([]byte, bool) {
	if checkJSON(data, reflect.TypeOf(invite.EnrolRequest{})) != nil {
		return nil, false
	}
	var req invite.EnrolRequest
	dec := json.NewDecoder(bytes.NewReader(data))
	dec.DisallowUnknownFields()
	if dec.Decode(&req) != nil || dec.Decode(&struct{}{}) != io.EOF {
		return nil, false
	}
	secret, err := base64.RawURLEncoding.Strict().DecodeString(req.Secret)
	if err != nil || len(secret) != 32 {
		return nil, false
	}
	return secret, true
}

func isJSONContentType(values []string) bool {
	if len(values) != 1 {
		return false
	}
	mediaType, _, err := mime.ParseMediaType(values[0])
	return err == nil && mediaType == "application/json"
}

// readInvite reads the record for id without claiming it. It mirrors invite's claim-time checks:
// no symlink, a regular file, a size bound and a strict decode. A missing record is fs.ErrNotExist;
// a record that cannot be used is errInvalidInvite.
func readInvite(dir, id string) (invite.Record, error) {
	f, err := os.OpenFile(filepath.Join(dir, id+".json"), os.O_RDONLY|syscall.O_NOFOLLOW, 0)
	if errors.Is(err, syscall.ELOOP) {
		return invite.Record{}, errInvalidInvite
	}
	if err != nil {
		return invite.Record{}, err
	}
	defer f.Close()
	info, err := f.Stat()
	if err != nil || !info.Mode().IsRegular() {
		return invite.Record{}, errInvalidInvite
	}
	data, err := io.ReadAll(io.LimitReader(f, maxInviteBytes+1))
	if err != nil || len(data) > maxInviteBytes {
		return invite.Record{}, errInvalidInvite
	}
	var r invite.Record
	dec := json.NewDecoder(bytes.NewReader(data))
	dec.DisallowUnknownFields()
	if dec.Decode(&r) != nil || r.ID != id {
		return invite.Record{}, errInvalidInvite
	}
	return r, nil
}

// writeEnrolJSON sends a JSON body that must never be cached or stored by an intermediary.
func writeEnrolJSON(w http.ResponseWriter, status int, body any) {
	w.Header().Set("Cache-Control", "no-store")
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(body)
}

func enrolError(w http.ResponseWriter, status int, code, message string) {
	writeEnrolJSON(w, status, &Error{Code: code, Message: message})
}
