package cmd

import (
	"bytes"
	"context"
	"crypto/ecdsa"
	"crypto/elliptic"
	"crypto/rand"
	"crypto/x509"
	"crypto/x509/pkix"
	"encoding/json"
	"encoding/pem"
	"errors"
	"github.com/veridian69/cairn/garden/internal/identityseal"
	"io"
	"math/big"
	"net"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"syscall"
	"testing"
	"time"

	"github.com/google/uuid"
	"github.com/veridian69/cairn/garden/internal/garden"
	"github.com/veridian69/cairn/garden/internal/gardenauth"
	"github.com/veridian69/cairn/garden/internal/invite"
)

const invTestAdminToken = "cairn1.admin-token_TEST.1"

// invFake is an httptest Cairn: the admin routes and the diagnostic endpoint. It records every
// admin request and can be told to fail one route.
type invFake struct {
	t          *testing.T
	srv        *httptest.Server
	instance   string
	mu         sync.Mutex
	calls      []invCall
	diagnoses  int
	failRoute  string
	failMode   string
	ingestOff  bool
	otherWho   bool
	principal  string
	grant      string
	credential string
	plaintext  string
}

type invCall struct {
	route string
	auth  string
	idem  string
	ctype string
	body  map[string]any
}

func newInvFake(t *testing.T) *invFake {
	t.Helper()
	f := &invFake{t: t, instance: uuid.NewString()}
	f.srv = httptest.NewServer(http.HandlerFunc(f.handle))
	t.Cleanup(f.srv.Close)
	return f
}

func (f *invFake) reply(w http.ResponseWriter, result any) {
	w.Header().Set("Content-Type", "application/json")
	_ = json.NewEncoder(w).Encode(map[string]any{"outcome": "committed", "result": result})
}

func (f *invFake) handle(w http.ResponseWriter, r *http.Request) {
	f.mu.Lock()
	defer f.mu.Unlock()
	raw, _ := io.ReadAll(io.LimitReader(r.Body, 1<<20))
	var body map[string]any
	if r.Method != http.MethodPost || json.Unmarshal(raw, &body) != nil {
		http.Error(w, "bad request", http.StatusBadRequest)
		return
	}
	if r.URL.Path == "/memory/v1/diagnose" {
		f.diagnose(w, r, body)
		return
	}
	call := invCall{route: r.URL.Path, auth: r.Header.Get("Authorization"), idem: r.Header.Get("Idempotency-Key"), ctype: r.Header.Get("Content-Type"), body: body}
	f.calls = append(f.calls, call)
	if call.auth != "Bearer "+invTestAdminToken || call.ctype != "application/json" {
		f.t.Errorf("%s: wrong admin headers", call.route)
		http.Error(w, "unauthorised", http.StatusUnauthorized)
		return
	}
	if u, err := uuid.Parse(call.idem); err != nil || u.Version() != 4 {
		f.t.Errorf("%s: Idempotency-Key %q is not a UUID version 4", call.route, call.idem)
	}
	if r.URL.Path == f.failRoute {
		switch f.failMode {
		case "http":
			http.Error(w, "unavailable", http.StatusServiceUnavailable)
		case "rejected":
			w.Header().Set("Content-Type", "application/json")
			_, _ = io.WriteString(w, `{"outcome":"rejected","result":{}}`)
		}
		return
	}
	switch r.URL.Path {
	case "/v1/create-principal":
		if body["realm_id"] != "cairn" || body["kind"] != "workload" || body["label"] != "zen-garden" {
			f.t.Errorf("create-principal body %v", body)
		}
		f.principal = uuid.NewString()
		f.reply(w, map[string]any{"principal_id": f.principal})
	case "/v1/create-grant":
		inner, _ := body["grant"].(map[string]any)
		if inner["principal_id"] != f.principal {
			f.t.Errorf("create-grant names %v, want %s", inner["principal_id"], f.principal)
		}
		f.grant = uuid.NewString()
		f.reply(w, map[string]any{"grant_id": f.grant})
	case "/v1/issue-credential":
		if body["principal_id"] != f.principal {
			f.t.Errorf("issue-credential names %v, want %s", body["principal_id"], f.principal)
		}
		f.credential = uuid.NewString()
		f.plaintext = "cairn1." + strings.ReplaceAll(uuid.NewString(), "-", "") + ".test"
		expires, err := time.Parse(time.RFC3339, body["expires_at"].(string))
		if err != nil {
			f.t.Errorf("issue-credential expiry %v is not RFC3339", body["expires_at"])
		}
		f.reply(w, map[string]any{"credential_id": f.credential, "plaintext": f.plaintext, "expires_at": expires.Format("2006-01-02T15:04:05.000000Z")})
	case "/v1/revoke-credential", "/v1/revoke-grant":
		f.reply(w, map[string]any{})
	default:
		http.NotFound(w, r)
	}
}

func (f *invFake) diagnose(w http.ResponseWriter, r *http.Request, body map[string]any) {
	f.diagnoses++
	if f.plaintext == "" || r.Header.Get("Authorization") != "Bearer "+f.plaintext {
		http.Error(w, "unauthorised", http.StatusUnauthorized)
		return
	}
	who := f.principal
	if f.otherWho {
		who = uuid.NewString()
	}
	digest := strings.Repeat("ab", 32)
	identity := map[string]any{
		"instance_id": f.instance, "product_version": "0.7.18", "contract_identity": "cairn.memory/v1",
		"contract_digest": digest, "mcp_contract_digest": digest, "principal_id": who, "principal_kind": "workload",
		"scope": body["scope"], "classification": body["classification"],
		"permissions":  map[string]any{"retrieve": true, "ingest": !f.ingestOff, "promote": false, "invalidate": false},
		"evaluated_at": "2026-11-08T10:00:00.000000Z", "permission_basis": "current_grants_only",
	}
	w.Header().Set("Content-Type", "application/json")
	_ = json.NewEncoder(w).Encode(identity)
}

func (f *invFake) snapshot() ([]invCall, int) {
	f.mu.Lock()
	defer f.mu.Unlock()
	return append([]invCall(nil), f.calls...), f.diagnoses
}

// invFixture is a temporary gateway: configuration, chain with an internal CA, invites directory,
// admin token file and a fake Cairn. runCommand is stubbed, so systemctl is never reached.
type invFixture struct {
	t          *testing.T
	root       string
	configPath string
	invitesDir string
	tokenPath  string
	chainPath  string
	fake       *invFake
	caDigest   string
	reloads    *[][]string
	reloadErr  error
	chain      []byte
	// passphrase is what the fake terminal answers; passphraseErr makes it refuse, as a missing
	// terminal or a mismatched confirmation would. prompts records each question asked.
	passphrase    []byte
	passphraseErr error
	prompts       []string
	confirms      []bool
}

func newInvFixture(t *testing.T) *invFixture {
	t.Helper()
	root := t.TempDir()
	fx := &invFixture{
		t:          t,
		root:       root,
		configPath: filepath.Join(root, "etc", "garden.json"),
		invitesDir: filepath.Join(root, "invites"),
		tokenPath:  filepath.Join(root, "admin.token"),
		chainPath:  filepath.Join(root, "chain.pem"),
	}
	mustMkdir(t, filepath.Dir(fx.configPath), 0750)
	mustMkdir(t, fx.invitesDir, 0700)
	fx.fake = newInvFake(t)

	ca, caKey := invTestCA(t, "garden internal CA")
	leaf := invTestLeaf(t, ca, caKey, "garden.test")
	fx.caDigest = invite.CADigest(ca)
	fx.chain = append(invPEM(leaf), invPEM(ca)...)
	writeInvFile(t, fx.chainPath, fx.chain, 0600)
	writeInvFile(t, fx.tokenPath, []byte(invTestAdminToken+"\n"), 0600)
	fx.writeConfig(nil)

	reloads := [][]string{}
	fx.reloads = &reloads
	orig := runCommand
	runCommand = func(ctx context.Context, name string, args ...string) ([]byte, error) {
		reloads = append(reloads, append([]string{name}, args...))
		return []byte("reload output"), fx.reloadErr
	}
	t.Cleanup(func() { runCommand = orig })
	fx.passphrase = []byte("correct horse battery")
	fx.passphraseErr = errors.New("no terminal")
	origPass := readPassphrase
	readPassphrase = func(prompt string, confirm bool) ([]byte, error) {
		fx.prompts = append(fx.prompts, prompt)
		fx.confirms = append(fx.confirms, confirm)
		if fx.passphraseErr != nil {
			return nil, fx.passphraseErr
		}
		return append([]byte(nil), fx.passphrase...), nil
	}
	t.Cleanup(func() { readPassphrase = origPass })
	return fx
}

func mustMkdir(t *testing.T, dir string, mode os.FileMode) {
	t.Helper()
	if err := os.MkdirAll(dir, mode); err != nil {
		t.Fatal(err)
	}
	if err := os.Chmod(dir, mode); err != nil {
		t.Fatal(err)
	}
}

func writeInvFile(t *testing.T, path string, data []byte, mode os.FileMode) {
	t.Helper()
	if err := os.WriteFile(path, data, mode); err != nil {
		t.Fatal(err)
	}
	if err := os.Chmod(path, mode); err != nil {
		t.Fatal(err)
	}
}

func (fx *invFixture) principalID() string { return "2c6a6a2e-6e7b-4a58-9f6d-1f4c1d2e3a4b" }

// writeConfig writes the gateway configuration, mode 0640, after applying mutate.
func (fx *invFixture) writeConfig(mutate func(*garden.Config)) {
	fx.t.Helper()
	cfg := garden.Config{
		PublicEndpoint: "https://garden.test:8443/mcp",
		Listen:         "127.0.0.1:8443",
		DataDir:        "/var/lib/garden-test",
		DaemonURLFile:  "/run/garden-test/daemon.url",
		TLSCertFile:    fx.chainPath,
		TLSKeyFile:     "/etc/garden-test/tls.key",
		Auth: gardenauth.Config{
			Endpoint:       fx.fake.srv.URL + "/memory/v1/diagnose",
			InstanceID:     fx.fake.instance,
			Scope:          gardenauth.Scope{Realm: "cairn", Segments: []gardenauth.Segment{{Kind: "garden", Identifier: "agents"}}},
			Classification: "internal",
		},
		Principals: map[string]string{fx.principalID(): "kay"},
		InvitesDir: fx.invitesDir,
	}
	if mutate != nil {
		mutate(&cfg)
	}
	data, err := json.MarshalIndent(cfg, "", "  ")
	if err != nil {
		fx.t.Fatal(err)
	}
	writeInvFile(fx.t, fx.configPath, append(data, '\n'), 0640)
}

// args returns the standard flags for name, followed by extra, so later flags override earlier.
func (fx *invFixture) args(name string, extra ...string) []string {
	base := []string{name,
		"--harness", "opencode",
		"--admin-token-file", fx.tokenPath,
		"--gateway-config", fx.configPath,
		"--cairn-url", fx.fake.srv.URL,
		"--reload-unit", "garden-mcp.service",
	}
	return append(base, extra...)
}

// runInvCommand runs a fresh invite command and returns its streams and error.
func runInvCommand(args []string) (string, string, error) {
	command := newInviteCommand()
	var out, errOut bytes.Buffer
	command.SetOut(&out)
	command.SetErr(&errOut)
	command.SetArgs(args)
	command.SilenceUsage = true
	command.SilenceErrors = true
	err := command.Execute()
	return out.String(), errOut.String(), err
}

func invKey(t *testing.T) *ecdsa.PrivateKey {
	t.Helper()
	key, err := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	return key
}

func invSerial(t *testing.T) *big.Int {
	t.Helper()
	n, err := rand.Int(rand.Reader, big.NewInt(1<<62))
	if err != nil {
		t.Fatal(err)
	}
	return n
}

func invTestCA(t *testing.T, name string) (*x509.Certificate, *ecdsa.PrivateKey) {
	t.Helper()
	key := invKey(t)
	tmpl := &x509.Certificate{
		SerialNumber:          invSerial(t),
		Subject:               pkix.Name{CommonName: name},
		NotBefore:             time.Now().Add(-time.Hour),
		NotAfter:              time.Now().Add(48 * time.Hour),
		IsCA:                  true,
		BasicConstraintsValid: true,
		KeyUsage:              x509.KeyUsageCertSign | x509.KeyUsageCRLSign,
	}
	der, err := x509.CreateCertificate(rand.Reader, tmpl, tmpl, &key.PublicKey, key)
	if err != nil {
		t.Fatal(err)
	}
	ca, err := x509.ParseCertificate(der)
	if err != nil {
		t.Fatal(err)
	}
	return ca, key
}

func invTestLeaf(t *testing.T, ca *x509.Certificate, caKey *ecdsa.PrivateKey, host string) *x509.Certificate {
	t.Helper()
	key := invKey(t)
	tmpl := &x509.Certificate{
		SerialNumber: invSerial(t),
		Subject:      pkix.Name{CommonName: host},
		DNSNames:     []string{host},
		IPAddresses:  []net.IP{net.ParseIP("127.0.0.1")},
		NotBefore:    time.Now().Add(-time.Hour),
		NotAfter:     time.Now().Add(48 * time.Hour),
		KeyUsage:     x509.KeyUsageDigitalSignature,
		ExtKeyUsage:  []x509.ExtKeyUsage{x509.ExtKeyUsageServerAuth},
	}
	der, err := x509.CreateCertificate(rand.Reader, tmpl, ca, &key.PublicKey, caKey)
	if err != nil {
		t.Fatal(err)
	}
	leaf, err := x509.ParseCertificate(der)
	if err != nil {
		t.Fatal(err)
	}
	return leaf
}

func invPEM(c *x509.Certificate) []byte {
	return pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: c.Raw})
}

// invRecords returns the invite records in the directory, by file name.
func (fx *invFixture) invRecords() []string {
	entries, err := os.ReadDir(fx.invitesDir)
	if err != nil {
		fx.t.Fatal(err)
	}
	var names []string
	for _, e := range entries {
		if strings.HasSuffix(e.Name(), ".json") {
			names = append(names, e.Name())
		}
	}
	return names
}

func (fx *invFixture) configBytes() []byte {
	data, err := os.ReadFile(fx.configPath)
	if err != nil {
		fx.t.Fatal(err)
	}
	return data
}

func TestInviteCommandIssuesRedeemableCode(t *testing.T) {
	fx := newInvFixture(t)
	before, err := os.Stat(fx.configPath)
	if err != nil {
		t.Fatal(err)
	}
	stat := before.Sys().(*syscall.Stat_t)

	stdout, stderr, err := runInvCommand(fx.args("zen"))
	if err != nil {
		t.Fatalf("invite failed: %v\nstderr: %s", err, stderr)
	}

	// stdout is the code alone.
	if strings.Count(stdout, "\n") != 1 {
		t.Fatalf("stdout is not one line: %q", stdout)
	}
	codeText := strings.TrimSuffix(stdout, "\n")
	code, err := invite.Decode(codeText)
	if err != nil {
		t.Fatalf("code does not decode: %v", err)
	}
	if code.Endpoint != "https://garden.test:8443/mcp" || code.CASHA256 != fx.caDigest {
		t.Fatalf("code pins %q / %q", code.Endpoint, code.CASHA256)
	}

	// The record opens to the issued token, and nothing else is in the directory.
	records := fx.invRecords()
	if len(records) != 1 || records[0] != invite.ID(code.Secret)+".json" {
		t.Fatalf("invite records %v", records)
	}
	data, err := os.ReadFile(filepath.Join(fx.invitesDir, records[0]))
	if err != nil {
		t.Fatal(err)
	}
	var rec invite.Record
	if err := json.Unmarshal(data, &rec); err != nil {
		t.Fatal(err)
	}
	got, err := rec.Open(code.Secret, time.Now())
	if err != nil {
		t.Fatalf("record does not open with the code's secret: %v", err)
	}
	if got != fx.fake.plaintext {
		t.Fatal("record opens to a different token than the one issued")
	}
	if rec.Name != "zen" || rec.Harness != "opencode" || rec.PrincipalID != fx.fake.principal || rec.GrantID != fx.fake.grant || rec.CredentialID != fx.fake.credential {
		t.Fatalf("record fields %+v", rec)
	}
	if exp, _ := time.Parse(time.RFC3339, rec.ExpiresAt); time.Until(exp) < 23*time.Hour || time.Until(exp) > 25*time.Hour {
		t.Fatalf("invite expires %s, want about 24h", rec.ExpiresAt)
	}

	// The gateway gained exactly one principal; the rest is intact and the file keeps its owner and mode.
	cfg, err := garden.LoadConfig(fx.configPath)
	if err != nil {
		t.Fatal(err)
	}
	if len(cfg.Principals) != 2 || cfg.Principals[fx.principalID()] != "kay" || cfg.Principals[fx.fake.principal] != "zen" {
		t.Fatalf("principals %v", cfg.Principals)
	}
	after, err := os.Stat(fx.configPath)
	if err != nil {
		t.Fatal(err)
	}
	if after.Mode().Perm() != 0640 || after.Sys().(*syscall.Stat_t).Uid != stat.Uid || after.Sys().(*syscall.Stat_t).Gid != stat.Gid {
		t.Fatalf("config mode %v or owner changed", after.Mode())
	}

	// Admin calls: fresh keys, bearer auth, the gateway's scope, and the default one-year expiry.
	calls, diagnoses := fx.fake.snapshot()
	routes := []string{}
	keys := map[string]bool{}
	for _, c := range calls {
		routes = append(routes, c.route)
		if keys[c.idem] {
			t.Fatalf("Idempotency-Key %s reused", c.idem)
		}
		keys[c.idem] = true
	}
	if strings.Join(routes, ",") != "/v1/create-principal,/v1/create-grant,/v1/issue-credential" || diagnoses != 1 {
		t.Fatalf("admin routes %v, diagnoses %d", routes, diagnoses)
	}
	grant, _ := calls[1].body["grant"].(map[string]any)
	if grant["read_clearance"] != "internal" || grant["expires_at"] == nil {
		t.Fatalf("grant body %v", grant)
	}
	ops, _ := grant["operations"].([]any)
	if len(ops) != 2 || ops[0] != "retrieve" || ops[1] != "ingest" {
		t.Fatalf("grant operations %v", ops)
	}
	expires, _ := time.Parse(time.RFC3339, calls[2].body["expires_at"].(string))
	if d := expires.Sub(time.Now().AddDate(1, 0, 0)); d < -time.Minute || d > time.Minute {
		t.Fatalf("credential expires %s, want about one year", expires)
	}

	// The reload seam ran once, with the unit, and nothing was reloaded for real.
	if len(*fx.reloads) != 1 || strings.Join((*fx.reloads)[0], " ") != "systemctl reload garden-mcp.service" {
		t.Fatalf("reload calls %v", *fx.reloads)
	}

	// Human-facing summary on stderr, never the token, the plaintext or the code.
	for _, want := range []string{"name: zen", "principal: " + fx.fake.principal, "harness default: opencode", "credential expires:", "invite expires:", "single use", "garden join"} {
		if !strings.Contains(stderr, want) {
			t.Errorf("stderr lacks %q:\n%s", want, stderr)
		}
	}
	for _, secret := range []string{invTestAdminToken, fx.fake.plaintext, "cairn1."} {
		if strings.Contains(stderr, secret) || strings.Contains(stdout, secret) {
			t.Fatalf("output leaks a credential: %q", secret)
		}
	}
}

func TestInviteCommandPrunesExpiredInvites(t *testing.T) {
	fx := newInvFixture(t)
	secret, err := invite.NewSecret()
	if err != nil {
		t.Fatal(err)
	}
	old, err := invite.Seal(secret, invTestAdminToken, "old", "", uuid.NewString(), time.Now().Add(-time.Hour))
	if err != nil {
		t.Fatal(err)
	}
	if err := invite.Write(fx.invitesDir, old); err != nil {
		t.Fatal(err)
	}
	if _, _, err := runInvCommand(fx.args("zen")); err != nil {
		t.Fatal(err)
	}
	for _, name := range fx.invRecords() {
		if name == old.ID+".json" {
			t.Fatal("expired invite was not pruned")
		}
	}
	if len(fx.invRecords()) != 1 {
		t.Fatalf("records after run: %v", fx.invRecords())
	}
}

func TestInviteCommandReloadSeam(t *testing.T) {
	fx := newInvFixture(t)
	stdout, stderr, err := runInvCommand(fx.args("zen", "--reload-unit", ""))
	if err != nil {
		t.Fatal(err)
	}
	if len(*fx.reloads) != 0 {
		t.Fatalf("empty --reload-unit still ran %v", *fx.reloads)
	}
	if !strings.Contains(stderr, "SIGHUP") {
		t.Fatalf("no SIGHUP instruction: %s", stderr)
	}
	if _, err := invite.Decode(strings.TrimSuffix(stdout, "\n")); err != nil {
		t.Fatalf("code missing: %v", err)
	}
}

func TestInviteCommandReloadFailureKeepsInvite(t *testing.T) {
	fx := newInvFixture(t)
	fx.reloadErr = errors.New("exit status 5")
	stdout, stderr, err := runInvCommand(fx.args("zen"))
	if err != nil {
		t.Fatalf("a failed reload must not fail the invite: %v", err)
	}
	if !strings.Contains(stderr, "warning") || !strings.Contains(stderr, "not_ready") || !strings.Contains(stderr, "systemctl reload garden-mcp.service") {
		t.Fatalf("no reload warning: %s", stderr)
	}
	if _, err := invite.Decode(strings.TrimSuffix(stdout, "\n")); err != nil {
		t.Fatalf("code missing after reload failure: %v", err)
	}
	if len(fx.invRecords()) != 1 {
		t.Fatal("the invite record was removed after a reload failure")
	}
}

func TestInviteCommandRefusesBeforeAnyAdminCall(t *testing.T) {
	cases := []struct {
		name  string
		setup func(fx *invFixture)
		args  func(fx *invFixture) []string
	}{
		{"name with capital", nil, func(fx *invFixture) []string { return fx.args("Zen") }},
		{"name with underscore", nil, func(fx *invFixture) []string { return fx.args("zen_") }},
		{"name already mapped", nil, func(fx *invFixture) []string { return fx.args("kay") }},
		{"bad harness", nil, func(fx *invFixture) []string { return fx.args("zen", "--harness", "claude-code") }},
		{"ttl too long", nil, func(fx *invFixture) []string { return fx.args("zen", "--ttl", "169h") }},
		{"ttl zero", nil, func(fx *invFixture) []string { return fx.args("zen", "--ttl", "0s") }},
		{"expiry in the past", nil, func(fx *invFixture) []string {
			return fx.args("zen", "--expires", "2000-01-01T00:00:00Z")
		}},
		{"expiry not a time", nil, func(fx *invFixture) []string { return fx.args("zen", "--expires", "1y") }},
		{"expiry before the invite's", nil, func(fx *invFixture) []string { return fx.args("zen", "--ttl", "48h", "--expires", "24h") }},
		{"plain http to a public host", nil, func(fx *invFixture) []string {
			return fx.args("zen", "--cairn-url", "http://cairn.example.net:8100")
		}},
		{"bad reload unit", nil, func(fx *invFixture) []string { return fx.args("zen", "--reload-unit", "-x") }},
		{"token mode 0640", func(fx *invFixture) { _ = os.Chmod(fx.tokenPath, 0640) }, func(fx *invFixture) []string { return fx.args("zen") }},
		{"token is a symlink", func(fx *invFixture) {
			target := filepath.Join(fx.root, "real.token")
			writeInvFile(fx.t, target, []byte(invTestAdminToken), 0600)
			_ = os.Remove(fx.tokenPath)
			if err := os.Symlink(target, fx.tokenPath); err != nil {
				fx.t.Fatal(err)
			}
		}, func(fx *invFixture) []string { return fx.args("zen") }},
		{"token not a cairn credential", func(fx *invFixture) {
			writeInvFile(fx.t, fx.tokenPath, []byte("sk-not-cairn\n"), 0600)
		}, func(fx *invFixture) []string { return fx.args("zen") }},
		{"chain without CA", func(fx *invFixture) {
			writeInvFile(fx.t, fx.chainPath, invLeafOnly(fx), 0600)
		}, func(fx *invFixture) []string { return fx.args("zen") }},
		{"invites directory too open", func(fx *invFixture) { _ = os.Chmod(fx.invitesDir, 0755) }, func(fx *invFixture) []string { return fx.args("zen") }},
		{"config without invites_dir", func(fx *invFixture) {
			fx.writeConfig(func(c *garden.Config) { c.InvitesDir = "" })
		}, func(fx *invFixture) []string { return fx.args("zen") }},
		{"config without public_endpoint", func(fx *invFixture) {
			fx.writeConfig(func(c *garden.Config) { c.PublicEndpoint = "" })
		}, func(fx *invFixture) []string { return fx.args("zen") }},
		{"config without tls_cert_file", func(fx *invFixture) {
			fx.writeConfig(func(c *garden.Config) { c.TLSCertFile = ""; c.TLSKeyFile = "" })
		}, func(fx *invFixture) []string { return fx.args("zen") }},
		{"public endpoint not https", func(fx *invFixture) {
			fx.writeConfig(func(c *garden.Config) { c.PublicEndpoint = "http://garden.test:8443/mcp" })
		}, func(fx *invFixture) []string { return fx.args("zen") }},
		{"missing admin token file", nil, func(fx *invFixture) []string {
			return fx.args("zen", "--admin-token-file", filepath.Join(fx.root, "absent"))
		}},
		{"chain for another host", func(fx *invFixture) {
			fx.writeConfig(func(c *garden.Config) { c.PublicEndpoint = "https://other.test:8443/mcp" })
		}, func(fx *invFixture) []string { return fx.args("zen") }},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			fx := newInvFixture(t)
			if tc.setup != nil {
				tc.setup(fx)
			}
			before := fx.configBytes()
			_, _, err := runInvCommand(tc.args(fx))
			if err == nil {
				t.Fatal("invite was not refused")
			}
			t.Logf("refused: %v", err)
			if calls, diagnoses := fx.fake.snapshot(); len(calls) != 0 || diagnoses != 0 {
				t.Fatalf("refused invite still called Cairn: %d admin calls, %d diagnoses", len(calls), diagnoses)
			}
			if len(fx.invRecords()) != 0 {
				t.Fatal("refused invite wrote a record")
			}
			if string(fx.configBytes()) != string(before) {
				t.Fatal("refused invite changed the configuration")
			}
			if len(*fx.reloads) != 0 {
				t.Fatal("refused invite reloaded the gateway")
			}
		})
	}
}

// invLeafOnly returns the server certificate from the fixture chain, without its CA.
func invLeafOnly(fx *invFixture) []byte {
	block, _ := pem.Decode(fx.chain)
	return pem.EncodeToMemory(block)
}

func TestInviteCommandRevokesWhenGrantFails(t *testing.T) {
	fx := newInvFixture(t)
	fx.fake.failRoute = "/v1/create-grant"
	fx.fake.failMode = "rejected"
	before := fx.configBytes()
	_, _, err := runInvCommand(fx.args("zen"))
	if err == nil {
		t.Fatal("a rejected grant was reported as success")
	}
	if !strings.Contains(err.Error(), "principal "+fx.fake.principal) || !strings.Contains(err.Error(), "grant unknown") {
		t.Fatalf("failure does not report the principal and unknown grant: %v", err)
	}
	calls, _ := fx.fake.snapshot()
	if len(calls) != 2 {
		t.Fatalf("calls after a failed grant: %d (no revocation expected without a grant ID)", len(calls))
	}
	if len(fx.invRecords()) != 0 || string(fx.configBytes()) != string(before) {
		t.Fatal("failed grant left a record or changed the configuration")
	}
}

func TestInviteCommandRevokesCredentialAndGrantOnLaterFailures(t *testing.T) {
	cases := map[string]struct {
		configure func(fx *invFixture)
		revoked   []string
	}{
		"issue fails":                      {func(fx *invFixture) { fx.fake.failRoute = "/v1/issue-credential"; fx.fake.failMode = "http" }, []string{"/v1/revoke-grant"}},
		"diagnose names another principal": {func(fx *invFixture) { fx.fake.otherWho = true }, []string{"/v1/revoke-credential", "/v1/revoke-grant"}},
		"diagnose lacks ingest":            {func(fx *invFixture) { fx.fake.ingestOff = true }, []string{"/v1/revoke-credential", "/v1/revoke-grant"}},
		"revoke grant also fails": {func(fx *invFixture) {
			fx.fake.ingestOff = true
			fx.fake.failRoute = "/v1/revoke-grant"
			fx.fake.failMode = "http"
		}, []string{"/v1/revoke-credential", "/v1/revoke-grant"}},
	}
	for name, tc := range cases {
		t.Run(name, func(t *testing.T) {
			fx := newInvFixture(t)
			tc.configure(fx)
			_, _, err := runInvCommand(fx.args("zen"))
			if err == nil {
				t.Fatal("the invite was not refused")
			}
			if !strings.Contains(err.Error(), "principal "+fx.fake.principal) {
				t.Fatalf("failure omits the principal: %v", err)
			}
			calls, _ := fx.fake.snapshot()
			var revoked []string
			for _, c := range calls {
				if strings.HasPrefix(c.route, "/v1/revoke-") {
					revoked = append(revoked, c.route)
					if c.body["reason_code"] != "garden_invite_failed" || c.body["realm_id"] != "cairn" {
						t.Fatalf("revocation body %v", c.body)
					}
				}
			}
			if strings.Join(revoked, ",") != strings.Join(tc.revoked, ",") {
				t.Fatalf("revocations %v, want %v", revoked, tc.revoked)
			}
			if len(fx.invRecords()) != 0 {
				t.Fatal("failed invite left its record")
			}
			if len(*fx.reloads) != 0 {
				t.Fatal("failed invite reloaded the gateway")
			}
		})
	}
}

func TestInviteCommandRemovesRecordAndRevokesWhenConfigCannotBeWritten(t *testing.T) {
	if os.Geteuid() == 0 {
		t.Skip("root ignores directory permissions")
	}
	fx := newInvFixture(t)
	etc := filepath.Dir(fx.configPath)
	// The operation lock file exists already, as it does after the first invite on a host.
	if err := os.WriteFile(filepath.Join(etc, operationLockFile), nil, 0600); err != nil {
		t.Fatal(err)
	}
	if err := os.Chmod(etc, 0500); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = os.Chmod(etc, 0750) })
	before := fx.configBytes()

	_, _, err := runInvCommand(fx.args("zen"))
	if err == nil {
		t.Fatal("an unwritable configuration was reported as success")
	}
	if !strings.Contains(err.Error(), "credential "+fx.fake.credential) || !strings.Contains(err.Error(), "grant "+fx.fake.grant) {
		t.Fatalf("failure omits the grant or credential: %v", err)
	}
	calls, _ := fx.fake.snapshot()
	var revoked []string
	for _, c := range calls {
		if strings.HasPrefix(c.route, "/v1/revoke-") {
			revoked = append(revoked, c.route)
		}
	}
	if strings.Join(revoked, ",") != "/v1/revoke-credential,/v1/revoke-grant" {
		t.Fatalf("revocations %v", revoked)
	}
	if len(fx.invRecords()) != 0 {
		t.Fatal("record kept after the configuration could not be written")
	}
	if string(fx.configBytes()) != string(before) {
		t.Fatal("configuration changed")
	}
}

// Concurrent invites must not lose each other's principal (review finding, 8 November 2026).
func TestConcurrentGatewayUpdatesKeepEveryPrincipal(t *testing.T) {
	fx := newInvFixture(t)
	names := []string{"ann", "ben", "cat", "dan", "eve", "fay"}
	ids := map[string]string{}
	for _, n := range names {
		ids[n] = uuid.NewString()
	}
	var wg sync.WaitGroup
	errs := make(chan error, len(names))
	for _, n := range names {
		wg.Add(1)
		go func(n string) {
			defer wg.Done()
			errs <- addGatewayPrincipal(fx.configPath, ids[n], n)
		}(n)
	}
	wg.Wait()
	close(errs)
	for err := range errs {
		if err != nil {
			t.Fatal(err)
		}
	}
	cfg, err := garden.LoadConfig(fx.configPath)
	if err != nil {
		t.Fatal(err)
	}
	for _, n := range names {
		if cfg.Principals[ids[n]] != n {
			t.Fatalf("principal for %s lost: %v", n, cfg.Principals)
		}
	}
}

// seedEarlier records an earlier issuance for the fixture's mapped identity, as a previous invite
// would have, and returns its entry.
func (fx *invFixture) seedEarlier(t *testing.T) invite.LedgerEntry {
	t.Helper()
	entry := invite.NewLedgerEntry("kay", fx.principalID(), uuid.NewString(), uuid.NewString(), time.Now().Add(-time.Hour), time.Now().Add(365*24*time.Hour))
	if err := invite.AppendLedger(fx.ledgerPath(), entry); err != nil {
		t.Fatal(err)
	}
	return entry
}

// reissueFixture points the fake at the mapped identity, as reissue names it.
func reissueFixture(t *testing.T) (*invFixture, invite.LedgerEntry) {
	t.Helper()
	fx := newInvFixture(t)
	fx.fake.principal = fx.principalID()
	return fx, fx.seedEarlier(t)
}

func invRevocations(calls []invCall) []invCall {
	var out []invCall
	for _, c := range calls {
		if strings.HasPrefix(c.route, "/v1/revoke-") {
			out = append(out, c)
		}
	}
	return out
}

func TestInviteCommandWritesLedgerLineWithoutSecrets(t *testing.T) {
	fx := newInvFixture(t)
	stdout, _, err := runInvCommand(fx.args("zen"))
	if err != nil {
		t.Fatal(err)
	}
	entries, err := invite.ReadLedger(fx.ledgerPath())
	if err != nil {
		t.Fatal(err)
	}
	if len(entries) != 1 {
		t.Fatalf("ledger entries %d, want 1", len(entries))
	}
	e := entries[0]
	if e.Name != "zen" || e.PrincipalID != fx.fake.principal || e.GrantID != fx.fake.grant || e.CredentialID != fx.fake.credential || !e.CredentialPending() || !e.GrantPending() {
		t.Fatalf("ledger entry %+v", e)
	}
	raw, err := os.ReadFile(fx.ledgerPath())
	if err != nil {
		t.Fatal(err)
	}
	info, err := os.Stat(fx.ledgerPath())
	if err != nil {
		t.Fatal(err)
	}
	if info.Mode().Perm() != 0600 {
		t.Fatalf("ledger mode %v, want 0600", info.Mode().Perm())
	}
	allowed := map[string]bool{"name": true, "principal_id": true, "grant_id": true, "credential_id": true, "issued_at": true, "expires_at": true, "revoked_at": true}
	var line map[string]any
	if err := json.Unmarshal(raw, &line); err != nil {
		t.Fatal(err)
	}
	for key := range line {
		if !allowed[key] {
			t.Fatalf("ledger holds unexpected field %q", key)
		}
	}
	code, err := invite.Decode(strings.TrimSuffix(stdout, "\n"))
	if err != nil {
		t.Fatal(err)
	}
	if strings.Contains(string(raw), invTestAdminToken) || strings.Contains(string(raw), fx.fake.plaintext) || strings.Contains(string(raw), "cairn1.") || strings.Contains(string(raw), string(code.Secret)) {
		t.Fatal("ledger holds a token or secret")
	}
}

func TestInviteCommandRefusesReissueForUnmappedName(t *testing.T) {
	fx := newInvFixture(t)
	before := fx.configBytes()
	_, _, err := runInvCommand(fx.args("zen", "--reissue"))
	if err == nil || !strings.Contains(err.Error(), "not mapped") {
		t.Fatalf("reissue of an unmapped name: %v", err)
	}
	if calls, diagnoses := fx.fake.snapshot(); len(calls) != 0 || diagnoses != 0 {
		t.Fatalf("refused reissue called Cairn: %d calls", len(calls))
	}
	if string(fx.configBytes()) != string(before) || len(*fx.reloads) != 0 {
		t.Fatal("refused reissue changed the configuration or reloaded")
	}
}

func TestInviteCommandRefusesMappedNameWithoutReissueHint(t *testing.T) {
	fx := newInvFixture(t)
	_, _, err := runInvCommand(fx.args("kay"))
	if err == nil || !strings.Contains(err.Error(), "--reissue") {
		t.Fatalf("fresh invite of a mapped name: %v", err)
	}
	if calls, _ := fx.fake.snapshot(); len(calls) != 0 {
		t.Fatalf("refused invite called Cairn: %d calls", len(calls))
	}
}

func TestInviteCommandReissueRevokesOnlyRecordedCredentials(t *testing.T) {
	fx, old := reissueFixture(t)
	before := fx.configBytes()

	stdout, stderr, err := runInvCommand(fx.args("kay", "--reissue"))
	if err != nil {
		t.Fatalf("reissue failed: %v\n%s", err, stderr)
	}
	calls, _ := fx.fake.snapshot()
	var routes []string
	for _, c := range calls {
		routes = append(routes, c.route)
	}
	if strings.Join(routes, ",") != "/v1/create-grant,/v1/issue-credential,/v1/revoke-credential,/v1/revoke-grant" {
		t.Fatalf("admin routes %v", routes)
	}
	revs := invRevocations(calls)
	if revs[0].body["credential_id"] != old.CredentialID || revs[1].body["grant_id"] != old.GrantID {
		t.Fatalf("revoked %v, want the earlier credential and grant", revs)
	}
	for _, r := range revs {
		if r.body["reason_code"] != "garden_invite_reissued" || r.idem == "" {
			t.Fatalf("revocation %v lacks the reason or key", r.body)
		}
	}
	if string(fx.configBytes()) != string(before) || len(*fx.reloads) != 0 {
		t.Fatal("reissue changed the gateway configuration or reloaded")
	}
	if _, err := invite.Decode(strings.TrimSuffix(stdout, "\n")); err != nil {
		t.Fatalf("code missing: %v", err)
	}
	entries, err := invite.ReadLedger(fx.ledgerPath())
	if err != nil {
		t.Fatal(err)
	}
	if len(entries) != 2 || entries[0].CredentialPending() || entries[0].GrantPending() || entries[1].CredentialID != fx.fake.credential || !entries[1].CredentialPending() || !entries[1].GrantPending() {
		t.Fatalf("ledger after reissue: %+v", entries)
	}
	for _, want := range []string{"sessions using the old token stop now", "identities/kay.token", "identities/kay.sealed", "revoked credential " + old.CredentialID} {
		if !strings.Contains(stderr, want) {
			t.Errorf("stderr lacks %q:\n%s", want, stderr)
		}
	}
}

func TestInviteCommandReissueDoesNotRevokeTwice(t *testing.T) {
	fx, old := reissueFixture(t)
	if _, _, err := runInvCommand(fx.args("kay", "--reissue")); err != nil {
		t.Fatal(err)
	}
	firstNew := fx.fake.credential
	if _, _, err := runInvCommand(fx.args("kay", "--reissue")); err != nil {
		t.Fatal(err)
	}
	calls, _ := fx.fake.snapshot()
	count := map[string]int{}
	for _, r := range invRevocations(calls) {
		if id, ok := r.body["credential_id"].(string); ok {
			count[id]++
		}
	}
	if count[old.CredentialID] != 1 {
		t.Fatalf("earlier credential revoked %d times, want 1", count[old.CredentialID])
	}
	if count[firstNew] != 1 {
		t.Fatalf("first reissue's credential revoked %d times, want 1 (by the second reissue)", count[firstNew])
	}
	entries, err := invite.ReadLedger(fx.ledgerPath())
	if err != nil {
		t.Fatal(err)
	}
	for _, e := range entries {
		if e.CredentialID != fx.fake.credential && (e.CredentialPending() || e.GrantPending()) {
			t.Fatalf("entry %s not marked revoked", e.CredentialID)
		}
	}
}

func TestInviteCommandReissueWithoutLedgerWarnsAndSucceeds(t *testing.T) {
	fx := newInvFixture(t)
	fx.fake.principal = fx.principalID()
	stdout, stderr, err := runInvCommand(fx.args("kay", "--reissue"))
	if err != nil {
		t.Fatalf("reissue without ledger entries must succeed: %v", err)
	}
	if !strings.Contains(stderr, "no earlier credentials are recorded") || !strings.Contains(stderr, "revoked by hand") {
		t.Fatalf("no warning about unrecorded credentials:\n%s", stderr)
	}
	if len(invRevocations(invCallsOf(t, fx))) != 0 {
		t.Fatal("reissue without ledger entries revoked something")
	}
	if _, err := invite.Decode(strings.TrimSuffix(stdout, "\n")); err != nil {
		t.Fatalf("code missing: %v", err)
	}
}

func invCallsOf(t *testing.T, fx *invFixture) []invCall {
	t.Helper()
	c, _ := fx.fake.snapshot()
	return c
}

func TestInviteCommandReissueReportsLiveIDsAndExitsNonZero(t *testing.T) {
	fx, old := reissueFixture(t)
	fx.fake.failRoute = "/v1/revoke-grant"
	fx.fake.failMode = "http"
	stdout, _, err := runInvCommand(fx.args("kay", "--reissue"))
	if err == nil || !strings.Contains(err.Error(), "still live") || !strings.Contains(err.Error(), "grant "+old.GrantID) {
		t.Fatalf("failed revocation: %v", err)
	}
	if _, err := invite.Decode(strings.TrimSuffix(stdout, "\n")); err != nil {
		t.Fatalf("code not printed after a failed revocation: %v", err)
	}
	if len(fx.invRecords()) != 1 {
		t.Fatal("the new invite was removed after a failed revocation")
	}
	entries, err := invite.ReadLedger(fx.ledgerPath())
	if err != nil {
		t.Fatal(err)
	}
	if entries[0].CredentialPending() || !entries[0].GrantPending() {
		t.Fatalf("grant failed: want credential recorded and grant pending, got %+v", entries[0])
	}
}

func TestInviteCommandReissueFailureBeforeRecordRevokesOnlyNewIDs(t *testing.T) {
	fx, old := reissueFixture(t)
	before := fx.configBytes()
	fx.fake.ingestOff = true
	_, _, err := runInvCommand(fx.args("kay", "--reissue"))
	if err == nil {
		t.Fatal("a reissue whose credential cannot ingest was accepted")
	}
	revs := invRevocations(invCallsOf(t, fx))
	if len(revs) != 2 {
		t.Fatalf("revocations %d, want the new credential and grant", len(revs))
	}
	for _, r := range revs {
		for _, id := range []any{r.body["credential_id"], r.body["grant_id"]} {
			if id == old.CredentialID || id == old.GrantID {
				t.Fatal("an earlier credential or grant was revoked by a failed reissue")
			}
		}
	}
	if len(fx.invRecords()) != 0 {
		t.Fatal("failed reissue left a record")
	}
	entries, err := invite.ReadLedger(fx.ledgerPath())
	if err != nil {
		t.Fatal(err)
	}
	if len(entries) != 1 || !entries[0].CredentialPending() || !entries[0].GrantPending() {
		t.Fatalf("ledger changed by a failed reissue: %+v", entries)
	}
	if string(fx.configBytes()) != string(before) || len(*fx.reloads) != 0 {
		t.Fatal("failed reissue changed the configuration or reloaded")
	}
}

// ledgerPath is the ledger beside the gateway configuration, where it must live.
func (fx *invFixture) ledgerPath() string {
	return filepath.Join(filepath.Dir(fx.configPath), invite.LedgerFile)
}

// writeLedgerLines replaces the ledger with the given raw lines, as an operator might seed it.
func (fx *invFixture) writeLedgerLines(t *testing.T, lines ...string) {
	t.Helper()
	writeInvFile(t, fx.ledgerPath(), []byte(strings.Join(lines, "\n")+"\n"), 0600)
}

func TestInviteCommandPartialRevocationRetriesOnlyTheFailedHalf(t *testing.T) {
	fx, old := reissueFixture(t)
	fx.fake.failRoute = "/v1/revoke-grant"
	fx.fake.failMode = "http"
	_, _, err := runInvCommand(fx.args("kay", "--reissue"))
	if err == nil || !strings.Contains(err.Error(), "still live") || !strings.Contains(err.Error(), "grant "+old.GrantID) {
		t.Fatalf("grant failure not reported: %v", err)
	}
	entries, err := invite.ReadLedger(fx.ledgerPath())
	if err != nil {
		t.Fatal(err)
	}
	if entries[0].CredentialPending() || !entries[0].GrantPending() {
		t.Fatalf("after a grant failure the credential must be marked and the grant pending: %+v", entries[0])
	}

	// The retry sends only the grant; the credential is not sent again.
	fx.fake.failRoute = ""
	fx.fake.failMode = ""
	if _, _, err := runInvCommand(fx.args("kay", "--reissue")); err != nil {
		t.Fatalf("retry failed: %v", err)
	}
	calls, _ := fx.fake.snapshot()
	credSent, grantSent := 0, 0
	for _, r := range invRevocations(calls) {
		if r.body["credential_id"] == old.CredentialID {
			credSent++
		}
		if r.body["grant_id"] == old.GrantID {
			grantSent++
		}
	}
	if credSent != 1 || grantSent != 2 {
		t.Fatalf("old credential sent %d times (want 1), old grant sent %d times (want 2: failed, then retry)", credSent, grantSent)
	}
	entries, err = invite.ReadLedger(fx.ledgerPath())
	if err != nil {
		t.Fatal(err)
	}
	if entries[0].CredentialPending() || entries[0].GrantPending() {
		t.Fatalf("old entry not fully marked after the retry: %+v", entries[0])
	}
}

func TestInviteCommandMarkFailureExitsNonZeroNamingTheID(t *testing.T) {
	fx, old := reissueFixture(t)
	orig := ledgerMark
	ledgerMark = func(string, string, string, time.Time) (bool, error) { return false, errors.New("disk full") }
	t.Cleanup(func() { ledgerMark = orig })

	stdout, _, err := runInvCommand(fx.args("kay", "--reissue"))
	if err == nil || !strings.Contains(err.Error(), "revoked in Cairn but unrecorded") || !strings.Contains(err.Error(), "credential "+old.CredentialID) || !strings.Contains(err.Error(), "credential_revoked_at") {
		t.Fatalf("mark failure not reported with the ID and field: %v", err)
	}
	if _, err := invite.Decode(strings.TrimSuffix(stdout, "\n")); err != nil {
		t.Fatalf("code not printed after a mark failure: %v", err)
	}
	calls, _ := fx.fake.snapshot()
	if len(invRevocations(calls)) != 2 {
		t.Fatalf("revocations %d; the unmarked credential must not be sent twice in one run", len(invRevocations(calls)))
	}
}

func TestInviteCommandMalformedLedgerRefusesBeforeAnyAdminCall(t *testing.T) {
	fx := newInvFixture(t)
	fx.writeLedgerLines(t, `{"name":"kay","principal_id":"nope"`)
	before := fx.configBytes()
	_, _, err := runInvCommand(fx.args("zen"))
	if err == nil || !strings.Contains(err.Error(), "refusing to issue") || !strings.Contains(err.Error(), "malformed") {
		t.Fatalf("malformed ledger accepted or unclear: %v", err)
	}
	if calls, diagnoses := fx.fake.snapshot(); len(calls) != 0 || diagnoses != 0 {
		t.Fatalf("malformed ledger still called Cairn: %d calls", len(calls))
	}
	if string(fx.configBytes()) != string(before) || len(fx.invRecords()) != 0 {
		t.Fatal("malformed ledger changed state")
	}
}

func TestInviteCommandHonoursLegacyRevokedAtLines(t *testing.T) {
	fx := newInvFixture(t)
	fx.fake.principal = fx.principalID()
	legacy := `{"name":"kay","principal_id":"` + fx.principalID() + `","grant_id":"` + uuid.NewString() +
		`","credential_id":"` + uuid.NewString() + `","issued_at":"2026-11-01T10:00:00Z","expires_at":"2027-11-01T10:00:00Z","revoked_at":"2026-11-02T10:00:00Z"}`
	fx.writeLedgerLines(t, legacy)
	if _, _, err := runInvCommand(fx.args("kay", "--reissue")); err != nil {
		t.Fatal(err)
	}
	calls, _ := fx.fake.snapshot()
	if len(invRevocations(calls)) != 0 {
		t.Fatalf("a line with revoked_at was revoked again: %v", invRevocations(calls))
	}
	entries, err := invite.ReadLedger(fx.ledgerPath())
	if err != nil {
		t.Fatal(err)
	}
	if entries[0].CredentialPending() || entries[0].GrantPending() {
		t.Fatalf("legacy revoked_at entry is pending: %+v", entries[0])
	}
}

func TestInviteCommandCLIWithoutTerminalIsRefusedBeforeAnyAdminCall(t *testing.T) {
	fx := newInvFixture(t)
	fx.passphraseErr = errors.New("a sealed Garden identity needs a terminal to ask for its passphrase")
	before := fx.configBytes()
	_, _, err := runInvCommand(fx.args("zen", "--harness", "cli"))
	if err == nil || !strings.Contains(err.Error(), "ssh -t") {
		t.Fatalf("cli invite without a terminal: %v", err)
	}
	if calls, diagnoses := fx.fake.snapshot(); len(calls) != 0 || diagnoses != 0 {
		t.Fatalf("refused cli invite called Cairn: %d calls", len(calls))
	}
	if len(fx.invRecords()) != 0 || string(fx.configBytes()) != string(before) || len(*fx.reloads) != 0 {
		t.Fatal("refused cli invite changed state")
	}
}

func TestInviteCommandCLIMismatchedConfirmationIsRefusedBeforeAnyAdminCall(t *testing.T) {
	fx := newInvFixture(t)
	fx.passphraseErr = errors.New("the passphrases do not match")
	_, _, err := runInvCommand(fx.args("zen", "--harness", "cli"))
	if err == nil || !strings.Contains(err.Error(), "do not match") {
		t.Fatalf("mismatched confirmation: %v", err)
	}
	if calls, _ := fx.fake.snapshot(); len(calls) != 0 {
		t.Fatalf("mismatched confirmation reached Cairn: %d calls", len(calls))
	}
	if len(fx.prompts) != 1 || !fx.confirms[0] {
		t.Fatalf("the passphrase must be asked once, with confirmation: prompts %v confirms %v", fx.prompts, fx.confirms)
	}
}

// assertCLIRecord checks that the newest record holds only a sealed identity that opens with the
// passphrase to the token Cairn issued, and that no plaintext token reaches the directory.
func assertCLIRecord(t *testing.T, fx *invFixture, code invite.Code, name string) {
	t.Helper()
	var record invite.Record
	found := false
	for _, file := range fx.invRecords() {
		data, err := os.ReadFile(filepath.Join(fx.invitesDir, file))
		if err != nil {
			t.Fatal(err)
		}
		if strings.Contains(string(data), "cairn1.") || strings.Contains(string(data), fx.fake.plaintext) {
			t.Fatal("an invite record holds a plaintext credential")
		}
		var r invite.Record
		if err := json.Unmarshal(data, &r); err != nil {
			t.Fatal(err)
		}
		if r.Name == name && r.Payload == invite.PayloadSealedIdentity {
			record, found = r, true
		}
	}
	if !found {
		t.Fatalf("no sealed cli record for %s", name)
	}
	if record.Harness != "cli" {
		t.Fatalf("harness %q", record.Harness)
	}
	sealed, err := record.Open(code.Secret, time.Now())
	if err != nil {
		t.Fatalf("record does not open with the code's secret: %v", err)
	}
	token, err := identityseal.Open(name, []byte(sealed), []byte(string(fx.passphrase)))
	if err != nil || token != fx.fake.plaintext {
		t.Fatalf("sealed identity opens to %q, %v; want the issued token", token, err)
	}
	if _, err := identityseal.Open(name, []byte(sealed), []byte("wrong passphrase here")); err == nil {
		t.Fatal("the sealed identity opened with the wrong passphrase")
	}
}

func TestInviteCommandCLIStoresSealedIdentityOnly(t *testing.T) {
	fx := newInvFixture(t)
	fx.passphraseErr = nil
	stdout, stderr, err := runInvCommand(fx.args("zen", "--harness", "cli"))
	if err != nil {
		t.Fatalf("cli invite failed: %v\n%s", err, stderr)
	}
	if len(fx.prompts) != 1 || fx.prompts[0] != "passphrase for zen: " || !fx.confirms[0] {
		t.Fatalf("prompts %v confirms %v", fx.prompts, fx.confirms)
	}
	code, err := invite.Decode(strings.TrimSuffix(stdout, "\n"))
	if err != nil {
		t.Fatal(err)
	}
	assertCLIRecord(t, fx, code, "zen")
	if !strings.Contains(stderr, "passphrase") || !strings.Contains(stderr, "only the passphrase-sealed key") {
		t.Fatalf("stderr does not explain the sealed key:\n%s", stderr)
	}
	if strings.Contains(stdout+stderr, "cairn1.") || strings.Contains(stdout+stderr, fx.fake.plaintext) {
		t.Fatal("output carries a plaintext credential")
	}
}

func TestInviteCommandCLIReissueSealsToo(t *testing.T) {
	fx, old := reissueFixture(t)
	fx.passphraseErr = nil
	stdout, stderr, err := runInvCommand(fx.args("kay", "--reissue", "--harness", "cli"))
	if err != nil {
		t.Fatalf("cli reissue failed: %v\n%s", err, stderr)
	}
	if len(fx.prompts) != 1 || fx.prompts[0] != "passphrase for kay: " {
		t.Fatalf("prompts %v", fx.prompts)
	}
	code, err := invite.Decode(strings.TrimSuffix(stdout, "\n"))
	if err != nil {
		t.Fatal(err)
	}
	assertCLIRecord(t, fx, code, "kay")
	calls, _ := fx.fake.snapshot()
	revoked := false
	for _, r := range invRevocations(calls) {
		if r.body["credential_id"] == old.CredentialID {
			revoked = true
		}
	}
	if !revoked {
		t.Fatal("cli reissue did not revoke the earlier credential")
	}
}

// Overlapping invite runs are refused, so two re-issues cannot both revoke the same earlier
// credential and leave both replacements live (Val's review, 8 November 2026).
func TestInviteRefusesWhileAnotherInviteRuns(t *testing.T) {
	fx := newInvFixture(t)
	release, err := lockOperation(filepath.Dir(fx.configPath))
	if err != nil {
		t.Fatal(err)
	}
	before := fx.configBytes()
	_, _, err = runInvCommand(fx.args("zen"))
	if err == nil || !strings.Contains(err.Error(), "another garden invite is running") {
		t.Fatalf("overlapping invite: %v", err)
	}
	if calls, _ := fx.fake.snapshot(); len(calls) != 0 {
		t.Fatalf("an overlapping invite made %d admin calls", len(calls))
	}
	if !bytes.Equal(fx.configBytes(), before) {
		t.Fatal("an overlapping invite changed the gateway configuration")
	}
	release()
	if _, _, err := runInvCommand(fx.args("zen")); err != nil {
		t.Fatalf("invite after the lock was released: %v", err)
	}
}
