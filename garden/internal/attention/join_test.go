package attention

import (
	"bytes"
	"context"
	"crypto/ecdsa"
	"crypto/elliptic"
	"crypto/rand"
	"crypto/sha256"
	"crypto/tls"
	"crypto/x509"
	"crypto/x509/pkix"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log"
	"math/big"
	"net"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"reflect"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"github.com/veridian69/cairn/garden/internal/gardenauth"
	"github.com/veridian69/cairn/garden/internal/invite"
)

// The join tests run a real TLS listener under a generated internal CA. The join dialer maps the
// endpoint host onto the listener, so the certificate names the real host. Tests that swap the
// dialer, the passphrase or the terminal seam are not parallel.

const (
	joinTestHost     = "garden.test"
	joinTestToken    = "cairn1.join-fixture-token"
	joinTestInstance = "33333333-3333-4333-8333-333333333333"
	joinTestPass     = "correct horse battery"
)

var (
	joinTestScope  = gardenauth.Scope{Realm: "example", Segments: []gardenauth.Segment{{Kind: "project", Identifier: "garden"}}}
	joinTestSecret = bytes.Repeat([]byte{7}, 32)
)

// joinCA makes a self-signed internal CA and returns it with its DER and key.
func joinCA(t *testing.T) (*x509.Certificate, []byte, *ecdsa.PrivateKey) {
	t.Helper()
	key, err := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	tmpl := &x509.Certificate{
		SerialNumber:          big.NewInt(time.Now().UnixNano()),
		Subject:               pkix.Name{CommonName: "garden test internal CA"},
		NotBefore:             time.Now().Add(-time.Hour),
		NotAfter:              time.Now().Add(24 * time.Hour),
		IsCA:                  true,
		BasicConstraintsValid: true,
		KeyUsage:              x509.KeyUsageCertSign | x509.KeyUsageCRLSign,
	}
	der, err := x509.CreateCertificate(rand.Reader, tmpl, tmpl, &key.PublicKey, key)
	if err != nil {
		t.Fatal(err)
	}
	cert, err := x509.ParseCertificate(der)
	if err != nil {
		t.Fatal(err)
	}
	return cert, der, key
}

// joinServerCert issues a server leaf for names under ca. The presented chain is the leaf and ca.
func joinServerCert(t *testing.T, ca *x509.Certificate, caKey *ecdsa.PrivateKey, caDER []byte, names ...string) tls.Certificate {
	t.Helper()
	key, err := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	tmpl := &x509.Certificate{
		SerialNumber: big.NewInt(time.Now().UnixNano()),
		Subject:      pkix.Name{CommonName: names[0]},
		DNSNames:     names,
		NotBefore:    time.Now().Add(-time.Hour),
		NotAfter:     time.Now().Add(24 * time.Hour),
		KeyUsage:     x509.KeyUsageDigitalSignature,
		ExtKeyUsage:  []x509.ExtKeyUsage{x509.ExtKeyUsageServerAuth},
	}
	der, err := x509.CreateCertificate(rand.Reader, tmpl, ca, &key.PublicKey, caKey)
	if err != nil {
		t.Fatal(err)
	}
	return tls.Certificate{Certificate: [][]byte{der, caDER}, PrivateKey: key}
}

// joinGateway serves cert on a local TLS listener and points the join dialer at it, whatever host
// the client dials. The previous dialer is restored when the test ends.
func joinGateway(t *testing.T, cert tls.Certificate, handler http.HandlerFunc) {
	t.Helper()
	srv := httptest.NewUnstartedServer(handler)
	srv.Config.ErrorLog = log.New(io.Discard, "", 0)
	srv.TLS = &tls.Config{Certificates: []tls.Certificate{cert}}
	srv.StartTLS()
	t.Cleanup(srv.Close)
	previous := joinDialContext
	joinDialContext = func(ctx context.Context, _, _ string) (net.Conn, error) {
		return (&net.Dialer{}).DialContext(ctx, "tcp", srv.Listener.Addr().String())
	}
	t.Cleanup(func() { joinDialContext = previous })
}

// joinEnrol answers POST /enrol as the gateway does for one invite: the given status and body
// once the secret matches. Every request that reaches it is counted.
func joinEnrol(calls *atomic.Int32, status int, body string) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		calls.Add(1)
		var req invite.EnrolRequest
		if r.Method != http.MethodPost || r.URL.Path != "/enrol" || r.Header.Get("Content-Type") != "application/json" ||
			json.NewDecoder(r.Body).Decode(&req) != nil || req.Secret != base64.RawURLEncoding.EncodeToString(joinTestSecret) {
			w.WriteHeader(http.StatusNotFound)
			return
		}
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(status)
		_, _ = io.WriteString(w, body)
	}
}

func joinReply(t *testing.T, name, harness string) string {
	t.Helper()
	resp := invite.EnrolResponse{Name: name, Harness: harness, Token: joinTestToken, InstanceID: joinTestInstance, Scope: joinTestScope, Classification: "internal"}
	if harness == "cli" {
		sealed, err := SealIdentityToken(name, joinTestToken, []byte(joinTestPass))
		if err != nil {
			t.Fatal(err)
		}
		resp.Token, resp.SealedIdentity = "", string(sealed)
	}
	data, err := json.Marshal(resp)
	if err != nil {
		t.Fatal(err)
	}
	return string(data)
}

func joinDigest(der []byte) string {
	sum := sha256.Sum256(der)
	return hex.EncodeToString(sum[:])
}

// joinCode is the invite that pins the CA with the given DER.
func joinCode(pinned []byte) invite.Code {
	return invite.Code{Endpoint: "https://" + joinTestHost + "/mcp", CASHA256: joinDigest(pinned), Secret: joinTestSecret}
}

// joinRig is an empty home and a gateway that presents the CA the invite pins.
type joinRig struct {
	home  string
	code  invite.Code
	caDER []byte
	calls *atomic.Int32
}

func newJoinRig(t *testing.T, status int, body string) joinRig {
	t.Helper()
	ca, caDER, caKey := joinCA(t)
	calls := new(atomic.Int32)
	joinGateway(t, joinServerCert(t, ca, caKey, caDER, joinTestHost), joinEnrol(calls, status, body))
	return joinRig{home: filepath.Join(privateHome(t), "garden"), code: joinCode(caDER), caDER: caDER, calls: calls}
}

// joinWriteHome writes a home bound to the test gateway, with tls/internal-ca.pem holding caDER.
// extra is spliced in as more garden.json fields, such as a defaults object.
func joinWriteHome(t *testing.T, home string, caDER []byte, instance, extra string) string {
	t.Helper()
	if err := os.MkdirAll(filepath.Join(home, "tls"), 0700); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(home, "tls", "internal-ca.pem"), pinnedPEM(caDER), 0644); err != nil {
		t.Fatal(err)
	}
	body := fmt.Sprintf(`{"garden_endpoint":"https://%s/mcp","garden_tls_ca_file":"tls/internal-ca.pem","instance_id":"%s","scope":{"realm":"example","segments":[{"kind":"project","identifier":"garden"}]},"classification":"internal"%s}`, joinTestHost, instance, extra)
	writeHome(t, home, body)
	data, err := os.ReadFile(filepath.Join(home, "garden.json"))
	if err != nil {
		t.Fatal(err)
	}
	return string(data)
}

// joinAbsent fails unless every named path under home is absent.
func joinAbsent(t *testing.T, home string, rels ...string) {
	t.Helper()
	for _, rel := range rels {
		if _, err := os.Lstat(filepath.Join(home, rel)); !errors.Is(err, os.ErrNotExist) {
			t.Fatalf("%s exists after a refused join (err %v)", rel, err)
		}
	}
}

func joinFileEquals(t *testing.T, path, want string) {
	t.Helper()
	got, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	if string(got) != want {
		t.Fatalf("%s changed:\n%s", path, got)
	}
}

func TestJoinFreshHomeInstallsHarnessIdentity(t *testing.T) {
	rig := newJoinRig(t, http.StatusOK, joinReply(t, "joiner", "opencode"))
	result, err := Join(context.Background(), rig.home, rig.code)
	if err != nil {
		t.Fatalf("join: %v", err)
	}
	if result.Name != "joiner" || result.Harness != "opencode" || result.Sealed || !result.DefaultSet || len(result.Written) != 3 {
		t.Fatalf("result: %+v", result)
	}
	if rig.calls.Load() != 1 {
		t.Fatalf("POST count %d", rig.calls.Load())
	}
	for rel, want := range map[string]os.FileMode{
		"tls":                     0700,
		"tls/internal-ca.pem":     0644,
		"identities":              0700,
		"identities/joiner.token": 0600,
		"garden.json":             0600,
	} {
		if got := mode(t, filepath.Join(rig.home, rel)); got != want {
			t.Fatalf("%s mode %o, want %o", rel, got, want)
		}
	}
	ca, err := os.ReadFile(filepath.Join(rig.home, "tls", "internal-ca.pem"))
	if err != nil || !sameCertificate(ca, rig.caDER) {
		t.Fatalf("CA file is not the pinned certificate: %v", err)
	}
	home, err := LoadHome(rig.home)
	if err != nil {
		t.Fatalf("garden.json does not load: %v", err)
	}
	if home.Config.GardenEndpoint != "https://garden.test/mcp" || home.Config.InstanceID != joinTestInstance || home.Config.Classification != "internal" || !reflect.DeepEqual(home.Config.Scope, joinTestScope) {
		t.Fatalf("binding: %+v", home.Config)
	}
	if len(home.Config.Defaults) != 1 || home.Config.Defaults["opencode"] != "joiner" {
		t.Fatalf("defaults: %v", home.Config.Defaults)
	}
	value, err := ReadCredential(home.TokenFile("joiner"))
	if err != nil || value != joinTestToken {
		t.Fatalf("installed token not readable: %v", err)
	}
	garden, _ := os.ReadFile(filepath.Join(rig.home, "garden.json"))
	if strings.Contains(string(garden), joinTestToken) {
		t.Fatal("garden.json holds the token")
	}
	joinAbsent(t, rig.home, "identities/joiner.sealed")
}

func TestJoinRefusesWrongCADigestBeforePosting(t *testing.T) {
	rig := newJoinRig(t, http.StatusOK, joinReply(t, "joiner", "opencode"))
	_, otherDER, _ := joinCA(t)
	rig.code.CASHA256 = joinDigest(otherDER)
	_, err := Join(context.Background(), rig.home, rig.code)
	if err == nil || !strings.Contains(err.Error(), "does not include the CA") {
		t.Fatalf("wrong digest accepted: %v", err)
	}
	if rig.calls.Load() != 0 {
		t.Fatalf("invite posted to a gateway that did not present the pinned CA (%d)", rig.calls.Load())
	}
	joinAbsent(t, rig.home, "garden.json", "identities", "tls")
}

func TestJoinRefusesChainWithoutPinnedCA(t *testing.T) {
	_, pinnedDER, _ := joinCA(t)
	gatewayCA, gatewayDER, gatewayKey := joinCA(t)
	calls := new(atomic.Int32)
	joinGateway(t, joinServerCert(t, gatewayCA, gatewayKey, gatewayDER, joinTestHost), joinEnrol(calls, http.StatusOK, joinReply(t, "joiner", "opencode")))
	home := filepath.Join(privateHome(t), "garden")
	if _, err := Join(context.Background(), home, joinCode(pinnedDER)); err == nil {
		t.Fatal("chain without the pinned CA accepted")
	}
	if calls.Load() != 0 {
		t.Fatalf("invite posted (%d)", calls.Load())
	}
	joinAbsent(t, home, "garden.json", "identities")
}

func TestJoinRefusesLeafForAnotherHost(t *testing.T) {
	ca, caDER, caKey := joinCA(t)
	calls := new(atomic.Int32)
	joinGateway(t, joinServerCert(t, ca, caKey, caDER, "other.test"), joinEnrol(calls, http.StatusOK, joinReply(t, "joiner", "opencode")))
	home := filepath.Join(privateHome(t), "garden")
	if _, err := Join(context.Background(), home, joinCode(caDER)); err == nil {
		t.Fatal("leaf for another host accepted")
	}
	if calls.Load() != 0 {
		t.Fatalf("invite posted to a leaf for another host (%d)", calls.Load())
	}
}

func TestJoinRefusesHomeBoundToAnotherEndpointBeforePosting(t *testing.T) {
	rig := newJoinRig(t, http.StatusOK, joinReply(t, "joiner", "opencode"))
	writeHome(t, rig.home, validHomeConfig)
	before, _ := os.ReadFile(filepath.Join(rig.home, "garden.json"))
	_, err := Join(context.Background(), rig.home, rig.code)
	if err == nil || !strings.Contains(err.Error(), "bound to https://garden.example/mcp") {
		t.Fatalf("home bound elsewhere accepted: %v", err)
	}
	if rig.calls.Load() != 0 {
		t.Fatalf("invite posted for a home bound elsewhere (%d)", rig.calls.Load())
	}
	joinFileEquals(t, filepath.Join(rig.home, "garden.json"), string(before))
}

func TestJoinRefusesDifferentCAFileBeforePosting(t *testing.T) {
	rig := newJoinRig(t, http.StatusOK, joinReply(t, "joiner", "opencode"))
	_, otherDER, _ := joinCA(t)
	joinWriteHome(t, rig.home, otherDER, joinTestInstance, "")
	if _, err := Join(context.Background(), rig.home, rig.code); err == nil || !strings.Contains(err.Error(), "different certificate") {
		t.Fatalf("different CA file accepted: %v", err)
	}
	if rig.calls.Load() != 0 {
		t.Fatalf("invite posted against a different CA file (%d)", rig.calls.Load())
	}
	joinAbsent(t, rig.home, "identities")
}

func TestJoinRefusesMissingCAFileBeforePosting(t *testing.T) {
	rig := newJoinRig(t, http.StatusOK, joinReply(t, "joiner", "opencode"))
	joinWriteHome(t, rig.home, rig.caDER, joinTestInstance, "")
	if err := os.Remove(filepath.Join(rig.home, "tls", "internal-ca.pem")); err != nil {
		t.Fatal(err)
	}
	if _, err := Join(context.Background(), rig.home, rig.code); err == nil || !strings.Contains(err.Error(), "which is missing") {
		t.Fatalf("missing CA file accepted: %v", err)
	}
	if rig.calls.Load() != 0 {
		t.Fatalf("invite posted (%d)", rig.calls.Load())
	}
}

func TestJoinRefusesResponseForAnotherInstanceAfterRedeeming(t *testing.T) {
	rig := newJoinRig(t, http.StatusOK, joinReply(t, "joiner", "opencode"))
	garden := joinWriteHome(t, rig.home, rig.caDER, "44444444-4444-4444-8444-444444444444", "")
	if _, err := Join(context.Background(), rig.home, rig.code); err == nil || !strings.Contains(err.Error(), "different instance") || !strings.Contains(err.Error(), "invite was used") {
		t.Fatalf("other instance accepted: %v", err)
	}
	if rig.calls.Load() != 1 {
		t.Fatalf("POST count %d", rig.calls.Load())
	}
	joinAbsent(t, rig.home, "identities/joiner.token")
	joinFileEquals(t, filepath.Join(rig.home, "garden.json"), garden)
}

func TestJoinRefusesDefaultConflictBeforeWriting(t *testing.T) {
	rig := newJoinRig(t, http.StatusOK, joinReply(t, "joiner", "opencode"))
	garden := joinWriteHome(t, rig.home, rig.caDER, joinTestInstance, `,"defaults":{"opencode":"other"}`)
	if _, err := Join(context.Background(), rig.home, rig.code); err == nil || !strings.Contains(err.Error(), "already defaults to other") {
		t.Fatalf("default conflict accepted: %v", err)
	}
	joinAbsent(t, rig.home, "identities/joiner.token")
	joinFileEquals(t, filepath.Join(rig.home, "garden.json"), garden)
}

func TestJoinAddsIdentityToBoundHomeKeepingItsFields(t *testing.T) {
	rig := newJoinRig(t, http.StatusOK, joinReply(t, "joiner", "opencode"))
	joinWriteHome(t, rig.home, rig.caDER, joinTestInstance, `,"defaults":{"claude":"spike"}`)
	caBefore, _ := os.ReadFile(filepath.Join(rig.home, "tls", "internal-ca.pem"))
	result, err := Join(context.Background(), rig.home, rig.code)
	if err != nil {
		t.Fatalf("join: %v", err)
	}
	if !result.DefaultSet || len(result.Written) != 2 {
		t.Fatalf("result: %+v", result)
	}
	home, err := LoadHome(rig.home)
	if err != nil {
		t.Fatal(err)
	}
	if home.Config.Defaults["claude"] != "spike" || home.Config.Defaults["opencode"] != "joiner" || len(home.Config.Defaults) != 2 {
		t.Fatalf("defaults: %v", home.Config.Defaults)
	}
	joinFileEquals(t, filepath.Join(rig.home, "tls", "internal-ca.pem"), string(caBefore))
	if value, err := ReadCredential(home.TokenFile("joiner")); err != nil || value != joinTestToken {
		t.Fatalf("token: %v", err)
	}
}

func TestJoinStatusRepliesAreExplained(t *testing.T) {
	cases := []struct {
		name   string
		status int
		want   string
	}{
		{"not ready", http.StatusConflict, "not loaded this identity yet"},
		{"unknown or used", http.StatusNotFound, "unknown or has already been used"},
		{"expired", http.StatusGone, "has expired"},
		{"rate limited", http.StatusTooManyRequests, "try again in a minute"},
		{"server error", http.StatusInternalServerError, "HTTP 500"},
		{"redirect not followed", http.StatusTemporaryRedirect, "HTTP 307"},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			rig := newJoinRig(t, tc.status, `{"code":"x","message":"not shown"}`)
			_, err := Join(context.Background(), rig.home, rig.code)
			if err == nil || !strings.Contains(err.Error(), tc.want) {
				t.Fatalf("status %d: %v, want %q", tc.status, err, tc.want)
			}
			if strings.Contains(err.Error(), "not shown") {
				t.Fatalf("gateway body echoed: %v", err)
			}
			if rig.calls.Load() != 1 {
				t.Fatalf("POST count %d", rig.calls.Load())
			}
			joinAbsent(t, rig.home, "identities", "garden.json")
		})
	}
}

func TestJoinRejectsMalformedReplies(t *testing.T) {
	validFields := func(t *testing.T) map[string]any {
		t.Helper()
		var fields map[string]any
		if err := json.Unmarshal([]byte(joinReply(t, "joiner", "opencode")), &fields); err != nil {
			t.Fatal(err)
		}
		return fields
	}
	encode := func(t *testing.T, fields map[string]any) string {
		t.Helper()
		data, err := json.Marshal(fields)
		if err != nil {
			t.Fatal(err)
		}
		return string(data)
	}
	withField := func(key string, value any) func(*testing.T) string {
		return func(t *testing.T) string {
			fields := validFields(t)
			fields[key] = value
			return encode(t, fields)
		}
	}
	cases := []struct {
		name string
		body func(*testing.T) string
		want string
	}{
		{"unknown field", withField("extra", 1), "not a valid join response"},
		{"trailing value", func(t *testing.T) string { return joinReply(t, "joiner", "opencode") + " {}" }, "not a valid join response"},
		{"oversized", func(*testing.T) string { return `{"name":"` + strings.Repeat("x", 70*1024) + `"}` }, "too large"},
		{"bad name", func(t *testing.T) string { return joinReply(t, "../joiner", "opencode") }, "invalid Garden identity name"},
		{"bad harness", func(t *testing.T) string { return joinReply(t, "joiner", "stdio") }, "unknown harness"},
		{"bad token", withField("token", "cairn1 with spaces"), "invalid credential"},
		{"bad instance", withField("instance_id", "not-a-uuid"), "invalid binding"},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			rig := newJoinRig(t, http.StatusOK, tc.body(t))
			_, err := Join(context.Background(), rig.home, rig.code)
			if err == nil || !strings.Contains(err.Error(), tc.want) {
				t.Fatalf("%s: %v, want %q", tc.name, err, tc.want)
			}
			if rig.calls.Load() != 1 {
				t.Fatalf("POST count %d", rig.calls.Load())
			}
			joinAbsent(t, rig.home, "identities", "garden.json", "tls")
		})
	}
}

func TestJoinRefusesNameAlreadyInHomeAfterRedeeming(t *testing.T) {
	for _, suffix := range []string{".token", ".sealed"} {
		t.Run(suffix, func(t *testing.T) {
			rig := newJoinRig(t, http.StatusOK, joinReply(t, "joiner", "opencode"))
			identities := filepath.Join(rig.home, "identities")
			if err := os.MkdirAll(identities, 0700); err != nil {
				t.Fatal(err)
			}
			existing := filepath.Join(identities, "joiner"+suffix)
			if err := os.WriteFile(existing, []byte("keep\n"), 0600); err != nil {
				t.Fatal(err)
			}
			if _, err := Join(context.Background(), rig.home, rig.code); err == nil || !strings.Contains(err.Error(), "already in this home") {
				t.Fatalf("existing name accepted: %v", err)
			}
			if rig.calls.Load() != 1 {
				t.Fatalf("POST count %d", rig.calls.Load())
			}
			joinFileEquals(t, existing, "keep\n")
			joinAbsent(t, rig.home, "garden.json", "tls/internal-ca.pem")
		})
	}
}

func TestAddIdentityRefusesSealedName(t *testing.T) {
	dir := privateHome(t)
	writeHome(t, dir, homeWithoutDefaults)
	if err := os.MkdirAll(filepath.Join(dir, "identities"), 0700); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(dir, "identities", "new1.sealed"), []byte("{}\n"), 0600); err != nil {
		t.Fatal(err)
	}
	src := sourceToken(t, identityFixtureToken, 0600)
	if _, err := AddIdentity(dir, "new1", src, ""); err == nil || !strings.Contains(err.Error(), "passphrase-sealed") {
		t.Fatalf("token added beside a sealed name: %v", err)
	}
	joinAbsent(t, dir, "identities/new1.token")
}

func TestAddIdentityRefusesPlaintextCLIDefault(t *testing.T) {
	dir := privateHome(t)
	writeHome(t, dir, homeWithoutDefaults)
	src := sourceToken(t, identityFixtureToken, 0600)
	if _, err := AddIdentity(dir, "new1", src, "cli"); err == nil || !strings.Contains(err.Error(), "garden join") {
		t.Fatalf("plaintext cli default accepted: %v", err)
	}
	joinAbsent(t, dir, "identities", "identities/new1.token")
}

func TestHarnessRefusesSealedOnlyIdentity(t *testing.T) {
	dir := privateHome(t)
	writeHome(t, dir, validHomeConfig)
	if err := os.MkdirAll(filepath.Join(dir, "identities"), 0700); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(dir, "identities", "operator.sealed"), []byte("{}\n"), 0600); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(dir, "identities", "spike.token"), []byte(identityFixtureToken+"\n"), 0600); err != nil {
		t.Fatal(err)
	}
	home, err := LoadHome(dir)
	if err != nil {
		t.Fatal(err)
	}
	if err := home.CheckHarnessIdentity("operator"); err == nil || !strings.Contains(err.Error(), "passphrase-sealed") {
		t.Fatalf("harness session allowed a sealed-only identity: %v", err)
	}
	if err := home.CheckHarnessIdentity("spike"); err != nil {
		t.Fatalf("token identity refused: %v", err)
	}
	if err := home.CheckHarnessIdentity("absent"); err != nil {
		t.Fatalf("absent identity refused by the sealed check: %v", err)
	}
}

func TestJoinHintedNameAlreadyInHomeIsRefusedBeforePosting(t *testing.T) {
	rig := newJoinRig(t, http.StatusOK, joinReply(t, "joiner", "opencode"))
	rig.code.Name, rig.code.Harness = "joiner", "opencode"
	identities := filepath.Join(rig.home, "identities")
	if err := os.MkdirAll(identities, 0700); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(identities, "joiner.token"), []byte("keep\n"), 0600); err != nil {
		t.Fatal(err)
	}
	if _, err := Join(context.Background(), rig.home, rig.code); err == nil || !strings.Contains(err.Error(), "not used") {
		t.Fatalf("existing hinted name: %v", err)
	}
	if rig.calls.Load() != 0 {
		t.Fatalf("the invite was spent: POST count %d", rig.calls.Load())
	}
}

func TestJoinRefusesAnswerThatDisagreesWithTheCode(t *testing.T) {
	rig := newJoinRig(t, http.StatusOK, joinReply(t, "other", "opencode"))
	rig.code.Name, rig.code.Harness = "joiner", "opencode"
	if _, err := Join(context.Background(), rig.home, rig.code); err == nil || !strings.Contains(err.Error(), "does not match the invite code") {
		t.Fatalf("mismatched answer: %v", err)
	}
	joinAbsent(t, rig.home, "identities/other.token", "identities/joiner.token", "garden.json")
}

// A cli identity arrives sealed at invite time and is written exactly as received; join needs no
// terminal and never sees the plain token.
func TestJoinWritesTheSealedCLIIdentityAsReceived(t *testing.T) {
	body := joinReply(t, "joiner", "cli")
	rig := newJoinRig(t, http.StatusOK, body)
	rig.code.Name, rig.code.Harness = "joiner", "cli"
	previous := openTTY
	openTTY = func() (*os.File, error) { return nil, errors.New("no tty") }
	t.Cleanup(func() { openTTY = previous })
	result, err := Join(context.Background(), rig.home, rig.code)
	if err != nil || !result.Sealed {
		t.Fatalf("cli join: %+v, %v", result, err)
	}
	var resp invite.EnrolResponse
	if err := json.Unmarshal([]byte(body), &resp); err != nil {
		t.Fatal(err)
	}
	joinFileEquals(t, filepath.Join(rig.home, "identities", "joiner.sealed"), resp.SealedIdentity)
	data, err := os.ReadFile(filepath.Join(rig.home, "identities", "joiner.sealed"))
	if err != nil {
		t.Fatal(err)
	}
	if opened, err := OpenIdentityToken("joiner", data, []byte(joinTestPass)); err != nil || opened != joinTestToken {
		t.Fatalf("sealed identity does not open: %v", err)
	}
	joinAbsent(t, rig.home, "identities/joiner.token")
}

func TestJoinRefusesAPlainTokenForACLIIdentity(t *testing.T) {
	data, err := json.Marshal(invite.EnrolResponse{Name: "joiner", Harness: "cli", Token: joinTestToken, InstanceID: joinTestInstance, Scope: joinTestScope, Classification: "internal"})
	if err != nil {
		t.Fatal(err)
	}
	rig := newJoinRig(t, http.StatusOK, string(data))
	if _, err := Join(context.Background(), rig.home, rig.code); err == nil || !strings.Contains(err.Error(), "sealed human identity") {
		t.Fatalf("plain cli token accepted: %v", err)
	}
	joinAbsent(t, rig.home, "identities/joiner.token", "identities/joiner.sealed", "garden.json")
}

func TestJoinRefusesASealedIdentityForAnAgent(t *testing.T) {
	sealed, err := SealIdentityToken("joiner", joinTestToken, []byte(joinTestPass))
	if err != nil {
		t.Fatal(err)
	}
	data, err := json.Marshal(invite.EnrolResponse{Name: "joiner", Harness: "opencode", SealedIdentity: string(sealed), InstanceID: joinTestInstance, Scope: joinTestScope, Classification: "internal"})
	if err != nil {
		t.Fatal(err)
	}
	rig := newJoinRig(t, http.StatusOK, string(data))
	if _, err := Join(context.Background(), rig.home, rig.code); err == nil || !strings.Contains(err.Error(), "invalid credential") {
		t.Fatalf("sealed agent identity accepted: %v", err)
	}
}
