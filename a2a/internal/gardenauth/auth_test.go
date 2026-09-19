package gardenauth_test

import (
	"compress/gzip"
	"context"
	"encoding/json"
	"errors"
	"io"
	"log"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"github.com/veridian69/cairn/a2a/internal/gardenauth"
)

const instance = "84995229-fc82-4ed7-a03d-cdff053b025b"
const principal = "d1dc7c3d-3808-49b6-8334-a632b958f458"
const token = "synthetic-credential-never-echo-this"

// This fixture follows Cairn's actual flat REST DiagnoseBody, including fields
// its Python client requires. MCP's structuredContent envelope is not REST.
const diagnostic = `{"instance_id":"84995229-fc82-4ed7-a03d-cdff053b025b","product_version":"0.1.0","contract_identity":"cairn.memory/v1","contract_digest":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","mcp_contract_digest":"bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb","principal_id":"d1dc7c3d-3808-49b6-8334-a632b958f458","principal_kind":"workload","scope":{"realm":"garden","segments":[{"kind":"job","identifier":"chat-1"}]},"classification":"internal","permissions":{"retrieve":true,"ingest":true,"promote":false,"invalidate":false},"evaluated_at":"2026-09-17T01:02:03.123456Z","permission_basis":"current_grants_only"}`

func config(endpoint string) gardenauth.Config {
	return gardenauth.Config{Endpoint: endpoint + "/memory/v1/diagnose", InstanceID: instance,
		Scope: gardenauth.Scope{Realm: "garden", Segments: []gardenauth.Segment{{Kind: "job", Identifier: "chat-1"}}}, Classification: "internal"}
}

func client(t *testing.T, endpoint string) *gardenauth.Client {
	t.Helper()
	c, err := gardenauth.New(config(endpoint))
	if err != nil {
		t.Fatal(err)
	}
	return c
}

func TestAuthenticateUsesExactDiagnosticRequest(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Method != "POST" || r.URL.RequestURI() != "/memory/v1/diagnose" {
			t.Errorf("unexpected request: %s %s", r.Method, r.URL)
		}
		if r.Header.Get("Authorization") != "Bearer "+token {
			t.Error("missing bearer")
		}
		if r.Header.Get("Content-Type") != "application/json" || r.Header.Get("Idempotency-Key") != "" {
			t.Error("incorrect request headers")
		}
		var request map[string]any
		if err := json.NewDecoder(r.Body).Decode(&request); err != nil {
			t.Error(err)
		}
		want := `{"classification":"internal","scope":{"realm":"garden","segments":[{"identifier":"chat-1","kind":"job"}]}}`
		got, _ := json.Marshal(request)
		if string(got) != want {
			t.Errorf("request = %s", got)
		}
		w.Header().Set("Content-Type", "application/json")
		io.WriteString(w, diagnostic)
	}))
	defer srv.Close()
	got, err := client(t, srv.URL).Authenticate(context.Background(), token)
	if err != nil {
		t.Fatal(err)
	}
	if got != (gardenauth.Identity{PrincipalID: principal, Kind: "workload", CanRead: true, CanWrite: true}) {
		t.Fatalf("identity = %+v", got)
	}
}

func TestPermissionsRefreshWithoutCaching(t *testing.T) {
	var calls atomic.Int32
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		body := diagnostic
		switch calls.Add(1) {
		case 2:
			body = strings.Replace(body, `"ingest":true`, `"ingest":false`, 1)
		case 3:
			body = strings.Replace(body, `"retrieve":true`, `"retrieve":false`, 1)
		case 4:
			w.WriteHeader(http.StatusUnauthorized)
			return
		}
		w.Header().Set("Content-Type", "application/json")
		io.WriteString(w, body)
	}))
	defer srv.Close()
	c := client(t, srv.URL)
	for _, want := range []gardenauth.Identity{
		{PrincipalID: principal, Kind: "workload", CanRead: true, CanWrite: true},
		{PrincipalID: principal, Kind: "workload", CanRead: true, CanWrite: false},
		{PrincipalID: principal, Kind: "workload", CanRead: false, CanWrite: false},
	} {
		got, err := c.Authenticate(context.Background(), token)
		if err != nil || got != want {
			t.Fatalf("got %+v, %v; want %+v", got, err, want)
		}
	}
	if _, err := c.Authenticate(context.Background(), token); !errors.Is(err, gardenauth.ErrInvalidCredentials) {
		t.Fatalf("revoked credential: %v", err)
	}
}

func TestAuthenticationFailuresAreTypedAndSanitised(t *testing.T) {
	for _, tc := range []struct {
		code int
		want error
	}{
		{401, gardenauth.ErrInvalidCredentials}, {403, gardenauth.ErrPermissionDenied},
		{429, gardenauth.ErrUnavailable}, {500, gardenauth.ErrUnavailable},
		{503, gardenauth.ErrUnavailable}, {404, gardenauth.ErrInvalidResponse}, {204, gardenauth.ErrInvalidResponse},
	} {
		t.Run(http.StatusText(tc.code), func(t *testing.T) {
			srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { w.WriteHeader(tc.code); io.WriteString(w, token) }))
			defer srv.Close()
			got, err := client(t, srv.URL).Authenticate(context.Background(), token)
			if !errors.Is(err, tc.want) || strings.Contains(err.Error(), token) || got != (gardenauth.Identity{}) {
				t.Fatalf("identity %+v error %v", got, err)
			}
		})
	}
}

func TestRedirectCannotReceiveCredential(t *testing.T) {
	var followed atomic.Bool
	target := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { followed.Store(true); io.WriteString(w, diagnostic) }))
	defer target.Close()
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		http.Redirect(w, r, target.URL, http.StatusTemporaryRedirect)
	}))
	defer srv.Close()
	_, err := client(t, srv.URL).Authenticate(context.Background(), token)
	if !errors.Is(err, gardenauth.ErrInvalidResponse) || followed.Load() {
		t.Fatalf("redirect followed=%v error=%v", followed.Load(), err)
	}
}

func TestMalformedDiagnosticsCannotEstablishIdentity(t *testing.T) {
	for name, body := range map[string]string{
		"wrong instance":         strings.Replace(diagnostic, instance, principal, 1),
		"uppercase principal":    strings.Replace(diagnostic, principal, strings.ToUpper(principal), 1),
		"UUID wrong version":     strings.Replace(diagnostic, principal, "d1dc7c3d-3808-59b6-8334-a632b958f458", 1),
		"wrong kind":             strings.Replace(diagnostic, `"workload"`, `"admin"`, 1),
		"wrong scope":            strings.Replace(diagnostic, "chat-1", "chat-2", 1),
		"scope null":             strings.Replace(diagnostic, `"segments":[{"kind":"job","identifier":"chat-1"}]`, `"segments":null`, 1),
		"wrong classification":   strings.Replace(diagnostic, `"internal"`, `"public"`, 1),
		"string permission":      strings.Replace(diagnostic, `"retrieve":true`, `"retrieve":"true"`, 1),
		"null permission":        strings.Replace(diagnostic, `"retrieve":true`, `"retrieve":null`, 1),
		"missing permission":     strings.Replace(diagnostic, `"retrieve":true,`, "", 1),
		"unknown permission":     strings.Replace(diagnostic, `"retrieve":true`, `"retrieve":true,"admin":true`, 1),
		"duplicate permission":   strings.Replace(diagnostic, `"retrieve":true`, `"retrieve":false,"retrieve":true`, 1),
		"duplicate root":         strings.Replace(diagnostic, `"principal_kind":"workload"`, `"principal_kind":"human","principal_kind":"workload"`, 1),
		"case alias":             strings.Replace(diagnostic, `"principal_kind":"workload"`, `"PRINCIPAL_KIND":"workload"`, 1),
		"wrong contract":         strings.Replace(diagnostic, "cairn.memory/v1", "cairn.memory/v2", 1),
		"wrong permission basis": strings.Replace(diagnostic, "current_grants_only", "cached_permissions", 1),
		"invalid digest":         strings.Replace(diagnostic, strings.Repeat("a", 64), "invalid", 1),
		"invalid version":        strings.Replace(diagnostic, `"0.1.0"`, `"untrusted prose"`, 1),
		"invalid timestamp":      strings.Replace(diagnostic, "2026-09-17", "2026-99-99", 1),
		"missing field":          strings.Replace(diagnostic, `"product_version":"0.1.0",`, "", 1),
		"extra field":            strings.Replace(diagnostic, `"product_version":"0.1.0"`, `"product_version":"0.1.0","unexpected":true`, 1),
		"array":                  "[" + diagnostic + "]", "null": "null", "multiple objects": diagnostic + diagnostic,
		"oversized": diagnostic + strings.Repeat(" ", 17000), "bad json": "{", "nested": strings.Repeat("[", 2000) + strings.Repeat("]", 2000),
	} {
		t.Run(name, func(t *testing.T) {
			srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				w.Header().Set("Content-Type", "application/json")
				io.WriteString(w, body)
			}))
			defer srv.Close()
			got, err := client(t, srv.URL).Authenticate(context.Background(), token)
			if !errors.Is(err, gardenauth.ErrInvalidResponse) || got != (gardenauth.Identity{}) {
				t.Fatalf("identity %+v error %v", got, err)
			}
		})
	}
}

func TestInvalidConfigurationIsRejected(t *testing.T) {
	for _, endpoint := range []string{"http://cairn.example", "http://localhost", "ftp://127.0.0.1", "https://user:password@cairn.example", "https://cairn.example?query=1", "https://cairn.example#fragment", "https://cairn.example:99999", "https://", "/relative"} {
		cfg := config(endpoint)
		if _, err := gardenauth.New(cfg); err == nil {
			t.Errorf("accepted endpoint %q", endpoint)
		}
	}
	for _, mutate := range []func(*gardenauth.Config){
		func(c *gardenauth.Config) { c.InstanceID = strings.ToUpper(instance) },
		func(c *gardenauth.Config) { c.Classification = "secret" },
		func(c *gardenauth.Config) { c.Scope.Realm = "Wrong Realm" },
		func(c *gardenauth.Config) { c.Scope.Segments[0].Kind = "JOB" },
		func(c *gardenauth.Config) { c.Scope.Segments[0].Identifier = "space here" },
		func(c *gardenauth.Config) { c.Scope.Segments = make([]gardenauth.Segment, 17) },
	} {
		cfg := config("https://cairn.example")
		mutate(&cfg)
		if _, err := gardenauth.New(cfg); err == nil {
			t.Errorf("accepted config %+v", cfg)
		}
	}
	for _, endpoint := range []string{"http://127.0.0.1:8888", "http://[::1]:8888", "https://cairn.example"} {
		if _, err := gardenauth.New(config(endpoint)); err != nil {
			t.Errorf("rejected %s: %v", endpoint, err)
		}
	}
}

func TestConfigurationCannotBeMutatedAfterConstruction(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		body, _ := io.ReadAll(r.Body)
		if !strings.Contains(string(body), `"identifier":"chat-1"`) {
			t.Errorf("mutated scope sent: %s", body)
		}
		w.Header().Set("Content-Type", "application/json")
		io.WriteString(w, diagnostic)
	}))
	defer srv.Close()
	cfg := config(srv.URL)
	c, err := gardenauth.New(cfg)
	if err != nil {
		t.Fatal(err)
	}
	cfg.Scope.Segments[0].Identifier = "another-job"
	if _, err := c.Authenticate(context.Background(), token); err != nil {
		t.Fatal(err)
	}
}

func TestTransportFailureAndCancelledRequest(t *testing.T) {
	release := make(chan struct{})
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { <-release }))
	c := client(t, srv.URL)
	ctx, cancel := context.WithTimeout(context.Background(), 20*time.Millisecond)
	defer cancel()
	_, err := c.Authenticate(ctx, token)
	close(release)
	srv.Close()
	if !errors.Is(err, gardenauth.ErrUnavailable) {
		t.Fatalf("cancel: %v", err)
	}
	_, err = c.Authenticate(context.Background(), token)
	if !errors.Is(err, gardenauth.ErrUnavailable) || strings.Contains(err.Error(), srv.URL) {
		t.Fatalf("connection failure: %v", err)
	}
}

func TestInvalidBearerNeverReachesUpstream(t *testing.T) {
	var called atomic.Bool
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { called.Store(true) }))
	defer srv.Close()
	c := client(t, srv.URL)
	for _, bad := range []string{"", "space token", "token\nInjected: header", strings.Repeat("a", 4097)} {
		if _, err := c.Authenticate(context.Background(), bad); !errors.Is(err, gardenauth.ErrInvalidCredentials) {
			t.Errorf("invalid token error: %v", err)
		}
	}
	if called.Load() {
		t.Fatal("invalid credential reached upstream")
	}
}

func TestRootScopeAndHumanWithoutGrants(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		body, _ := io.ReadAll(r.Body)
		if !strings.Contains(string(body), `"segments":[]`) {
			t.Errorf("root scope must use []: %s", body)
		}
		result := strings.Replace(diagnostic, `[{"kind":"job","identifier":"chat-1"}]`, `[]`, 1)
		result = strings.ReplaceAll(result, `:true`, `:false`)
		result = strings.Replace(result, `"workload"`, `"human"`, 1)
		w.Header().Set("Content-Type", "application/json; charset=utf-8")
		io.WriteString(w, result)
	}))
	defer srv.Close()
	cfg := config(srv.URL)
	cfg.Scope.Segments = nil
	c, err := gardenauth.New(cfg)
	if err != nil {
		t.Fatal(err)
	}
	got, err := c.Authenticate(context.Background(), token)
	if err != nil || got != (gardenauth.Identity{PrincipalID: principal, Kind: "human"}) {
		t.Fatalf("identity=%+v error=%v", got, err)
	}
}

func TestTLSDoesNotTrustAnUnverifiedServer(t *testing.T) {
	var called atomic.Bool
	srv := httptest.NewUnstartedServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { called.Store(true) }))
	srv.Config.ErrorLog = log.New(io.Discard, "", 0)
	srv.StartTLS()
	defer srv.Close()
	_, err := client(t, srv.URL).Authenticate(context.Background(), token)
	if !errors.Is(err, gardenauth.ErrUnavailable) || called.Load() {
		t.Fatalf("unverified TLS reached handler=%v error=%v", called.Load(), err)
	}
}

func TestResponseRequiresJSONContentType(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "text/plain")
		io.WriteString(w, diagnostic)
	}))
	defer srv.Close()
	if _, err := client(t, srv.URL).Authenticate(context.Background(), token); !errors.Is(err, gardenauth.ErrInvalidResponse) {
		t.Fatalf("content type: %v", err)
	}
}

func TestConcurrentCredentialsKeepSeparateIdentities(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		body := diagnostic
		if r.Header.Get("Authorization") == "Bearer other-synthetic-token" {
			body = strings.Replace(body, principal, instance, 1)
		}
		w.Header().Set("Content-Type", "application/json")
		io.WriteString(w, body)
	}))
	defer srv.Close()
	c := client(t, srv.URL)
	var wg sync.WaitGroup
	for i := 0; i < 16; i++ {
		wg.Add(1)
		go func(i int) {
			defer wg.Done()
			bearer, want := token, principal
			if i%2 == 0 {
				bearer, want = "other-synthetic-token", instance
			}
			got, err := c.Authenticate(context.Background(), bearer)
			if err != nil || got.PrincipalID != want {
				t.Errorf("crossed identity: %+v %v", got, err)
			}
		}(i)
	}
	wg.Wait()
}

func TestEncodedDiagnosticIsRejectedBeforeReadingBody(t *testing.T) {
	for _, encoding := range []string{"gzip", "br", "identity, gzip"} {
		t.Run(encoding, func(t *testing.T) {
			release := make(chan struct{})
			srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				if r.Header.Get("Accept-Encoding") != "identity" {
					t.Error("diagnostic request did not require identity encoding")
				}
				w.Header().Set("Content-Type", "application/json")
				w.Header().Set("Content-Encoding", encoding)
				w.(http.Flusher).Flush()
				// Supply no body: a decoder/read must wait until the client deadline.
				// Rejection must instead occur at the header boundary.
				<-release
			}))
			ctx, cancel := context.WithTimeout(context.Background(), 100*time.Millisecond)
			defer cancel()
			got, err := client(t, srv.URL).Authenticate(ctx, token)
			close(release)
			srv.Close()
			if !errors.Is(err, gardenauth.ErrInvalidResponse) || got != (gardenauth.Identity{}) {
				t.Fatalf("encoded body was read: identity=%+v error=%v", got, err)
			}
		})
	}
}

func TestCompressedValidDiagnosticCannotAuthenticate(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		w.Header().Set("Content-Encoding", "gzip")
		gz := gzip.NewWriter(w)
		io.WriteString(gz, diagnostic)
		gz.Close()
	}))
	defer srv.Close()
	got, err := client(t, srv.URL).Authenticate(context.Background(), token)
	if !errors.Is(err, gardenauth.ErrInvalidResponse) || got != (gardenauth.Identity{}) {
		t.Fatalf("compressed identity=%+v error=%v", got, err)
	}
}

func TestAmbiguousContentEncodingIsRejected(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		w.Header().Add("Content-Encoding", "identity")
		w.Header().Add("Content-Encoding", "gzip")
		io.WriteString(w, diagnostic)
	}))
	defer srv.Close()
	if _, err := client(t, srv.URL).Authenticate(context.Background(), token); !errors.Is(err, gardenauth.ErrInvalidResponse) {
		t.Fatalf("ambiguous encoding: %v", err)
	}
}

func TestExplicitIdentityEncodingIsAccepted(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		w.Header().Set("Content-Encoding", "identity")
		io.WriteString(w, diagnostic)
	}))
	defer srv.Close()
	if _, err := client(t, srv.URL).Authenticate(context.Background(), token); err != nil {
		t.Fatalf("identity encoding: %v", err)
	}
}
