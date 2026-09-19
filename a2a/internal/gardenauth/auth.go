// Package gardenauth delegates credential and grant checks to Cairn's exact-scope
// diagnostic endpoint. It neither reads Cairn storage nor retains credentials.
package gardenauth

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"io"
	"mime"
	"net"
	"net/http"
	"net/netip"
	"net/url"
	"regexp"
	"strconv"
	"strings"
	"time"
	"unicode/utf8"
)

// Segment is one ordered Cairn scope segment; identifiers are case-sensitive.
type Segment struct {
	Kind       string `json:"kind"`
	Identifier string `json:"identifier"`
}

// Scope is a fixed deployment boundary, never supplied by a remote caller.
type Scope struct {
	Realm    string    `json:"realm"`
	Segments []Segment `json:"segments"`
}

// Config pins a deployment to one Cairn instance, scope and classification.
type Config struct {
	// Endpoint is the complete /memory/v1/diagnose URL. HTTP is allowed only
	// with a numeric loopback host; HTTPS verifies certificates normally.
	Endpoint       string `json:"endpoint"`
	InstanceID     string `json:"instance_id"`
	Scope          Scope  `json:"scope"`
	Classification string `json:"classification"`
}

// Identity contains only server-authenticated identity and current permissions.
// CanWrite requires both retrieve and ingest: sending must not bypass read
// admission. These are snapshots, not permission to cache future decisions.
type Identity struct {
	PrincipalID string
	Kind        string
	CanRead     bool
	CanWrite    bool
}

var (
	ErrInvalidCredentials = errors.New("Cairn credential is invalid")
	ErrPermissionDenied   = errors.New("Cairn denied the diagnostic request")
	ErrUnavailable        = errors.New("Cairn authentication is unavailable")
	ErrInvalidResponse    = errors.New("Cairn returned an invalid diagnostic result")
)

const (
	maxResponseBytes = 16384
	requestTimeout   = 5 * time.Second
)

var (
	uuidPattern       = regexp.MustCompile(`^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$`)
	labelPattern      = regexp.MustCompile(`^[a-z](?:[a-z0-9-]{0,61}[a-z0-9])?$`)
	identifierPattern = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9._~:/@+%-]{0,254}$`)
	digestPattern     = regexp.MustCompile(`^[0-9a-f]{64}$`)
	versionPattern    = regexp.MustCompile(`^[0-9][a-zA-Z0-9.+-]{0,63}$`)
	timestampPattern  = regexp.MustCompile(`^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\.[0-9]{6}Z$`)
)

// Client is safe for concurrent use. It holds configuration and an HTTP pool,
// never a credential or an authenticated identity.
type Client struct {
	cfg  Config
	body []byte
	http *http.Client
}

// New validates and copies the deployment binding without making a request.
func New(cfg Config) (*Client, error) {
	u, err := url.Parse(cfg.Endpoint)
	if err != nil || u.Hostname() == "" || u.User != nil || u.RawQuery != "" || u.ForceQuery || u.Fragment != "" || u.RawFragment != "" || u.RawPath != "" || u.Path != "/memory/v1/diagnose" {
		return nil, errors.New("invalid Cairn diagnostic endpoint")
	}
	if u.Scheme != "https" {
		addr, err := netip.ParseAddr(u.Hostname())
		if u.Scheme != "http" || err != nil || !addr.IsLoopback() {
			return nil, errors.New("Cairn endpoint requires HTTPS or numeric loopback HTTP")
		}
	}
	if port := u.Port(); port != "" {
		n, err := strconv.Atoi(port)
		if err != nil || n < 1 || n > 65535 {
			return nil, errors.New("invalid Cairn endpoint port")
		}
	}
	if !uuidPattern.MatchString(cfg.InstanceID) {
		return nil, errors.New("Cairn instance must be a canonical version 4 UUID")
	}
	if cfg.Classification != "public" && cfg.Classification != "internal" && cfg.Classification != "restricted" {
		return nil, errors.New("invalid Cairn classification")
	}
	if !labelPattern.MatchString(cfg.Scope.Realm) || len(cfg.Scope.Segments) > 16 {
		return nil, errors.New("invalid Cairn scope")
	}
	for _, s := range cfg.Scope.Segments {
		if !labelPattern.MatchString(s.Kind) || !identifierPattern.MatchString(s.Identifier) {
			return nil, errors.New("invalid Cairn scope segment")
		}
	}
	// Allocate even for an empty root scope so JSON emits [] rather than null.
	cfg.Scope.Segments = append(make([]Segment, 0, len(cfg.Scope.Segments)), cfg.Scope.Segments...)
	body, err := json.Marshal(struct {
		Scope          Scope  `json:"scope"`
		Classification string `json:"classification"`
	}{cfg.Scope, cfg.Classification})
	if err != nil {
		return nil, errors.New("invalid Cairn configuration")
	}
	transport := &http.Transport{
		DisableCompression:     true,
		DialContext:            (&net.Dialer{Timeout: requestTimeout, KeepAlive: 30 * time.Second}).DialContext,
		ForceAttemptHTTP2:      true,
		MaxIdleConns:           20,
		MaxIdleConnsPerHost:    10,
		IdleConnTimeout:        30 * time.Second,
		TLSHandshakeTimeout:    requestTimeout,
		ResponseHeaderTimeout:  requestTimeout,
		MaxResponseHeaderBytes: maxResponseBytes,
	}
	return &Client{cfg: cfg, body: body, http: &http.Client{
		Transport:     transport,
		Timeout:       requestTimeout,
		CheckRedirect: func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse },
	}}, nil
}

// Authenticate obtains a fresh diagnostic snapshot. No result or credential is
// cached. Errors deliberately exclude upstream bodies, URLs and transport prose.
func (c *Client) Authenticate(ctx context.Context, token string) (Identity, error) {
	if len(token) == 0 || len(token) > 4096 {
		return Identity{}, ErrInvalidCredentials
	}
	for _, b := range []byte(token) {
		if b <= ' ' || b >= 127 {
			return Identity{}, ErrInvalidCredentials
		}
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, c.cfg.Endpoint, bytes.NewReader(c.body))
	if err != nil {
		return Identity{}, ErrUnavailable
	}
	req.Header.Set("Authorization", "Bearer "+token)
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Accept", "application/json")
	req.Header.Set("Accept-Encoding", "identity")
	resp, err := c.http.Do(req)
	if err != nil {
		return Identity{}, ErrUnavailable
	}
	defer resp.Body.Close()
	switch {
	case resp.StatusCode == http.StatusUnauthorized:
		return Identity{}, ErrInvalidCredentials
	case resp.StatusCode == http.StatusForbidden:
		return Identity{}, ErrPermissionDenied
	case resp.StatusCode == http.StatusTooManyRequests || resp.StatusCode >= 500:
		return Identity{}, ErrUnavailable
	case resp.StatusCode != http.StatusOK:
		return Identity{}, ErrInvalidResponse
	}
	// Never let transparent decompression move the size boundary off the wire
	// or consume unbounded encoded headers before LimitReader sees a byte.
	encodings := resp.Header.Values("Content-Encoding")
	if len(encodings) > 1 || (len(encodings) == 1 && !strings.EqualFold(strings.TrimSpace(encodings[0]), "identity")) {
		return Identity{}, ErrInvalidResponse
	}
	mediaType, _, err := mime.ParseMediaType(resp.Header.Get("Content-Type"))
	if err != nil || mediaType != "application/json" {
		return Identity{}, ErrInvalidResponse
	}
	body, err := io.ReadAll(io.LimitReader(resp.Body, maxResponseBytes+1))
	if err != nil {
		return Identity{}, ErrUnavailable
	}
	if len(body) > maxResponseBytes || !utf8.Valid(body) {
		return Identity{}, ErrInvalidResponse
	}
	value, err := uniqueJSON(body)
	if err != nil {
		return Identity{}, ErrInvalidResponse
	}
	return c.identity(value)
}

func (c *Client) identity(value any) (Identity, error) {
	obj, ok := object(value, "instance_id", "product_version", "contract_identity", "contract_digest", "mcp_contract_digest", "principal_id", "principal_kind", "scope", "classification", "permissions", "evaluated_at", "permission_basis")
	if !ok {
		return Identity{}, ErrInvalidResponse
	}
	if obj["instance_id"] != c.cfg.InstanceID || obj["classification"] != c.cfg.Classification || obj["contract_identity"] != "cairn.memory/v1" || obj["permission_basis"] != "current_grants_only" {
		return Identity{}, ErrInvalidResponse
	}
	principal, ok := obj["principal_id"].(string)
	if !ok || !uuidPattern.MatchString(principal) {
		return Identity{}, ErrInvalidResponse
	}
	kind, ok := obj["principal_kind"].(string)
	if !ok || (kind != "human" && kind != "workload") {
		return Identity{}, ErrInvalidResponse
	}
	for key, pattern := range map[string]*regexp.Regexp{"product_version": versionPattern, "contract_digest": digestPattern, "mcp_contract_digest": digestPattern, "evaluated_at": timestampPattern} {
		text, ok := obj[key].(string)
		if !ok || !pattern.MatchString(text) {
			return Identity{}, ErrInvalidResponse
		}
	}
	if _, err := time.Parse(time.RFC3339Nano, obj["evaluated_at"].(string)); err != nil {
		return Identity{}, ErrInvalidResponse
	}
	scope, ok := object(obj["scope"], "realm", "segments")
	if !ok || scope["realm"] != c.cfg.Scope.Realm {
		return Identity{}, ErrInvalidResponse
	}
	segments, ok := scope["segments"].([]any)
	if !ok || len(segments) != len(c.cfg.Scope.Segments) {
		return Identity{}, ErrInvalidResponse
	}
	for i, raw := range segments {
		segment, ok := object(raw, "kind", "identifier")
		if !ok || segment["kind"] != c.cfg.Scope.Segments[i].Kind || segment["identifier"] != c.cfg.Scope.Segments[i].Identifier {
			return Identity{}, ErrInvalidResponse
		}
	}
	permissions, ok := object(obj["permissions"], "retrieve", "ingest", "promote", "invalidate")
	if !ok {
		return Identity{}, ErrInvalidResponse
	}
	for _, value := range permissions {
		if _, ok := value.(bool); !ok {
			return Identity{}, ErrInvalidResponse
		}
	}
	read := permissions["retrieve"].(bool)
	return Identity{PrincipalID: principal, Kind: kind, CanRead: read, CanWrite: read && permissions["ingest"].(bool)}, nil
}

func object(value any, keys ...string) (map[string]any, bool) {
	obj, ok := value.(map[string]any)
	if !ok || len(obj) != len(keys) {
		return nil, false
	}
	for _, key := range keys {
		if _, ok := obj[key]; !ok {
			return nil, false
		}
	}
	return obj, true
}

// A normal encoding/json unmarshal accepts duplicate keys and case aliases.
// Decode exact keys ourselves before validating the closed diagnostic schema.
func uniqueJSON(body []byte) (any, error) {
	dec := json.NewDecoder(bytes.NewReader(body))
	dec.UseNumber()
	value, err := jsonValue(dec, 0)
	if err != nil {
		return nil, err
	}
	if _, err := dec.Token(); err != io.EOF {
		return nil, ErrInvalidResponse
	}
	return value, nil
}

func jsonValue(dec *json.Decoder, depth int) (any, error) {
	if depth > 16 {
		return nil, ErrInvalidResponse
	}
	token, err := dec.Token()
	if err != nil {
		return nil, err
	}
	switch token {
	case json.Delim('{'):
		obj := make(map[string]any)
		for dec.More() {
			key, err := dec.Token()
			if err != nil {
				return nil, err
			}
			name, ok := key.(string)
			if !ok {
				return nil, ErrInvalidResponse
			}
			if _, exists := obj[name]; exists {
				return nil, ErrInvalidResponse
			}
			value, err := jsonValue(dec, depth+1)
			if err != nil {
				return nil, err
			}
			obj[name] = value
		}
		end, err := dec.Token()
		if err != nil || end != json.Delim('}') {
			return nil, ErrInvalidResponse
		}
		return obj, nil
	case json.Delim('['):
		arr := make([]any, 0)
		for dec.More() {
			value, err := jsonValue(dec, depth+1)
			if err != nil {
				return nil, err
			}
			arr = append(arr, value)
		}
		end, err := dec.Token()
		if err != nil || end != json.Delim(']') {
			return nil, ErrInvalidResponse
		}
		return arr, nil
	default:
		if _, ok := token.(json.Delim); ok {
			return nil, ErrInvalidResponse
		}
		return token, nil
	}
}
