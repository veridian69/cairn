package invite

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/netip"
	"net/url"
	"os"
	"regexp"
	"strings"
	"syscall"
	"time"

	"github.com/google/uuid"
	"github.com/veridian69/cairn/garden/internal/gardenauth"
)

// The Cairn admin API issues the principal, grant and credential for an invite, through its
// /v1/create-principal, /v1/create-grant and /v1/issue-credential routes. Responses can carry a
// plaintext credential, so no error from this file quotes a response body.

const (
	adminTimeout      = 60 * time.Second
	maxAdminResponse  = 1 << 20
	maxAdminTokenFile = 16 * 1024
)

var (
	labelPattern      = regexp.MustCompile(`^[a-z](?:[a-z0-9-]{0,61}[a-z0-9])?$`)
	adminTokenPattern = regexp.MustCompile(`^cairn1\.[A-Za-z0-9_.-]+$`)
)

// CheckName returns the Cairn label, <name>-garden, that the admin API gives the principal.
func CheckName(name string) (string, error) {
	if !ValidName(name) {
		return "", fmt.Errorf("invalid name %q: use letters, digits, dots, underscores and hyphens, starting with a letter or digit", name)
	}
	label := name + "-garden"
	if !labelPattern.MatchString(label) {
		return "", fmt.Errorf("name %q does not make a Cairn label: use lowercase letters, digits and hyphens, starting with a letter", name)
	}
	return label, nil
}

// CairnBaseURL returns the origin (scheme and host) of the Cairn admin API. An explicit override
// wins; otherwise the origin of the diagnostic endpoint is used. Plain HTTP is allowed only to a
// numeric loopback address.
func CairnBaseURL(override, diagnoseEndpoint string) (string, error) {
	if override == "" {
		u, err := url.Parse(diagnoseEndpoint)
		if err != nil || u.Host == "" {
			return "", errors.New("the Cairn diagnostic endpoint has no host")
		}
		override = u.Scheme + "://" + u.Host
	}
	return validCairnOrigin(override)
}

func validCairnOrigin(raw string) (string, error) {
	u, err := url.Parse(raw)
	if err != nil || u.Host == "" || u.User != nil || u.RawQuery != "" || u.Fragment != "" || (u.Path != "" && u.Path != "/") {
		return "", errors.New("the Cairn admin URL must be scheme://host[:port]")
	}
	if u.Scheme != "https" {
		addr, err := netip.ParseAddr(u.Hostname())
		if u.Scheme != "http" || err != nil || !addr.IsLoopback() {
			return "", errors.New("the Cairn admin URL requires HTTPS, or HTTP to a numeric loopback address")
		}
	}
	return u.Scheme + "://" + u.Host, nil
}

// LoadAdminToken reads the Cairn administrator credential. The file must be a regular file, not a
// symbolic link, mode 0600 and owned by the caller, holding one cairn1. token and an optional
// trailing newline.
func LoadAdminToken(path string) (string, error) {
	if path == "-" {
		return readAdminToken(os.Stdin)
	}
	bad := errors.New("the admin token file is invalid: it must be a regular file, mode 0600, owned by you, holding one cairn1. token")
	info, err := os.Lstat(path)
	if err != nil || !info.Mode().IsRegular() || info.Mode() != 0600 {
		return "", bad
	}
	stat, ok := info.Sys().(*syscall.Stat_t)
	if !ok || int(stat.Uid) != os.Getuid() {
		return "", bad
	}
	f, err := os.OpenFile(path, os.O_RDONLY|syscall.O_NOFOLLOW, 0)
	if err != nil {
		return "", bad
	}
	defer f.Close()
	opened, err := f.Stat()
	if err != nil || !os.SameFile(info, opened) {
		return "", bad
	}
	data, err := io.ReadAll(io.LimitReader(f, maxAdminTokenFile+1))
	if err != nil || len(data) > maxAdminTokenFile {
		return "", bad
	}
	token := strings.TrimSuffix(string(data), "\n")
	if !adminTokenPattern.MatchString(token) {
		return "", bad
	}
	return token, nil
}

// readAdminToken reads the administrator credential from standard input, so an operator can pass
// it through ssh from a store on another machine and it never touches the gateway host's disk:
//
//	ssh host sudo garden invite zen --admin-token-file - < cairn-admin.token
func readAdminToken(r io.Reader) (string, error) {
	data, err := io.ReadAll(io.LimitReader(r, maxAdminTokenFile+1))
	if err != nil || len(data) > maxAdminTokenFile {
		return "", errors.New("the admin token on standard input is invalid")
	}
	token := strings.TrimSuffix(string(data), "\n")
	if !adminTokenPattern.MatchString(token) {
		return "", errors.New("the admin token on standard input is invalid: it must be one cairn1. token")
	}
	return token, nil
}

// AdminClient calls the Cairn admin API. It holds the administrator credential and no response.
type AdminClient struct {
	base  string
	token string
	http  *http.Client
}

// GrantSpec describes the one grant an invite receives: retrieve and ingest in the gateway's scope.
type GrantSpec struct {
	PrincipalID    string
	Realm          string
	Segments       []gardenauth.Segment
	Classification string
	ExpiresAt      time.Time
}

// Credential is the result of issue-credential. Plaintext is the new token; callers must not print it.
type Credential struct {
	ID        string
	Plaintext string
	ExpiresAt time.Time
}

// NewAdminClient validates the base URL and returns a client that never follows redirects and
// never uses a proxy.
func NewAdminClient(baseURL, token string) (*AdminClient, error) {
	base, err := validCairnOrigin(baseURL)
	if err != nil {
		return nil, err
	}
	if !adminTokenPattern.MatchString(token) {
		return nil, errors.New("the admin token is not a cairn1. credential")
	}
	transport := &http.Transport{
		Proxy:               nil,
		DialContext:         (&net.Dialer{Timeout: adminTimeout, KeepAlive: 30 * time.Second}).DialContext,
		TLSHandshakeTimeout: adminTimeout,
		DisableCompression:  true,
	}
	return &AdminClient{base: base, token: token, http: &http.Client{
		Transport: transport,
		Timeout:   adminTimeout,
		CheckRedirect: func(*http.Request, []*http.Request) error {
			return http.ErrUseLastResponse
		},
	}}, nil
}

// CreatePrincipal creates a workload principal with the given label and returns its ID.
func (a *AdminClient) CreatePrincipal(ctx context.Context, realm, label string) (string, error) {
	var result struct {
		PrincipalID string `json:"principal_id"`
	}
	body := map[string]string{"realm_id": realm, "kind": "workload", "label": label}
	if err := a.post(ctx, "create-principal", body, &result); err != nil {
		return "", err
	}
	if !canonicalUUID(result.PrincipalID) {
		return "", errors.New("create-principal: result has no principal_id")
	}
	return result.PrincipalID, nil
}

// CreateGrant creates the single retrieve and ingest grant and returns its ID.
func (a *AdminClient) CreateGrant(ctx context.Context, g GrantSpec) (string, error) {
	var result struct {
		GrantID string `json:"grant_id"`
	}
	grant := map[string]any{
		"principal_id":          g.PrincipalID,
		"realm_id":              g.Realm,
		"segments":              g.Segments,
		"operations":            []string{"retrieve", "ingest"},
		"read_clearance":        g.Classification,
		"write_classifications": []string{g.Classification},
		"expires_at":            formatAdminTime(g.ExpiresAt),
	}
	body := map[string]any{"realm_id": g.Realm, "grant": grant}
	if err := a.post(ctx, "create-grant", body, &result); err != nil {
		return "", err
	}
	if !canonicalUUID(result.GrantID) {
		return "", errors.New("create-grant: result has no grant_id")
	}
	return result.GrantID, nil
}

// IssueCredential issues one credential for the principal. On a validation failure after the
// receipt is committed, the returned Credential still carries the ID so the caller can revoke it.
func (a *AdminClient) IssueCredential(ctx context.Context, realm, principalID string, expires time.Time) (Credential, error) {
	var result struct {
		CredentialID string `json:"credential_id"`
		Plaintext    string `json:"plaintext"`
		ExpiresAt    string `json:"expires_at"`
	}
	body := map[string]string{"realm_id": realm, "principal_id": principalID, "expires_at": formatAdminTime(expires)}
	if err := a.post(ctx, "issue-credential", body, &result); err != nil {
		return Credential{}, err
	}
	if !canonicalUUID(result.CredentialID) {
		return Credential{}, errors.New("issue-credential: result has no credential_id")
	}
	cred := Credential{ID: result.CredentialID, Plaintext: result.Plaintext}
	returned, err := time.Parse(time.RFC3339Nano, result.ExpiresAt)
	if err != nil {
		return cred, errors.New("issue-credential: result has no valid expires_at")
	}
	cred.ExpiresAt = returned
	if !returned.Equal(expires.UTC().Truncate(time.Second)) {
		return cred, errors.New("issue-credential: the credential expiry differs from the request")
	}
	if !adminTokenPattern.MatchString(result.Plaintext) {
		return cred, errors.New("issue-credential: result has no usable plaintext")
	}
	return cred, nil
}

// RevokeCredential revokes a credential by ID.
func (a *AdminClient) RevokeCredential(ctx context.Context, realm, credentialID, reason string) error {
	body := map[string]string{"realm_id": realm, "credential_id": credentialID, "reason_code": reason}
	return a.post(ctx, "revoke-credential", body, nil)
}

// RevokeGrant revokes a grant by ID.
func (a *AdminClient) RevokeGrant(ctx context.Context, realm, grantID, reason string) error {
	body := map[string]string{"realm_id": realm, "grant_id": grantID, "reason_code": reason}
	return a.post(ctx, "revoke-grant", body, nil)
}

// post sends one admin request with a fresh idempotency key and decodes a committed receipt's
// result into result, when result is not nil.
func (a *AdminClient) post(ctx context.Context, name string, body any, result any) error {
	key, err := uuid.NewRandom()
	if err != nil {
		return fmt.Errorf("%s: no idempotency key", name)
	}
	data, err := json.Marshal(body)
	if err != nil {
		return fmt.Errorf("%s: invalid request", name)
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, a.base+"/v1/"+name, bytes.NewReader(data))
	if err != nil {
		return fmt.Errorf("%s: invalid request", name)
	}
	req.Header.Set("Authorization", "Bearer "+a.token)
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Accept", "application/json")
	req.Header.Set("Accept-Encoding", "identity")
	req.Header.Set("Idempotency-Key", key.String())
	resp, err := a.http.Do(req)
	if err != nil {
		return fmt.Errorf("%s: no response from Cairn: %w", name, err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return fmt.Errorf("%s: Cairn answered HTTP %d", name, resp.StatusCode)
	}
	raw, err := io.ReadAll(io.LimitReader(resp.Body, maxAdminResponse+1))
	if err != nil {
		return fmt.Errorf("%s: reading the response failed", name)
	}
	if len(raw) > maxAdminResponse {
		return fmt.Errorf("%s: the response exceeds 1 MiB", name)
	}
	var receipt struct {
		Outcome string          `json:"outcome"`
		Result  json.RawMessage `json:"result"`
	}
	if err := json.Unmarshal(raw, &receipt); err != nil {
		return fmt.Errorf("%s: the response is not JSON", name)
	}
	if receipt.Outcome != "committed" {
		return fmt.Errorf("%s: the outcome is not committed", name)
	}
	if result != nil {
		if err := json.Unmarshal(receipt.Result, result); err != nil {
			return fmt.Errorf("%s: the result is malformed", name)
		}
	}
	return nil
}

func formatAdminTime(t time.Time) string {
	return t.UTC().Truncate(time.Second).Format("2006-01-02T15:04:05Z")
}

func canonicalUUID(s string) bool {
	u, err := uuid.Parse(s)
	return err == nil && u.String() == s
}
