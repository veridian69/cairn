package invite

import (
	"context"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/google/uuid"
	"github.com/veridian69/cairn/garden/internal/gardenauth"
)

const adminTestToken = "cairn1.admin_TOKEN-1.part"

func TestAdminCheckNameAcceptsOnlyLabelSafeNames(t *testing.T) {
	for _, name := range []string{"zen", "a", "reference", "my-agent"} {
		if label, err := CheckName(name); err != nil || label != name+"-garden" {
			t.Errorf("CheckName(%q) = %q, %v", name, label, err)
		}
	}
	for _, name := range []string{"Zen", "zen_", "-zen", "zen.one", "1zen", strings.Repeat("a", 60), "", "zen/x"} {
		if _, err := CheckName(name); err == nil {
			t.Errorf("CheckName(%q) accepted a name that does not make a Cairn label", name)
		}
	}
}

func TestAdminCairnBaseURL(t *testing.T) {
	good := map[[2]string]string{
		{"", "http://127.0.0.1:8100/memory/v1/diagnose"}:          "http://127.0.0.1:8100",
		{"http://[::1]:8100/", ""}:                                "http://[::1]:8100",
		{"https://cairn.example.net", ""}:                         "https://cairn.example.net",
		{"", "https://cairn.example.net:8443/memory/v1/diagnose"}: "https://cairn.example.net:8443",
	}
	for in, want := range good {
		got, err := CairnBaseURL(in[0], in[1])
		if err != nil || got != want {
			t.Errorf("CairnBaseURL(%q, %q) = %q, %v; want %q", in[0], in[1], got, err, want)
		}
	}
	bad := [][2]string{
		{"http://cairn.example.net:8100", ""},
		{"http://localhost:8100", ""},
		{"http://10.0.0.5:8100", ""},
		{"ftp://127.0.0.1", ""},
		{"https://cairn.example.net/v1", ""},
		{"https://user:pass@cairn.example.net", ""},
		{"https://cairn.example.net?x=1", ""},
		{"", "http://cairn.example.net/memory/v1/diagnose"},
		{"", ""},
	}
	for _, in := range bad {
		if got, err := CairnBaseURL(in[0], in[1]); err == nil {
			t.Errorf("CairnBaseURL(%q, %q) = %q, want refusal", in[0], in[1], got)
		}
	}
}

func TestAdminLoadTokenFile(t *testing.T) {
	dir := t.TempDir()
	write := func(name, content string, mode os.FileMode) string {
		t.Helper()
		path := filepath.Join(dir, name)
		if err := os.WriteFile(path, []byte(content), mode); err != nil {
			t.Fatal(err)
		}
		if err := os.Chmod(path, mode); err != nil {
			t.Fatal(err)
		}
		return path
	}
	got, err := LoadAdminToken(write("with-newline", adminTestToken+"\n", 0600))
	if err != nil || got != adminTestToken {
		t.Fatalf("valid token refused: %q, %v", got, err)
	}
	if got, err := LoadAdminToken(write("bare", adminTestToken, 0600)); err != nil || got != adminTestToken {
		t.Fatalf("valid token without newline refused: %q, %v", got, err)
	}
	refused := map[string]string{
		"mode-0640": write("mode-0640", adminTestToken, 0640),
		"mode-0400": write("mode-0400", adminTestToken, 0400),
		"two-lines": write("two-lines", adminTestToken+"\nextra", 0600),
		"no-prefix": write("no-prefix", "sk-abc", 0600),
		"space":     write("space", "cairn1.abc def", 0600),
		"crlf":      write("crlf", adminTestToken+"\r\n", 0600),
		"empty":     write("empty", "", 0600),
		"oversized": write("oversized", "cairn1."+strings.Repeat("a", 16*1024), 0600),
		"missing":   filepath.Join(dir, "missing"),
		"directory": dir,
	}
	target := write("target", adminTestToken, 0600)
	if err := os.Symlink(target, filepath.Join(dir, "symlink")); err != nil {
		t.Fatal(err)
	}
	refused["symlink"] = filepath.Join(dir, "symlink")
	for name, path := range refused {
		if got, err := LoadAdminToken(path); err == nil {
			t.Errorf("%s: accepted %q", name, got)
		}
	}
}

// newAdminFake returns an admin API that answers committed receipts and records each request.
type adminRequest struct {
	route string
	auth  string
	idem  string
	ctype string
	body  map[string]any
}

func newAdminFake(t *testing.T, handler http.HandlerFunc) (*httptest.Server, *[]adminRequest) {
	t.Helper()
	var seen []adminRequest
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		raw, _ := io.ReadAll(r.Body)
		var body map[string]any
		_ = json.Unmarshal(raw, &body)
		seen = append(seen, adminRequest{route: r.URL.Path, auth: r.Header.Get("Authorization"), idem: r.Header.Get("Idempotency-Key"), ctype: r.Header.Get("Content-Type"), body: body})
		handler(w, r)
	}))
	t.Cleanup(srv.Close)
	return srv, &seen
}

func committed(w http.ResponseWriter, result string) {
	w.Header().Set("Content-Type", "application/json")
	_, _ = io.WriteString(w, `{"outcome":"committed","result":`+result+`}`)
}

func TestAdminClientSendsAuthorisedKeyedRequests(t *testing.T) {
	principal := uuid.NewString()
	srv, seen := newAdminFake(t, func(w http.ResponseWriter, r *http.Request) {
		committed(w, `{"principal_id":"`+principal+`"}`)
	})
	client, err := NewAdminClient(srv.URL, adminTestToken)
	if err != nil {
		t.Fatal(err)
	}
	got, err := client.CreatePrincipal(context.Background(), "cairn", "zen-garden")
	if err != nil || got != principal {
		t.Fatalf("CreatePrincipal = %q, %v", got, err)
	}
	if _, err := client.CreatePrincipal(context.Background(), "cairn", "zen-garden"); err != nil {
		t.Fatal(err)
	}
	if len(*seen) != 2 {
		t.Fatalf("%d requests, want 2", len(*seen))
	}
	first := (*seen)[0]
	if first.route != "/v1/create-principal" || first.auth != "Bearer "+adminTestToken || first.ctype != "application/json" {
		t.Fatalf("request headers or route wrong: %+v", first)
	}
	if first.body["realm_id"] != "cairn" || first.body["kind"] != "workload" || first.body["label"] != "zen-garden" || len(first.body) != 3 {
		t.Fatalf("request body wrong: %v", first.body)
	}
	if parsed, err := uuid.Parse(first.idem); err != nil || parsed.Version() != 4 {
		t.Fatalf("Idempotency-Key %q is not a UUID version 4", first.idem)
	}
	if (*seen)[1].idem == first.idem {
		t.Fatal("two calls reused an Idempotency-Key")
	}
}

func TestAdminClientRefusesUncommittedAndOversizedReceipts(t *testing.T) {
	cases := map[string]http.HandlerFunc{
		"http-500": func(w http.ResponseWriter, r *http.Request) {
			w.WriteHeader(http.StatusInternalServerError)
			_, _ = io.WriteString(w, `{"secret":"SECRET-BODY"}`)
		},
		"redirect": func(w http.ResponseWriter, r *http.Request) {
			http.Redirect(w, r, "/elsewhere", http.StatusFound)
		},
		"rejected": func(w http.ResponseWriter, r *http.Request) {
			w.Header().Set("Content-Type", "application/json")
			_, _ = io.WriteString(w, `{"outcome":"rejected","result":{"principal_id":"`+uuid.NewString()+`"}}`)
		},
		"oversized": func(w http.ResponseWriter, r *http.Request) {
			w.Header().Set("Content-Type", "application/json")
			_, _ = io.WriteString(w, `{"outcome":"committed","result":{"principal_id":"`+uuid.NewString()+`","pad":"`+strings.Repeat("x", 1<<20)+`"}}`)
		},
		"no-principal": func(w http.ResponseWriter, r *http.Request) {
			committed(w, `{"principal_id":"not-a-uuid"}`)
		},
	}
	for name, handler := range cases {
		srv, _ := newAdminFake(t, handler)
		client, err := NewAdminClient(srv.URL, adminTestToken)
		if err != nil {
			t.Fatal(err)
		}
		_, err = client.CreatePrincipal(context.Background(), "cairn", "zen-garden")
		if err == nil {
			t.Errorf("%s: a refused receipt was accepted", name)
		}
		if err != nil && strings.Contains(err.Error(), "SECRET-BODY") {
			t.Errorf("%s: the error quotes the response body", name)
		}
	}
}

func TestAdminIssueCredentialChecksExpiryAndKeepsIDForRevocation(t *testing.T) {
	credential := uuid.NewString()
	expires := time.Date(2027, 11, 8, 12, 0, 0, 0, time.UTC)
	srv, _ := newAdminFake(t, func(w http.ResponseWriter, r *http.Request) {
		committed(w, `{"credential_id":"`+credential+`","plaintext":"`+adminTestToken+`","expires_at":"2027-11-09T12:00:00.000000Z"}`)
	})
	client, err := NewAdminClient(srv.URL, adminTestToken)
	if err != nil {
		t.Fatal(err)
	}
	cred, err := client.IssueCredential(context.Background(), "cairn", uuid.NewString(), expires)
	if err == nil {
		t.Fatal("a credential with a different expiry was accepted")
	}
	if cred.ID != credential {
		t.Fatalf("mismatched credential lost its ID: %q", cred.ID)
	}
}

func TestAdminIssueCredentialAcceptsCanonicalReceipt(t *testing.T) {
	credential := uuid.NewString()
	expires := time.Date(2027, 11, 8, 12, 0, 0, 0, time.UTC)
	srv, seen := newAdminFake(t, func(w http.ResponseWriter, r *http.Request) {
		committed(w, `{"credential_id":"`+credential+`","plaintext":"`+adminTestToken+`","expires_at":"2027-11-08T12:00:00.000000Z"}`)
	})
	client, err := NewAdminClient(srv.URL, adminTestToken)
	if err != nil {
		t.Fatal(err)
	}
	principal := uuid.NewString()
	cred, err := client.IssueCredential(context.Background(), "cairn", principal, expires)
	if err != nil || cred.ID != credential || cred.Plaintext != adminTestToken {
		t.Fatalf("IssueCredential = %+v, %v", cred, err)
	}
	if body := (*seen)[0].body; body["expires_at"] != "2027-11-08T12:00:00Z" || body["principal_id"] != principal {
		t.Fatalf("issue body wrong: %v", body)
	}
}

func TestAdminCreateGrantBody(t *testing.T) {
	grant := uuid.NewString()
	srv, seen := newAdminFake(t, func(w http.ResponseWriter, r *http.Request) {
		committed(w, `{"grant_id":"`+grant+`"}`)
	})
	client, err := NewAdminClient(srv.URL, adminTestToken)
	if err != nil {
		t.Fatal(err)
	}
	spec := GrantSpec{
		PrincipalID:    uuid.NewString(),
		Realm:          "cairn",
		Segments:       []gardenauth.Segment{{Kind: "garden", Identifier: "agents"}},
		Classification: "internal",
		ExpiresAt:      time.Date(2027, 11, 8, 12, 0, 0, 0, time.UTC),
	}
	got, err := client.CreateGrant(context.Background(), spec)
	if err != nil || got != grant {
		t.Fatalf("CreateGrant = %q, %v", got, err)
	}
	body := (*seen)[0].body
	inner, _ := body["grant"].(map[string]any)
	if body["realm_id"] != "cairn" || inner["principal_id"] != spec.PrincipalID || inner["realm_id"] != "cairn" ||
		inner["read_clearance"] != "internal" || inner["expires_at"] != "2027-11-08T12:00:00Z" {
		t.Fatalf("grant body wrong: %v", body)
	}
	ops, _ := inner["operations"].([]any)
	writes, _ := inner["write_classifications"].([]any)
	segments, _ := inner["segments"].([]any)
	if len(ops) != 2 || ops[0] != "retrieve" || ops[1] != "ingest" || len(writes) != 1 || writes[0] != "internal" || len(segments) != 1 {
		t.Fatalf("grant operations, classifications or segments wrong: %v", inner)
	}
}

func TestReadAdminTokenFromStandardInput(t *testing.T) {
	token, err := readAdminToken(strings.NewReader("cairn1.abc_DEF-1.x\n"))
	if err != nil || token != "cairn1.abc_DEF-1.x" {
		t.Fatalf("readAdminToken = %q, %v", token, err)
	}
	for _, bad := range []string{"", "token\n", "cairn1.a b\n", "cairn1.a\n\n", strings.Repeat("x", 20000)} {
		if _, err := readAdminToken(strings.NewReader(bad)); err == nil {
			t.Errorf("readAdminToken accepted %.20q", bad)
		}
	}
}
