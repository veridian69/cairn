package main

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"io"
	"log"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	benchmarking "github.com/veridian69/cairn/cairn-mcp/internal/benchmark"
	"github.com/veridian69/cairn/cairn-mcp/internal/config"
)

func TestControlledBenchmarkEmitsJSONWithoutRunningWallClockGate(t *testing.T) {
	var stdout, stderr bytes.Buffer
	overhead := 0.0
	code := runWithBenchmark(
		context.Background(),
		[]string{"benchmark", "--mode", "controlled", "--samples", "3"},
		io.NopCloser(strings.NewReader("")),
		&stdout,
		&stderr,
		func(context.Context, benchmarking.Options) (benchmarking.Report, error) {
			return benchmarking.Report{
				SchemaVersion:      "1",
				Mode:               "controlled",
				Samples:            3,
				RelayOverheadP95MS: &overhead,
				Passed:             true,
			}, nil
		},
	)
	if code != 0 {
		t.Fatalf("code=%d stderr=%q", code, stderr.String())
	}

	var report struct {
		Mode               string  `json:"mode"`
		Samples            int     `json:"samples"`
		RelayOverheadP95MS float64 `json:"relay_overhead_p95_ms"`
	}
	if err := json.Unmarshal(stdout.Bytes(), &report); err != nil {
		t.Fatalf("stdout is not JSON: %v; stdout=%q", err, stdout.String())
	}
	if report.Mode != "controlled" || report.Samples != 3 || report.RelayOverheadP95MS < 0 {
		t.Fatalf("unexpected report: %+v", report)
	}
}

// A live run that fails partway through still retains the samples it collected,
// and that partial report is the whole point of retaining them: an operator who
// cannot repeat an authorised live run must still get machine-readable output.
func TestBenchmarkEmitsARetainedPartialReportOnFailure(t *testing.T) {
	var stdout, stderr bytes.Buffer
	code := runWithBenchmark(
		context.Background(),
		[]string{"benchmark", "--mode", "controlled", "--samples", "3"},
		io.NopCloser(strings.NewReader("")),
		&stdout,
		&stderr,
		func(context.Context, benchmarking.Options) (benchmarking.Report, error) {
			return benchmarking.Report{
				SchemaVersion: "1",
				Mode:          "live",
				Samples:       18,
				Passed:        false,
			}, errors.New("live sample 19 failed")
		},
	)
	if code != 1 {
		t.Fatalf("code=%d, want 1; stderr=%q", code, stderr.String())
	}
	if !strings.Contains(stderr.String(), "benchmark error") {
		t.Fatalf("stderr=%q, want it to report the error", stderr.String())
	}

	var report struct {
		Mode    string `json:"mode"`
		Samples int    `json:"samples"`
		Passed  bool   `json:"passed"`
	}
	if err := json.Unmarshal(stdout.Bytes(), &report); err != nil {
		t.Fatalf("stdout is not JSON: %v; stdout=%q", err, stdout.String())
	}
	if report.Samples != 18 || report.Passed {
		t.Fatalf("unexpected partial report: %+v", report)
	}
}

// A failure before any sample was collected has nothing to report, so stdout
// stays empty rather than carrying a zero-sample report a consumer must special-case.
func TestBenchmarkEmitsNoReportWhenNoSamplesWereCollected(t *testing.T) {
	var stdout, stderr bytes.Buffer
	code := runWithBenchmark(
		context.Background(),
		[]string{"benchmark", "--mode", "controlled", "--samples", "3"},
		io.NopCloser(strings.NewReader("")),
		&stdout,
		&stderr,
		func(context.Context, benchmarking.Options) (benchmarking.Report, error) {
			return benchmarking.Report{}, errors.New("upstream unreachable")
		},
	)
	if code != 1 || stdout.Len() != 0 {
		t.Fatalf("code=%d stdout=%q stderr=%q", code, stdout.String(), stderr.String())
	}
}

func TestLiveBenchmarkRequiresExplicitConfirmation(t *testing.T) {
	var stdout, stderr bytes.Buffer
	code := run(
		context.Background(),
		[]string{"benchmark", "--mode", "live", "--samples", "1"},
		io.NopCloser(strings.NewReader("")),
		&stdout,
		&stderr,
	)
	if code != 2 || stdout.Len() != 0 || !strings.Contains(stderr.String(), "--confirm-live") {
		t.Fatalf("code=%d stdout=%q stderr=%q", code, stdout.String(), stderr.String())
	}
}

func TestBenchmarkHelpUsesStdout(t *testing.T) {
	var stdout, stderr bytes.Buffer
	code := run(
		context.Background(),
		[]string{"benchmark", "--help"},
		io.NopCloser(strings.NewReader("")),
		&stdout,
		&stderr,
	)
	if code != 0 || stderr.Len() != 0 || !strings.Contains(stdout.String(), "--confirm-live") || !strings.Contains(stdout.String(), "--samples") {
		t.Fatalf("code=%d stdout=%q stderr=%q", code, stdout.String(), stderr.String())
	}
}

func writeMode0600(t *testing.T, name, value string) string {
	t.Helper()
	path := filepath.Join(t.TempDir(), name)
	if err := os.WriteFile(path, []byte(value+"\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	restrictTestSecret(t, path)
	return path
}

func TestStdioDoesNotRequireLocalToken(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(http.ResponseWriter, *http.Request) {}))
	defer server.Close()
	opts := stdioOptions{
		endpoint:         server.URL,
		clientIDPath:     writeMode0600(t, "client-id", "id"),
		clientSecretPath: writeMode0600(t, "client-secret", "secret"),
		httpClient:       server.Client(),
	}
	stdin, stdinWriter := io.Pipe()
	stdinWriter.Close()
	var stdout bytes.Buffer

	if err := runStdio(context.Background(), stdin, &stdout, opts); err != nil {
		t.Fatal(err)
	}
	if stdout.Len() != 0 {
		t.Fatalf("stdout contained diagnostics: %q", stdout.String())
	}
}

func TestUnknownCommandUsesStderr(t *testing.T) {
	var stdout, stderr bytes.Buffer
	code := run(context.Background(), []string{"nonsense"}, io.NopCloser(strings.NewReader("")), &stdout, &stderr)
	if code != 2 || stdout.Len() != 0 || !strings.Contains(stderr.String(), "serve|stdio|check") {
		t.Fatalf("code=%d stdout=%q stderr=%q", code, stdout.String(), stderr.String())
	}
}

func TestHelpUsesStdout(t *testing.T) {
	var stdout, stderr bytes.Buffer
	code := run(context.Background(), []string{"--help"}, io.NopCloser(strings.NewReader("")), &stdout, &stderr)
	if code != 0 || !strings.Contains(stdout.String(), "serve|stdio|check") || stderr.Len() != 0 {
		t.Fatalf("code=%d stdout=%q stderr=%q", code, stdout.String(), stderr.String())
	}
}

func TestCheckSuccessUsesStdout(t *testing.T) {
	var stdout, stderr bytes.Buffer
	code := run(context.Background(), []string{
		"check",
		"--cf-client-id-path", writeMode0600(t, "client-id", "test-client-id"),
		"--cf-client-secret-path", writeMode0600(t, "client-secret", "test-client-secret"),
		"--local-token-path", writeMode0600(t, "local-token", "test-local-token"),
	}, io.NopCloser(strings.NewReader("")), &stdout, &stderr)
	if code != 0 || stdout.String() != "configuration valid\n" || stderr.Len() != 0 {
		t.Fatalf("code=%d stdout=%q stderr=%q", code, stdout.String(), stderr.String())
	}
}

func TestRelayServerErrorLogUsesInjectedStderr(t *testing.T) {
	var stderr, global bytes.Buffer
	defaultWriter := log.Writer()
	log.SetOutput(&global)
	t.Cleanup(func() { log.SetOutput(defaultWriter) })

	server := newRelayHTTPServer(config.RelayConfig{}, http.NotFoundHandler(), log.New(&stderr, "", 0))
	server.ErrorLog.Print("server diagnostic")

	if !strings.Contains(stderr.String(), "server diagnostic") {
		t.Fatalf("stderr %q, want server diagnostic", stderr.String())
	}
	if global.Len() != 0 {
		t.Fatalf("global logger received %q", global.String())
	}
}

func TestRelayServerDoesNotApplyWholeResponseWriteDeadline(t *testing.T) {
	cfg := config.RelayConfig{
		BindHost:          "127.0.0.1",
		Port:              8765,
		HeaderReadTimeout: 30 * time.Second,
		ReadTimeout:       5 * time.Minute,
	}
	server := newRelayHTTPServer(cfg, http.NotFoundHandler(), log.New(io.Discard, "", 0))
	if server.WriteTimeout != 0 {
		t.Fatalf("WriteTimeout = %s, want no whole-response deadline", server.WriteTimeout)
	}
	if server.ReadHeaderTimeout != cfg.HeaderReadTimeout {
		t.Fatalf("ReadHeaderTimeout = %s, want %s", server.ReadHeaderTimeout, cfg.HeaderReadTimeout)
	}
}
