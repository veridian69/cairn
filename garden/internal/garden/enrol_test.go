package garden

import (
	"bytes"
	"context"
	"encoding/base64"
	"encoding/json"
	"io"
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/veridian69/cairn/garden/internal/identityseal"
	"github.com/veridian69/cairn/garden/internal/invite"
)

// enrolFixture starts a gateway with invites enabled in a fresh 0700 directory.
func enrolFixture(t *testing.T, before func(*fixture)) (*fixture, string) {
	t.Helper()
	dir := t.TempDir()
	// testing.TempDir is not created 0700; invites require a directory only its owner can use.
	if err := os.Chmod(dir, 0700); err != nil {
		t.Fatal(err)
	}
	f := fixtureBeforeStart(t, func(f *fixture) {
		f.cfg.InvitesDir = dir
		if before != nil {
			before(f)
		}
	})
	return f, dir
}

// sealInvite writes a redeemable record into dir and returns its secret.
func sealInvite(t *testing.T, dir, name, principal, harness string, expires time.Time) []byte {
	t.Helper()
	secret, err := invite.NewSecret()
	if err != nil {
		t.Fatal(err)
	}
	var rec invite.Record
	if harness == "cli" {
		var sealed []byte
		sealed, err = identityseal.Seal(name, "cairn1.enrolled-"+name, []byte("correct horse battery staple"))
		if err == nil {
			rec, err = invite.SealIdentity(secret, sealed, name, principal, expires)
		}
	} else {
		rec, err = invite.Seal(secret, "cairn1.enrolled-"+name, name, harness, principal, expires)
	}
	if err != nil {
		t.Fatal(err)
	}
	if err = invite.Write(dir, rec); err != nil {
		t.Fatal(err)
	}
	return secret
}

func secretBody(secret []byte) string {
	raw, _ := json.Marshal(invite.EnrolRequest{Secret: base64.RawURLEncoding.EncodeToString(secret)})
	return string(raw)
}

type enrolReply struct {
	status int
	header http.Header
	body   []byte
}

// postEnrol sends one request to /enrol. mutate may adjust the request before it is sent.
func postEnrol(t *testing.T, f *fixture, body string, mutate func(*http.Request)) enrolReply {
	t.Helper()
	req, err := http.NewRequest(http.MethodPost, f.http.URL+"/enrol", strings.NewReader(body))
	if err != nil {
		t.Fatal(err)
	}
	req.Header.Set("Content-Type", "application/json")
	if mutate != nil {
		mutate(req)
	}
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	data, _ := io.ReadAll(resp.Body)
	return enrolReply{status: resp.StatusCode, header: resp.Header, body: data}
}

// requireEnrolError checks the stable status, code and headers of an /enrol error response.
func requireEnrolError(t *testing.T, r enrolReply, status int, code string) {
	t.Helper()
	if r.status != status {
		t.Fatalf("status %d want %d: %s", r.status, status, r.body)
	}
	var e Error
	if err := json.Unmarshal(r.body, &e); err != nil || e.Code != code || e.Message == "" {
		t.Fatalf("error body %q, want code %s: %v", r.body, code, err)
	}
	requireEnrolHeaders(t, r)
}

func requireEnrolHeaders(t *testing.T, r enrolReply) {
	t.Helper()
	if r.header.Get("Cache-Control") != "no-store" {
		t.Fatalf("Cache-Control %q", r.header.Get("Cache-Control"))
	}
	if r.header.Get("Content-Type") != "application/json" {
		t.Fatalf("Content-Type %q", r.header.Get("Content-Type"))
	}
}

// requireNoSecret proves an invite secret is never echoed back.
func requireNoSecret(t *testing.T, r enrolReply, secret []byte) {
	t.Helper()
	if bytes.Contains(r.body, []byte(base64.RawURLEncoding.EncodeToString(secret))) {
		t.Fatal("response echoed the invite secret")
	}
}

func TestEnrolRedeemsOnceWithGatewayBinding(t *testing.T) {
	f, dir := enrolFixture(t, nil)
	secret := sealInvite(t, dir, "alice", principalA, "claude", time.Now().Add(time.Hour))
	r := postEnrol(t, f, secretBody(secret), nil)
	if r.status != http.StatusOK {
		t.Fatalf("status %d: %s", r.status, r.body)
	}
	requireEnrolHeaders(t, r)
	requireNoSecret(t, r, secret)
	var out invite.EnrolResponse
	if err := json.Unmarshal(r.body, &out); err != nil {
		t.Fatal(err)
	}
	if out.Name != "alice" || out.Harness != "claude" || out.Token != "cairn1.enrolled-alice" || out.InstanceID != instance || out.Classification != "internal" || out.Scope.Realm != "test" {
		t.Fatalf("response %+v", out)
	}
	again := postEnrol(t, f, secretBody(secret), nil)
	requireEnrolError(t, again, http.StatusNotFound, "not_found")
	if _, err := os.Stat(filepath.Join(dir, invite.ID(secret)+".json")); !os.IsNotExist(err) {
		t.Fatalf("redeemed record still present: %v", err)
	}
}

func TestEnrolNotReadyIsNotConsumedUntilReload(t *testing.T) {
	f, dir := enrolFixture(t, nil)
	secret := sealInvite(t, dir, "carol", principalCarol, "cli", time.Now().Add(time.Hour))
	r := postEnrol(t, f, secretBody(secret), nil)
	requireEnrolError(t, r, http.StatusConflict, "not_ready")
	requireNoSecret(t, r, secret)
	if _, err := os.Stat(filepath.Join(dir, invite.ID(secret)+".json")); err != nil {
		t.Fatalf("not_ready consumed the record: %v", err)
	}
	ctx := context.Background()
	if err := f.srv.Reload(ctx, withPrincipals(f.cfg, map[string]string{principalA: "alice", principalB: "bob", principalCarol: "carol"})); err != nil {
		t.Fatal(err)
	}
	r = postEnrol(t, f, secretBody(secret), nil)
	if r.status != http.StatusOK {
		t.Fatalf("after reload status %d: %s", r.status, r.body)
	}
	var out invite.EnrolResponse
	if err := json.Unmarshal(r.body, &out); err != nil || out.Name != "carol" || out.Harness != "cli" || out.Token != "" || out.SealedIdentity == "" {
		t.Fatalf("after reload response %+v %v", out, err)
	}
}

func TestEnrolExpiredInviteIsGoneAndConsumed(t *testing.T) {
	f, dir := enrolFixture(t, nil)
	secret := sealInvite(t, dir, "alice", principalA, "claude", time.Now().Add(-time.Minute))
	r := postEnrol(t, f, secretBody(secret), nil)
	requireEnrolError(t, r, http.StatusGone, "expired")
	requireNoSecret(t, r, secret)
	requireEnrolError(t, postEnrol(t, f, secretBody(secret), nil), http.StatusNotFound, "not_found")
}

func TestEnrolUnknownSecretIs404(t *testing.T) {
	f, _ := enrolFixture(t, nil)
	secret, err := invite.NewSecret()
	if err != nil {
		t.Fatal(err)
	}
	requireEnrolError(t, postEnrol(t, f, secretBody(secret), nil), http.StatusNotFound, "not_found")
}

func TestEnrolRejectsMalformedRequestsWithoutSpendingTokens(t *testing.T) {
	f, _ := enrolFixture(t, nil)
	secret, err := invite.NewSecret()
	if err != nil {
		t.Fatal(err)
	}
	raw := base64.RawURLEncoding.EncodeToString(secret)
	cases := map[string]string{
		"not json":         `{`,
		"empty body":       ``,
		"unknown field":    `{"secret":"` + raw + `","extra":1}`,
		"case alias":       `{"SECRET":"` + raw + `"}`,
		"duplicate key":    `{"secret":"` + raw + `","secret":"` + raw + `"}`,
		"trailing object":  `{"secret":"` + raw + `"}{}`,
		"trailing garbage": `{"secret":"` + raw + `"} x`,
		"padded":           `{"secret":"` + base64.URLEncoding.EncodeToString(secret) + `"}`,
		"short secret":     `{"secret":"` + base64.RawURLEncoding.EncodeToString(secret[:31]) + `"}`,
		"long secret":      `{"secret":"` + base64.RawURLEncoding.EncodeToString(append(secret, 0)) + `"}`,
		"not base64":       `{"secret":"!!!!"}`,
		"non-string":       `{"secret":1}`,
		"oversized":        `{"secret":"` + strings.Repeat("a", 5000) + `"}`,
	}
	for name, body := range cases {
		t.Run(name, func(t *testing.T) {
			requireEnrolError(t, postEnrol(t, f, body, nil), http.StatusBadRequest, "invalid_request")
		})
	}
	for name, ct := range map[string]string{"text/plain": "text/plain", "missing": ""} {
		t.Run("content type "+name, func(t *testing.T) {
			r := postEnrol(t, f, secretBody(secret), func(req *http.Request) {
				if ct == "" {
					req.Header.Del("Content-Type")
				} else {
					req.Header.Set("Content-Type", ct)
				}
			})
			requireEnrolError(t, r, http.StatusBadRequest, "invalid_request")
		})
	}
	// Malformed attempts never reach the bucket, so a well-formed redemption still gets through.
	dir := f.cfg.InvitesDir
	good := sealInvite(t, dir, "alice", principalA, "claude", time.Now().Add(time.Hour))
	r := postEnrol(t, f, secretBody(good), func(req *http.Request) { req.Header.Set("Content-Type", "application/json; charset=utf-8") })
	if r.status != http.StatusOK {
		t.Fatalf("malformed attempts spent the bucket: %d %s", r.status, r.body)
	}
}

func TestEnrolRefusesOriginAndWrongMethodAndHost(t *testing.T) {
	f, dir := enrolFixture(t, func(f *fixture) { f.cfg.PublicEndpoint = "https://garden.example/mcp" })
	secret := sealInvite(t, dir, "alice", principalA, "claude", time.Now().Add(time.Hour))
	body := secretBody(secret)

	requireEnrolError(t, postEnrol(t, f, body, func(req *http.Request) { req.Header.Set("Origin", "https://evil.example") }), http.StatusForbidden, "forbidden")

	get, err := http.NewRequest(http.MethodGet, f.http.URL+"/enrol", nil)
	if err != nil {
		t.Fatal(err)
	}
	get.Host = "garden.example"
	resp, err := http.DefaultClient.Do(get)
	if err != nil {
		t.Fatal(err)
	}
	resp.Body.Close()
	if resp.StatusCode != http.StatusMethodNotAllowed || resp.Header.Get("Allow") != http.MethodPost {
		t.Fatalf("GET: status %d Allow %q", resp.StatusCode, resp.Header.Get("Allow"))
	}

	requireEnrolError(t, postEnrol(t, f, body, func(req *http.Request) { req.Host = "evil.example" }), http.StatusForbidden, "forbidden")
	// The refused Host and Origin requests were rejected before any filesystem work, so the record survives.
	r := postEnrol(t, f, body, func(req *http.Request) { req.Host = "garden.example" })
	if r.status != http.StatusOK {
		t.Fatalf("public host: status %d: %s", r.status, r.body)
	}
}

func TestEnrolDisabledAndUnknownShapesAre404(t *testing.T) {
	f := newFixture(t)
	secret, err := invite.NewSecret()
	if err != nil {
		t.Fatal(err)
	}
	resp, err := http.Post(f.http.URL+"/enrol", "application/json", strings.NewReader(secretBody(secret)))
	if err != nil {
		t.Fatal(err)
	}
	resp.Body.Close()
	if resp.StatusCode != http.StatusNotFound {
		t.Fatalf("disabled enrolment: status %d", resp.StatusCode)
	}

	enabled, _ := enrolFixture(t, nil)
	resp, err = http.Post(enabled.http.URL+"/enrol?x=1", "application/json", strings.NewReader(secretBody(secret)))
	if err != nil {
		t.Fatal(err)
	}
	resp.Body.Close()
	if resp.StatusCode != http.StatusNotFound {
		t.Fatalf("query string on /enrol: status %d", resp.StatusCode)
	}
}

func TestEnrolRateLimitIsGlobalAndPrecedesFilesystem(t *testing.T) {
	clock := &testClock{t: time.Now()}
	missing := filepath.Join(t.TempDir(), "missing")
	f := fixtureBeforeStart(t, func(f *fixture) {
		f.cfg.InvitesDir = missing
		f.now = clock.Now
	})
	secret, err := invite.NewSecret()
	if err != nil {
		t.Fatal(err)
	}
	body := secretBody(secret)
	// The invites directory does not exist, so an admitted request answers 503 without reading anything.
	for i := 0; i < enrolBurst; i++ {
		requireEnrolError(t, postEnrol(t, f, body, nil), http.StatusServiceUnavailable, "unavailable")
	}
	r := postEnrol(t, f, body, nil)
	requireEnrolError(t, r, http.StatusTooManyRequests, "rate_limited")
	if r.header.Get("Retry-After") == "" {
		t.Fatal("429 without Retry-After")
	}
	// Ten tokens a minute is one every six seconds. Five seconds is not enough; seven is.
	clock.Advance(5 * time.Second)
	requireEnrolError(t, postEnrol(t, f, body, nil), http.StatusTooManyRequests, "rate_limited")
	clock.Advance(2 * time.Second)
	requireEnrolError(t, postEnrol(t, f, body, nil), http.StatusServiceUnavailable, "unavailable")
	requireEnrolError(t, postEnrol(t, f, body, nil), http.StatusTooManyRequests, "rate_limited")
}

func TestEnrolBucketSpendsOnlyForward(t *testing.T) {
	var b enrolBucket
	now := time.Unix(1_000_000, 0)
	for i := 0; i < enrolBurst; i++ {
		if !b.allow(now) {
			t.Fatalf("burst token %d refused", i)
		}
	}
	if b.allow(now) {
		t.Fatal("bucket admitted beyond burst")
	}
	if b.allow(now.Add(-time.Hour)) {
		t.Fatal("a backwards clock refilled the bucket")
	}
	if !b.allow(now.Add(7 * time.Second)) {
		t.Fatal("seven seconds did not refill one token")
	}
	if b.allow(now.Add(7 * time.Second)) {
		t.Fatal("one refill admitted two attempts")
	}
}
