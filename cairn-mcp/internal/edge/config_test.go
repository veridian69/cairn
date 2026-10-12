package edge

import (
	"fmt"
	"os"
	"path/filepath"
	"testing"
	"time"
)

func writeFile(t *testing.T, dir, name, content string) string {
	t.Helper()
	path := filepath.Join(dir, name)
	if err := os.WriteFile(path, []byte(content), 0o600); err != nil {
		t.Fatalf("write %s: %v", name, err)
	}
	return path
}

const validConfig = `{
  "team_domain": "example.cloudflareaccess.com",
  "audience": "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
  "allowed_email": "jon@jclk.ch",
  "upstream_url": "http://cairn:8000/memory/v1/mcp",
  "allow_http_upstream": true,
  "routes": {"/mcp": "%s", "/chatgpt/mcp": "%s"}
}`

func TestLoadConfigDefaultsAndDerivedURLs(t *testing.T) {
	dir := t.TempDir()
	a := writeFile(t, dir, "claude-ai", "cairn1.a.a\n")
	b := writeFile(t, dir, "chatgpt", "cairn1.b.b\n")
	path := writeFile(t, dir, "edge.json", fmt.Sprintf(validConfig, a, b))
	cfg, err := LoadConfig(path, os.Geteuid())
	if err != nil {
		t.Fatalf("load: %v", err)
	}
	if cfg.CertsURL() != "https://example.cloudflareaccess.com/cdn-cgi/access/certs" {
		t.Fatalf("CertsURL = %q", cfg.CertsURL())
	}
	if cfg.Issuer() != "https://example.cloudflareaccess.com" {
		t.Fatalf("Issuer = %q", cfg.Issuer())
	}
	if cfg.ReadTimeout != 300*time.Second || cfg.ConnectTimeout != 5*time.Second || cfg.JWKSTimeout != 5*time.Second || cfg.JWKSRefresh != time.Hour || cfg.JWKSMaxAge != 24*time.Hour {
		t.Fatalf("defaults = %+v", cfg)
	}
}

func TestLoadConfigRefusals(t *testing.T) {
	dir := t.TempDir()
	a := writeFile(t, dir, "claude-ai", "cairn1.a.a\n")
	cases := map[string]string{
		"unknown field":     `{"team_domain":"t.cloudflareaccess.com","audience":"x","allowed_email":"a@b","upstream_url":"https://c/memory/v1/mcp","routes":{"/mcp":"` + a + `"},"extra":1}`,
		"no routes":         `{"team_domain":"t.cloudflareaccess.com","audience":"x","allowed_email":"a@b","upstream_url":"https://c/memory/v1/mcp","routes":{}}`,
		"relative route":    `{"team_domain":"t.cloudflareaccess.com","audience":"x","allowed_email":"a@b","upstream_url":"https://c/memory/v1/mcp","routes":{"mcp":"` + a + `"}}`,
		"http upstream":     `{"team_domain":"t.cloudflareaccess.com","audience":"x","allowed_email":"a@b","upstream_url":"http://c/memory/v1/mcp","routes":{"/mcp":"` + a + `"}}`,
		"scheme in team":    `{"team_domain":"https://t.cloudflareaccess.com","audience":"x","allowed_email":"a@b","upstream_url":"https://c/memory/v1/mcp","routes":{"/mcp":"` + a + `"}}`,
		"bad email":         `{"team_domain":"t.cloudflareaccess.com","audience":"x","allowed_email":"operator","upstream_url":"https://c/memory/v1/mcp","routes":{"/mcp":"` + a + `"}}`,
		"healthz route":     `{"team_domain":"t.cloudflareaccess.com","audience":"x","allowed_email":"a@b","upstream_url":"https://c/memory/v1/mcp","routes":{"/healthz":"` + a + `"}}`,
		"empty audience":    `{"team_domain":"t.cloudflareaccess.com","audience":"","allowed_email":"a@b","upstream_url":"https://c/memory/v1/mcp","routes":{"/mcp":"` + a + `"}}`,
		"team suffix trick": `{"team_domain":"example.cloudflareaccess.com.evil.com","audience":"x","allowed_email":"a@b","upstream_url":"https://c/memory/v1/mcp","routes":{"/mcp":"` + a + `"}}`,
		"team userinfo":     `{"team_domain":"a@example.cloudflareaccess.com","audience":"x","allowed_email":"a@b","upstream_url":"https://c/memory/v1/mcp","routes":{"/mcp":"` + a + `"}}`,
		"team uppercase":    `{"team_domain":"EXAMPLE.cloudflareaccess.com","audience":"x","allowed_email":"a@b","upstream_url":"https://c/memory/v1/mcp","routes":{"/mcp":"` + a + `"}}`,
		"team bare suffix":  `{"team_domain":"cloudflareaccess.com","audience":"x","allowed_email":"a@b","upstream_url":"https://c/memory/v1/mcp","routes":{"/mcp":"` + a + `"}}`,
		"tiny timeout":      `{"team_domain":"t.cloudflareaccess.com","audience":"x","allowed_email":"a@b","upstream_url":"https://c/memory/v1/mcp","routes":{"/mcp":"` + a + `"},"read_timeout_seconds":1e-10}`,
		"trailing garbage":  `{"team_domain":"t.cloudflareaccess.com","audience":"x","allowed_email":"a@b","upstream_url":"https://c/memory/v1/mcp","routes":{"/mcp":"` + a + `"}}garbage`,
		"second object":     `{"team_domain":"t.cloudflareaccess.com","audience":"x","allowed_email":"a@b","upstream_url":"https://c/memory/v1/mcp","routes":{"/mcp":"` + a + `"}}{}`,
		"empty team":        `{"team_domain":"","audience":"x","allowed_email":"a@b","upstream_url":"https://c/memory/v1/mcp","routes":{"/mcp":"` + a + `"}}`,
	}
	for name, content := range cases {
		t.Run(name, func(t *testing.T) {
			path := writeFile(t, dir, "edge-"+name+".json", content)
			if _, err := LoadConfig(path, os.Geteuid()); err == nil {
				t.Fatal("expected an error")
			}
		})
	}
}

func TestRouteTokensLookupAndReload(t *testing.T) {
	dir := t.TempDir()
	a := writeFile(t, dir, "claude-ai", "cairn1.a.a\n")
	tokens, err := NewRouteTokens(map[string]string{"/mcp": a}, os.Geteuid())
	if err != nil {
		t.Fatalf("new: %v", err)
	}
	got, ok := tokens.Lookup("/mcp")
	if !ok || string(got) != "cairn1.a.a" {
		t.Fatalf("lookup = %q, %v", got, ok)
	}
	if _, ok := tokens.Lookup("/other"); ok {
		t.Fatal("unknown path must not resolve")
	}
	writeFile(t, dir, "claude-ai", "cairn1.rotated\n")
	if err := tokens.Reload(); err != nil {
		t.Fatalf("reload: %v", err)
	}
	if got, _ := tokens.Lookup("/mcp"); string(got) != "cairn1.rotated" {
		t.Fatalf("after reload = %q", got)
	}
	if _, err := NewRouteTokens(map[string]string{"/mcp": filepath.Join(dir, "missing")}, os.Geteuid()); err == nil {
		t.Fatal("missing credential must fail")
	}
}
