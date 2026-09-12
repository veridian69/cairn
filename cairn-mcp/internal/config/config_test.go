package config

import (
	"path/filepath"
	"strings"
	"testing"
	"time"
)

func TestParseConfigDefaults(t *testing.T) {
	t.Helper()
	home := t.TempDir()
	t.Setenv("HOME", home)

	command, cfg, err := ParseConfig(nil)
	if err != nil {
		t.Fatalf("ParseConfig() unexpected error: %v", err)
	}
	if command != "serve" {
		t.Fatalf("command = %q, want %q", command, "serve")
	}

	if got, want := cfg.BindHost, "127.0.0.1"; got != want {
		t.Fatalf("bind host %q, want %q", got, want)
	}
	if got, want := cfg.Port, 8765; got != want {
		t.Fatalf("port %d, want %d", got, want)
	}
	if got, want := cfg.UpstreamURL, "https://cairn.example.invalid/mcp"; got != want {
		t.Fatalf("upstream %q, want %q", got, want)
	}
	if got, want := cfg.ConnectTimeout, 5*time.Second; got != want {
		t.Fatalf("connect timeout %s, want %s", got, want)
	}
	if got, want := cfg.HeaderReadTimeout, 30*time.Second; got != want {
		t.Fatalf("header read timeout %s, want %s", got, want)
	}
	if got, want := cfg.WriteStallTimeout, 30*time.Second; got != want {
		t.Fatalf("write stall timeout %s, want %s", got, want)
	}
	if got, want := cfg.ReadTimeout, 300*time.Second; got != want {
		t.Fatalf("read timeout %s, want %s", got, want)
	}
	if got, want := cfg.StreamIdleTimeout, 300*time.Second; got != want {
		t.Fatalf("stream idle timeout %s, want %s", got, want)
	}
	if got, want := cfg.PoolTimeout, 5*time.Second; got != want {
		t.Fatalf("pool timeout %s, want %s", got, want)
	}
	if got, want := cfg.ShutdownGraceSeconds, 10*time.Second; got != want {
		t.Fatalf("shutdown grace %s, want %s", got, want)
	}

	expectedDir := filepath.Join(home, ".config", "cairn")
	if got, want := cfg.LocalTokenPath, filepath.Join(expectedDir, "relay-token"); got != want {
		t.Fatalf("token path %q, want %q", got, want)
	}
	if got, want := cfg.CFClientIDPath, filepath.Join(expectedDir, "cf-access-client-id"); got != want {
		t.Fatalf("client id path %q, want %q", got, want)
	}
	if got, want := cfg.CFClientSecretPath, filepath.Join(expectedDir, "cf-access-client-secret"); got != want {
		t.Fatalf("client secret path %q, want %q", got, want)
	}
}

func TestDefaultConfigDirRefusesToGuessWhenHomeIsUnavailable(t *testing.T) {
	t.Setenv("HOME", "")

	dir, err := DefaultConfigDir()
	if err == nil {
		t.Fatalf("DefaultConfigDir() = %q, want an error rather than a fallback", dir)
	}
	if dir != "" {
		t.Fatalf("DefaultConfigDir() = %q, want no path alongside the error", dir)
	}
}

func TestDefaultConfigDirUsesHomeWhenAvailable(t *testing.T) {
	home := t.TempDir()
	t.Setenv("HOME", home)

	dir, err := DefaultConfigDir()
	if err != nil {
		t.Fatalf("DefaultConfigDir() unexpected error: %v", err)
	}
	if want := filepath.Join(home, ".config", "cairn"); dir != want {
		t.Fatalf("DefaultConfigDir() = %q, want %q", dir, want)
	}
}

func TestParseConfigFailsWhenHomeIsUnavailable(t *testing.T) {
	t.Setenv("HOME", "")

	_, cfg, err := ParseConfig(nil)
	if err == nil {
		t.Fatal("ParseConfig() error = nil, want failure when the home directory is unavailable")
	}
	if strings.Contains(cfg.LocalTokenPath+cfg.CFClientIDPath+cfg.CFClientSecretPath, "/root") {
		t.Fatalf("ParseConfig() fell back to root paths: %+v", cfg)
	}
}

func TestParseConfigCommand(t *testing.T) {
	t.Helper()
	t.Setenv("HOME", t.TempDir())

	command, _, err := ParseConfig([]string{"check", "--bind-host", "127.0.0.1"})
	if err != nil {
		t.Fatalf("ParseConfig() unexpected error: %v", err)
	}
	if command != "check" {
		t.Fatalf("command %q, want %q", command, "check")
	}
}

func TestParseConfigStdioDoesNotResolveLocalTokenPath(t *testing.T) {
	t.Setenv("HOME", t.TempDir())

	command, cfg, err := ParseConfig([]string{"stdio"})
	if err != nil {
		t.Fatalf("ParseConfig() unexpected error: %v", err)
	}
	if command != "stdio" {
		t.Fatalf("command %q, want %q", command, "stdio")
	}
	if cfg.LocalTokenPath != "" {
		t.Fatalf("local token path %q, want empty", cfg.LocalTokenPath)
	}
}

func TestParseConfigRejectsBadCommand(t *testing.T) {
	t.Helper()
	t.Setenv("HOME", t.TempDir())

	_, _, err := ParseConfig([]string{"bad", "--bind-host", "127.0.0.1"})
	if err == nil {
		t.Fatal("expected error for unsupported command")
	}
	if got, want := err.Error(), "command must be one of serve, stdio, check"; !strings.Contains(got, want) {
		t.Fatalf("error %q, want containing %q", got, want)
	}
}

func TestParseConfigRejectsInvalidValues(t *testing.T) {
	t.Helper()
	t.Setenv("HOME", t.TempDir())

	cases := []struct {
		name string
		args []string
	}{
		{name: "bind host", args: []string{"--bind-host", "0.0.0.0"}},
		{name: "bad port zero", args: []string{"--port", "0"}},
		{name: "bad port max", args: []string{"--port", "65536"}},
		{name: "bad upstream query", args: []string{"--upstream-url", "https://cairn.example.invalid/mcp?x=1"}},
		{name: "bad upstream fragment", args: []string{"--upstream-url", "https://cairn.example.invalid/mcp#x"}},
		{name: "bad upstream credentials", args: []string{"--upstream-url", "https://u:pw@cairn.example.invalid/mcp"}},
		{name: "bad upstream protocol", args: []string{"--upstream-url", "ftp://cairn.example.invalid/mcp"}},
		{name: "bad negative timeout", args: []string{"--connect-timeout", "0"}},
		{name: "bad write stall timeout", args: []string{"--write-stall-timeout", "0"}},
		{name: "bad stream idle timeout", args: []string{"--stream-idle-timeout", "0"}},
		{name: "http without explicit allow", args: []string{"--upstream-url", "http://cairn.example.invalid/mcp"}},
	}

	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			if _, _, err := ParseConfig(c.args); err == nil {
				t.Fatalf("expected error for args %v", c.args)
			}
		})
	}
}

func TestParseConfigAllowsHTTPWithFlag(t *testing.T) {
	t.Helper()
	t.Setenv("HOME", t.TempDir())

	_, cfg, err := ParseConfig([]string{
		"--upstream-url",
		"http://127.0.0.1/mcp",
		"--allow-http-upstream",
	})
	if err != nil {
		t.Fatalf("ParseConfig() unexpected error: %v", err)
	}
	if !cfg.AllowHTTPUpstream {
		t.Fatal("expected allow-http-upstream to be true")
	}
	if cfg.UpstreamURL != "http://127.0.0.1/mcp" {
		t.Fatalf("upstream %q, want %q", cfg.UpstreamURL, "http://127.0.0.1/mcp")
	}
}
