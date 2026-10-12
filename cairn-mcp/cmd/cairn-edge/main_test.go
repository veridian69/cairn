package main

import (
	"bytes"
	"context"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

func writeEdgeConfig(t *testing.T, dir string, upstream string) string {
	t.Helper()
	credential := filepath.Join(dir, "claude-ai")
	if err := os.WriteFile(credential, []byte("cairn1.x.y\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	content := `{"team_domain":"example.cloudflareaccess.com","audience":"aud","allowed_email":"jon@jclk.ch","upstream_url":"` + upstream + `","allow_http_upstream":true,"routes":{"/mcp":"` + credential + `"}}`
	path := filepath.Join(dir, "edge.json")
	if err := os.WriteFile(path, []byte(content), 0o600); err != nil {
		t.Fatal(err)
	}
	return path
}

func TestCheckValidConfiguration(t *testing.T) {
	path := writeEdgeConfig(t, t.TempDir(), "http://127.0.0.1:1/memory/v1/mcp")
	var stdout, stderr bytes.Buffer
	if code := run(context.Background(), []string{"check", "--config", path}, &stdout, &stderr); code != 0 {
		t.Fatalf("exit %d: %s", code, stderr.String())
	}
	if !strings.Contains(stdout.String(), "configuration valid") {
		t.Fatalf("stdout = %q", stdout.String())
	}
}

func TestCheckMissingCredentialIsBland(t *testing.T) {
	dir := t.TempDir()
	path := writeEdgeConfig(t, dir, "http://127.0.0.1:1/memory/v1/mcp")
	if err := os.Remove(filepath.Join(dir, "claude-ai")); err != nil {
		t.Fatal(err)
	}
	var stdout, stderr bytes.Buffer
	if code := run(context.Background(), []string{"check", "--config", path}, &stdout, &stderr); code == 0 {
		t.Fatal("expected failure")
	}
	if !strings.Contains(stderr.String(), "configuration invalid") || strings.Contains(stderr.String(), dir) {
		t.Fatalf("stderr = %q", stderr.String())
	}
}

func TestUnknownCommandPrintsUsage(t *testing.T) {
	var stdout, stderr bytes.Buffer
	if code := run(context.Background(), []string{"bogus"}, &stdout, &stderr); code != 2 {
		t.Fatalf("exit %d", code)
	}
	if !strings.Contains(stderr.String(), "cairn-edge [serve|check]") {
		t.Fatalf("stderr = %q", stderr.String())
	}
}

func TestJWKSClientDoesNotFollowRedirects(t *testing.T) {
	followed := false
	target := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		followed = true
	}))
	defer target.Close()
	redirector := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		http.Redirect(w, r, target.URL, http.StatusFound)
	}))
	defer redirector.Close()
	resp, err := newJWKSClient(5 * time.Second).Get(redirector.URL)
	if err != nil {
		t.Fatal(err)
	}
	resp.Body.Close()
	if resp.StatusCode != http.StatusFound || followed {
		t.Fatalf("status %d, followed %v", resp.StatusCode, followed)
	}
}
