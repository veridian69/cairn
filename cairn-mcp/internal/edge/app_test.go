package edge

import (
	"bytes"
	"context"
	"crypto/rsa"
	"io"
	"log"
	"net"
	"net/http"
	"net/http/httptest"
	"os"
	"strings"
	"testing"
	"time"

	"github.com/veridian69/cairn/cairn-mcp/internal/accessjwt"
	"github.com/veridian69/cairn/cairn-mcp/internal/accessjwt/accessjwttest"
)

type captured struct {
	method  string
	path    string
	headers http.Header
	body    []byte
}

type fixture struct {
	app      *App
	server   *httptest.Server
	upstream chan captured
	key      *rsa.PrivateKey
	expect   accessjwt.Expectation
}

func newFixture(t *testing.T, upstreamHandler http.HandlerFunc) *fixture {
	t.Helper()
	f := &fixture{upstream: make(chan captured, 4)}
	f.key = accessjwttest.Key(t)
	jwks := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		_, _ = w.Write(accessjwttest.JWKS(t, "k1", &f.key.PublicKey))
	}))
	t.Cleanup(jwks.Close)
	if upstreamHandler == nil {
		upstreamHandler = func(w http.ResponseWriter, r *http.Request) {
			body, _ := io.ReadAll(r.Body)
			f.upstream <- captured{method: r.Method, path: r.URL.Path, headers: r.Header.Clone(), body: body}
			w.Header().Set("Content-Type", "application/json")
			w.Header().Set("X-Ignored", "1")
			_, _ = w.Write([]byte(`{"ok":true}`))
		}
	}
	upstream := httptest.NewServer(upstreamHandler)
	t.Cleanup(upstream.Close)
	dir := t.TempDir()
	credentialPath := writeFile(t, dir, "claude-ai", "cairn1.browser.token\n")
	cfg := Config{
		TeamDomain: "example.cloudflareaccess.com", Audience: "aud-1", AllowedEmail: "jon@jclk.ch",
		UpstreamURL: upstream.URL + "/memory/v1/mcp", AllowHTTPUpstream: true,
		Routes: map[string]string{"/mcp": credentialPath}, ReadTimeout: 2 * time.Second, ConnectTimeout: time.Second,
	}
	tokens, err := NewRouteTokens(cfg.Routes, os.Geteuid())
	if err != nil {
		t.Fatalf("tokens: %v", err)
	}
	verifier := accessjwt.NewVerifier(jwks.URL, jwks.Client(), accessjwt.VerifierOptions{})
	if err := verifier.Fetch(context.Background()); err != nil {
		t.Fatalf("jwks: %v", err)
	}
	f.expect = accessjwt.Expectation{Issuer: cfg.Issuer(), Audience: cfg.Audience, Email: cfg.AllowedEmail}
	f.app = NewApp(cfg, verifier, tokens, nil, log.New(io.Discard, "", 0))
	f.app.SetStarted(true)
	f.server = httptest.NewServer(f.app)
	t.Cleanup(f.server.Close)
	return f
}

func (f *fixture) assertion(t *testing.T, mutate func(map[string]any)) string {
	claims := map[string]any{
		"iss": f.expect.Issuer, "aud": []string{f.expect.Audience}, "email": f.expect.Email, "sub": "u",
		"iat": time.Now().Add(-time.Minute).Unix(), "exp": time.Now().Add(time.Hour).Unix(), "type": "app",
	}
	if mutate != nil {
		mutate(claims)
	}
	return accessjwttest.Token(t, f.key, "k1", claims)
}

func (f *fixture) post(t *testing.T, path, assertion string, body string, extra http.Header) *http.Response {
	t.Helper()
	request, _ := http.NewRequest(http.MethodPost, f.server.URL+path, strings.NewReader(body))
	request.Header.Set("Content-Type", "application/json")
	if assertion != "" {
		request.Header.Set("Cf-Access-Jwt-Assertion", assertion)
	}
	for name, values := range extra {
		for _, value := range values {
			request.Header.Add(name, value)
		}
	}
	response, err := http.DefaultClient.Do(request)
	if err != nil {
		t.Fatalf("post: %v", err)
	}
	t.Cleanup(func() { response.Body.Close() })
	return response
}

func TestEdgeForwardsWithBearerAndStripsClientCredentials(t *testing.T) {
	f := newFixture(t, nil)
	extra := http.Header{}
	extra.Set("Authorization", "Bearer client-supplied")
	extra.Set("CF-Access-Client-Id", "spoofed")
	extra.Set("MCP-Protocol-Version", "2025-11-25")
	response := f.post(t, "/mcp", f.assertion(t, nil), `{"jsonrpc":"2.0","id":1,"method":"ping"}`, extra)
	if response.StatusCode != http.StatusOK {
		t.Fatalf("status = %d", response.StatusCode)
	}
	if response.Header.Get("X-Ignored") != "" {
		t.Fatal("non-allowlisted response header must be dropped")
	}
	got := <-f.upstream
	if got.path != "/memory/v1/mcp" || got.method != http.MethodPost {
		t.Fatalf("upstream %s %s", got.method, got.path)
	}
	if values := got.headers.Values("Authorization"); len(values) != 1 || values[0] != "Bearer cairn1.browser.token" {
		t.Fatalf("Authorization = %v", values)
	}
	for _, name := range []string{"CF-Access-Client-Id", "Cf-Access-Jwt-Assertion"} {
		if got.headers.Get(name) != "" {
			t.Fatalf("%s must not reach upstream", name)
		}
	}
	if got.headers.Get("MCP-Protocol-Version") != "2025-11-25" {
		t.Fatal("allowlisted header must pass")
	}
	if !bytes.Equal(got.body, []byte(`{"jsonrpc":"2.0","id":1,"method":"ping"}`)) {
		t.Fatalf("body = %s", got.body)
	}
}

func TestEdgeRefusals(t *testing.T) {
	f := newFixture(t, nil)
	other := accessjwttest.Key(t)
	cases := []struct {
		name      string
		path      string
		method    string
		assertion string
		want      int
	}{
		{"missing assertion", "/mcp", http.MethodPost, "", 401},
		{"forged", "/mcp", http.MethodPost, accessjwttest.Token(t, other, "k1", map[string]any{"iss": f.expect.Issuer, "aud": []string{f.expect.Audience}, "email": f.expect.Email, "exp": time.Now().Add(time.Hour).Unix()}), 401},
		{"expired", "/mcp", http.MethodPost, f.assertion(t, func(c map[string]any) { c["exp"] = time.Now().Add(-time.Minute).Unix() }), 401},
		{"wrong audience", "/mcp", http.MethodPost, f.assertion(t, func(c map[string]any) { c["aud"] = []string{"other"} }), 401},
		{"wrong issuer", "/mcp", http.MethodPost, f.assertion(t, func(c map[string]any) { c["iss"] = "https://other.cloudflareaccess.com" }), 401},
		{"wrong identity", "/mcp", http.MethodPost, f.assertion(t, func(c map[string]any) { c["email"] = "mallory@example.com" }), 403},
		{"service token", "/mcp", http.MethodPost, f.assertion(t, func(c map[string]any) { c["email"] = ""; c["common_name"] = "svc.access" }), 403},
		{"unknown path", "/other", http.MethodPost, f.assertion(t, nil), 404},
		{"wrong method", "/mcp", http.MethodGet, f.assertion(t, nil), 405},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			request, _ := http.NewRequest(tc.method, f.server.URL+tc.path, strings.NewReader("{}"))
			if tc.assertion != "" {
				request.Header.Set("Cf-Access-Jwt-Assertion", tc.assertion)
			}
			response, err := http.DefaultClient.Do(request)
			if err != nil {
				t.Fatalf("do: %v", err)
			}
			defer response.Body.Close()
			if response.StatusCode != tc.want {
				t.Fatalf("status = %d, want %d", response.StatusCode, tc.want)
			}
			body, _ := io.ReadAll(response.Body)
			if strings.Contains(string(body), "example.cloudflareaccess.com") || strings.Contains(string(body), "jon@jclk.ch") || strings.Contains(string(body), "cairn1.") {
				t.Fatalf("error body leaks detail: %s", body)
			}
		})
	}
	select {
	case got := <-f.upstream:
		t.Fatalf("a refused request reached upstream: %+v", got)
	default:
	}
}

func TestEdgeRefusesOversizedBodyBeforeUpstream(t *testing.T) {
	f := newFixture(t, nil)
	big := strings.Repeat("x", 4*1024*1024+1)
	response := f.post(t, "/mcp", f.assertion(t, nil), big, nil)
	if response.StatusCode != http.StatusRequestEntityTooLarge {
		t.Fatalf("status = %d", response.StatusCode)
	}
	select {
	case <-f.upstream:
		t.Fatal("oversized body reached upstream")
	default:
	}
}

func TestEdgeMapsUpstreamRedirectAndTimeout(t *testing.T) {
	redirecting := newFixture(t, func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Location", "https://elsewhere/")
		w.WriteHeader(http.StatusFound)
	})
	if got := redirecting.post(t, "/mcp", redirecting.assertion(t, nil), "{}", nil).StatusCode; got != http.StatusBadGateway {
		t.Fatalf("redirect status = %d", got)
	}
	stalling := newFixture(t, func(w http.ResponseWriter, r *http.Request) {
		// Drain the body first: the server only notices the edge hanging up once it has.
		_, _ = io.Copy(io.Discard, r.Body)
		<-r.Context().Done()
	})
	if got := stalling.post(t, "/mcp", stalling.assertion(t, nil), "{}", nil).StatusCode; got != http.StatusGatewayTimeout {
		t.Fatalf("timeout status = %d", got)
	}
}

func TestEdgeHealthzAndNotReady(t *testing.T) {
	f := newFixture(t, nil)
	response, _ := http.Get(f.server.URL + "/healthz")
	if response.StatusCode != http.StatusOK {
		t.Fatalf("healthz = %d", response.StatusCode)
	}
	f.app.SetStarted(false)
	if got := f.post(t, "/mcp", f.assertion(t, nil), "{}", nil).StatusCode; got != http.StatusServiceUnavailable {
		t.Fatalf("not ready = %d", got)
	}
}

func TestEdgeCutsOffAClientThatStopsReading(t *testing.T) {
	chunk := bytes.Repeat([]byte("x"), 64*1024)
	f := newFixture(t, func(w http.ResponseWriter, r *http.Request) {
		_, _ = io.Copy(io.Discard, r.Body)
		w.Header().Set("Content-Type", "application/json")
		for i := 0; i < 2048; i++ { // 128 MiB, far beyond any socket buffer
			if _, err := w.Write(chunk); err != nil {
				return
			}
		}
	})
	f.app.writeStall = 200 * time.Millisecond
	done := make(chan struct{})
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		defer close(done)
		f.app.ServeHTTP(w, r)
	}))
	t.Cleanup(server.Close)
	conn, err := net.Dial("tcp", strings.TrimPrefix(server.URL, "http://"))
	if err != nil {
		t.Fatalf("dial: %v", err)
	}
	defer conn.Close()
	request := "POST /mcp HTTP/1.1\r\nHost: edge\r\nContent-Length: 2\r\nCf-Access-Jwt-Assertion: " + f.assertion(t, nil) + "\r\n\r\n{}"
	if _, err := conn.Write([]byte(request)); err != nil {
		t.Fatalf("write: %v", err)
	}
	// Never read: the handler must give up on its own.
	select {
	case <-done:
	case <-time.After(10 * time.Second):
		t.Fatal("handler still blocked writing to a client that stopped reading")
	}
}

func TestEdgeLogsOnlyFixedRouteLabels(t *testing.T) {
	f := newFixture(t, nil)
	var logs bytes.Buffer
	f.app.logger = log.New(&logs, "", 0)
	request, _ := http.NewRequest(http.MethodPost, f.server.URL+"/x%0Amethod=POST%20route=/mcp%20status_class=2%20decision=allow", strings.NewReader("{}"))
	response, err := http.DefaultClient.Do(request)
	if err != nil {
		t.Fatalf("do: %v", err)
	}
	response.Body.Close()
	if response.StatusCode != http.StatusNotFound {
		t.Fatalf("status = %d", response.StatusCode)
	}
	lines := strings.Split(strings.TrimSuffix(logs.String(), "\n"), "\n")
	if len(lines) != 1 {
		t.Fatalf("want one log line, got %d: %q", len(lines), logs.String())
	}
	if !strings.Contains(lines[0], " route=unmapped ") || strings.Contains(lines[0], "/x") || strings.Count(lines[0], "decision=") != 1 {
		t.Fatalf("log line carries client text: %q", lines[0])
	}
	logs.Reset()
	f.post(t, "/mcp", f.assertion(t, nil), "{}", nil)
	if !strings.Contains(logs.String(), " route=/mcp ") {
		t.Fatalf("mapped route not logged: %q", logs.String())
	}
}

func TestEdgeAnswers503WhenKeySetIsUnavailable(t *testing.T) {
	f := newFixture(t, nil)
	broken := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		http.Error(w, "down", http.StatusInternalServerError)
	}))
	t.Cleanup(broken.Close)
	f.app.verifier = accessjwt.NewVerifier(broken.URL, broken.Client(), accessjwt.VerifierOptions{})
	var logs bytes.Buffer
	f.app.logger = log.New(&logs, "", 0)
	response := f.post(t, "/mcp", f.assertion(t, nil), "{}", nil)
	if response.StatusCode != http.StatusServiceUnavailable {
		t.Fatalf("status = %d", response.StatusCode)
	}
	body, _ := io.ReadAll(response.Body)
	if !strings.Contains(string(body), `"error":"unavailable"`) {
		t.Fatalf("body = %s", body)
	}
	if !strings.Contains(logs.String(), "decision=keys_unavailable") {
		t.Fatalf("log = %q", logs.String())
	}
	select {
	case got := <-f.upstream:
		t.Fatalf("request reached upstream: %+v", got)
	default:
	}
}
