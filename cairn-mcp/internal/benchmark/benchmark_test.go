package benchmark

import (
	"context"
	"encoding/json"
	"errors"
	"net/http"
	"net/http/httptest"
	"os"
	"os/exec"
	"path/filepath"
	"reflect"
	"strconv"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"github.com/veridian69/cairn/cairn-mcp/internal/config"
)

func TestMeasureColdLiveStartsRelayAndMeasuresCall(t *testing.T) {
	options := Options{
		UpstreamURL:        "https://cairn.example/mcp",
		LocalTokenPath:     "/tmp/relay-token",
		CFClientIDPath:     "/tmp/client-id",
		CFClientSecretPath: "/tmp/client-secret",
	}
	factory := func(ctx context.Context, _ string, arguments ...string) *exec.Cmd {
		command := exec.CommandContext(ctx, os.Args[0], append([]string{"-test.run=TestColdBenchmarkHelperProcess", "--"}, arguments...)...)
		command.Env = append(os.Environ(), "CAIRN_BENCHMARK_HELPER=1")
		return command
	}

	duration, err := measureColdLiveWithCommand(context.Background(), options, []byte("live-local-token"), factory)
	if err != nil {
		t.Fatal(err)
	}
	if duration <= 0 {
		t.Fatalf("duration = %s, want positive", duration)
	}
}

func TestMeasureColdLiveFailsFastAndReportsChildDiagnostic(t *testing.T) {
	options := Options{
		UpstreamURL:        "https://cairn.example/mcp",
		LocalTokenPath:     "/tmp/relay-token",
		CFClientIDPath:     "/tmp/client-id",
		CFClientSecretPath: "/tmp/client-secret",
	}
	factory := func(ctx context.Context, _ string, arguments ...string) *exec.Cmd {
		return helperCommand(ctx, "exit", arguments)
	}

	_, err := measureColdLiveWithCommand(context.Background(), options, []byte("live-local-token"), factory)
	if err == nil {
		t.Fatal("measureColdLiveWithCommand() error = nil, want failure")
	}
	// The sentinel is produced only by the child-exit branch of waitForHealth, so
	// this pins the fail-fast mechanism without asserting on the clock.
	if !errors.Is(err, errChildStartup) {
		t.Fatalf("error = %v, want the fail-fast child-exit path", err)
	}
	if !strings.Contains(err.Error(), "cold-benchmark-child-refused") {
		t.Fatalf("error = %v, want captured child stderr diagnostic", err)
	}
	if strings.Contains(err.Error(), "/tmp/cairn-mcp-benchmark-secret-dir") {
		t.Fatalf("error disclosed a child path: %v", err)
	}
}

func TestMeasureColdLiveRetriesWhenChildLosesThePortRace(t *testing.T) {
	options := Options{
		UpstreamURL:        "https://cairn.example/mcp",
		LocalTokenPath:     "/tmp/relay-token",
		CFClientIDPath:     "/tmp/client-id",
		CFClientSecretPath: "/tmp/client-secret",
	}
	var attempts atomic.Int32
	factory := func(ctx context.Context, _ string, arguments ...string) *exec.Cmd {
		if attempts.Add(1) == 1 {
			return helperCommand(ctx, "bind-conflict", arguments)
		}
		return helperCommand(ctx, "healthy", arguments)
	}

	duration, err := measureColdLiveWithCommand(context.Background(), options, []byte("live-local-token"), factory)
	if err != nil {
		t.Fatal(err)
	}
	if duration <= 0 {
		t.Fatalf("duration = %s, want positive", duration)
	}
	if got := attempts.Load(); got != 2 {
		t.Fatalf("spawn attempts = %d, want 2", got)
	}
}

func helperCommand(ctx context.Context, mode string, arguments []string) *exec.Cmd {
	command := exec.CommandContext(ctx, os.Args[0], append([]string{"-test.run=TestColdBenchmarkHelperProcess", "--"}, arguments...)...)
	command.Env = append(os.Environ(), "CAIRN_BENCHMARK_HELPER=1", "CAIRN_BENCHMARK_HELPER_MODE="+mode)
	return command
}

func TestColdBenchmarkHelperProcess(t *testing.T) {
	if os.Getenv("CAIRN_BENCHMARK_HELPER") != "1" {
		return
	}
	switch os.Getenv("CAIRN_BENCHMARK_HELPER_MODE") {
	case "exit":
		os.Stderr.WriteString("cold-benchmark-child-refused reading /tmp/cairn-mcp-benchmark-secret-dir/token\n")
		os.Exit(1)
	case "bind-conflict":
		os.Stderr.WriteString("listen error: listen tcp 127.0.0.1:0: bind: address already in use\n")
		os.Exit(1)
	}
	port := argumentValue(os.Args, "--port")
	if _, err := strconv.Atoi(port); err != nil {
		os.Exit(2)
	}
	server := &http.Server{
		Addr: "127.0.0.1:" + port,
		Handler: http.HandlerFunc(func(writer http.ResponseWriter, request *http.Request) {
			if request.URL.Path == "/healthz" {
				writer.WriteHeader(http.StatusOK)
				return
			}
			if request.URL.Path != "/mcp" || request.Header.Get("Authorization") != "Bearer live-local-token" {
				writer.WriteHeader(http.StatusForbidden)
				return
			}
			if request.Method == http.MethodGet {
				writer.WriteHeader(http.StatusMethodNotAllowed)
				return
			}
			if request.Method == http.MethodDelete {
				writer.WriteHeader(http.StatusNoContent)
				return
			}
			var message struct {
				Method string `json:"method"`
			}
			if err := json.NewDecoder(request.Body).Decode(&message); err != nil {
				writer.WriteHeader(http.StatusBadRequest)
				return
			}
			switch message.Method {
			case "initialize":
				writer.Header().Set("Content-Type", "application/json")
				writer.Header().Set("Mcp-Session-Id", "cold-benchmark-session")
				_, _ = writer.Write([]byte(`{"jsonrpc":"2.0","id":1,"result":{"protocolVersion":"2025-11-25","capabilities":{}}}`))
			case "notifications/initialized":
				writer.WriteHeader(http.StatusAccepted)
			case "tools/call":
				writer.Header().Set("Content-Type", "application/json")
				_, _ = writer.Write([]byte(`{"jsonrpc":"2.0","id":2,"result":{"content":[]}}`))
			default:
				writer.WriteHeader(http.StatusBadRequest)
			}
		}),
	}
	if err := server.ListenAndServe(); err != nil && err != http.ErrServerClosed {
		os.Exit(3)
	}
}

func argumentValue(arguments []string, name string) string {
	for index := 0; index+1 < len(arguments); index++ {
		if arguments[index] == name {
			return arguments[index+1]
		}
	}
	return ""
}

func TestMeasureMCPCallInitialisesAndCallsGetStatus(t *testing.T) {
	var mutex sync.Mutex
	methods := make([]string, 0, 4)
	server := httptest.NewServer(http.HandlerFunc(func(writer http.ResponseWriter, request *http.Request) {
		if request.Header.Get("Authorization") != "Bearer live-local-token" {
			t.Errorf("authorization header = %q", request.Header.Get("Authorization"))
		}
		if request.Method == http.MethodGet {
			writer.WriteHeader(http.StatusMethodNotAllowed)
			return
		}
		if request.Method == http.MethodDelete {
			mutex.Lock()
			methods = append(methods, "DELETE")
			mutex.Unlock()
			writer.WriteHeader(http.StatusNoContent)
			return
		}

		var message struct {
			Method string `json:"method"`
		}
		if err := json.NewDecoder(request.Body).Decode(&message); err != nil {
			t.Errorf("decode request: %v", err)
			writer.WriteHeader(http.StatusBadRequest)
			return
		}
		mutex.Lock()
		methods = append(methods, message.Method)
		mutex.Unlock()

		switch message.Method {
		case "initialize":
			writer.Header().Set("Content-Type", "application/json")
			writer.Header().Set("Mcp-Session-Id", "benchmark-session")
			_, _ = writer.Write([]byte(`{"jsonrpc":"2.0","id":1,"result":{"protocolVersion":"2025-11-25","capabilities":{}}}`))
		case "notifications/initialized":
			writer.WriteHeader(http.StatusAccepted)
		case "tools/call":
			if request.Header.Get("Mcp-Session-Id") != "benchmark-session" || request.Header.Get("Mcp-Protocol-Version") != "2025-11-25" {
				t.Errorf("MCP headers session=%q protocol=%q", request.Header.Get("Mcp-Session-Id"), request.Header.Get("Mcp-Protocol-Version"))
			}
			writer.Header().Set("Content-Type", "application/json")
			_, _ = writer.Write([]byte(`{"jsonrpc":"2.0","id":2,"result":{"content":[]}}`))
		default:
			writer.WriteHeader(http.StatusBadRequest)
		}
	}))
	defer server.Close()

	duration, err := measureMCPCall(context.Background(), server.URL, []byte("live-local-token"), server.Client())
	if err != nil {
		t.Fatal(err)
	}
	if duration <= 0 {
		t.Fatalf("duration = %s, want positive", duration)
	}
	mutex.Lock()
	defer mutex.Unlock()
	want := []string{"initialize", "notifications/initialized", "tools/call", "DELETE"}
	if !reflect.DeepEqual(methods, want) {
		t.Fatalf("methods = %#v, want %#v", methods, want)
	}
}

func TestMeasureMCPCallCorrelatesResponsesWithoutLegacyListenerAndIgnoresCloseFailure(t *testing.T) {
	var getCalls atomic.Int32
	server := httptest.NewServer(http.HandlerFunc(func(writer http.ResponseWriter, request *http.Request) {
		if request.Method == http.MethodGet {
			getCalls.Add(1)
			writer.WriteHeader(http.StatusBadGateway)
			return
		}
		if request.Method == http.MethodDelete {
			writer.WriteHeader(http.StatusInternalServerError)
			return
		}
		var message struct {
			Method string `json:"method"`
		}
		if err := json.NewDecoder(request.Body).Decode(&message); err != nil {
			writer.WriteHeader(http.StatusBadRequest)
			return
		}
		writer.Header().Set("Content-Type", "application/json")
		switch message.Method {
		case "initialize":
			writer.Header().Set("Mcp-Session-Id", "benchmark-session")
			_, _ = writer.Write([]byte(`{"jsonrpc":"2.0","id":1,"result":{"protocolVersion":"2025-11-25","capabilities":{}}}`))
		case "notifications/initialized":
			_, _ = writer.Write([]byte(`{"jsonrpc":"2.0","method":"notifications/progress","params":{}}`))
		case "tools/call":
			_, _ = writer.Write([]byte(`{"jsonrpc":"2.0","id":2,"result":{"content":[]}}`))
		default:
			writer.WriteHeader(http.StatusBadRequest)
		}
	}))
	defer server.Close()

	if _, err := measureMCPCall(context.Background(), server.URL, []byte("live-local-token"), server.Client()); err != nil {
		t.Fatal(err)
	}
	if calls := getCalls.Load(); calls != 0 {
		t.Fatalf("legacy GET calls = %d, want 0", calls)
	}
}

func TestMeasureMCPCallBoundsUnroutableAndMismatchedReplies(t *testing.T) {
	cases := []struct {
		name         string
		initialize   string
		initializeOK bool
	}{
		{name: "null id error reply", initialize: `{"jsonrpc":"2.0","id":null,"error":{"code":-32600,"message":"invalid request"}}`},
		{name: "string typed id", initialize: `{"jsonrpc":"2.0","id":"1","result":{"protocolVersion":"2025-11-25","capabilities":{}}}`},
		{name: "accepted initialize without body", initializeOK: true},
	}
	for _, testCase := range cases {
		t.Run(testCase.name, func(t *testing.T) {
			server := httptest.NewServer(http.HandlerFunc(func(writer http.ResponseWriter, request *http.Request) {
				if request.Method != http.MethodPost {
					writer.WriteHeader(http.StatusNoContent)
					return
				}
				if testCase.initializeOK {
					writer.WriteHeader(http.StatusAccepted)
					return
				}
				writer.Header().Set("Content-Type", "application/json")
				_, _ = writer.Write([]byte(testCase.initialize))
			}))
			defer server.Close()

			type outcome struct {
				err error
			}
			done := make(chan outcome, 1)
			go func() {
				_, err := measureMCPCallWithTimeout(context.Background(), server.URL, []byte("live-local-token"), server.Client(), 200*time.Millisecond)
				done <- outcome{err: err}
			}()
			select {
			case result := <-done:
				if result.err == nil {
					t.Fatal("measureMCPCall() error = nil, want bounded correlation failure")
				}
			case <-time.After(2 * time.Second):
				t.Fatal("measureMCPCall() did not return; correlation wait is unbounded")
			}
		})
	}
}

func TestRunLiveReportsWarmAndColdP95(t *testing.T) {
	warm := []time.Duration{100 * time.Millisecond, 200 * time.Millisecond, 300 * time.Millisecond}
	cold := []time.Duration{900 * time.Millisecond, 1100 * time.Millisecond, 1400 * time.Millisecond}
	warmIndex, coldIndex := 0, 0
	dependencies := liveDependencies{
		readToken: func(string) ([]byte, error) { return []byte("live-local-token"), nil },
		measureWarm: func(context.Context, Options, []byte) (time.Duration, error) {
			value := warm[warmIndex]
			warmIndex++
			return value, nil
		},
		measureCold: func(context.Context, Options, []byte) (time.Duration, error) {
			value := cold[coldIndex]
			coldIndex++
			return value, nil
		},
	}

	report, err := runLive(context.Background(), Options{
		Mode:           "live",
		Samples:        3,
		ConfirmLive:    true,
		LocalTokenPath: "/not-read-by-test",
	}, dependencies)
	if err != nil {
		t.Fatal(err)
	}
	if report.Mode != "live" || report.WarmP95MS == nil || *report.WarmP95MS != 300 || report.ColdP95MS == nil || *report.ColdP95MS != 1400 || !report.Passed {
		t.Fatalf("unexpected live report: %+v", report)
	}
}

func TestRunLiveRetainsCollectedSamplesWhenAMeasurementFails(t *testing.T) {
	warm := []time.Duration{100 * time.Millisecond, 200 * time.Millisecond}
	cold := []time.Duration{900 * time.Millisecond, 1000 * time.Millisecond}
	warmIndex, coldIndex := 0, 0
	dependencies := liveDependencies{
		readToken: func(string) ([]byte, error) { return []byte("live-local-token"), nil },
		measureWarm: func(context.Context, Options, []byte) (time.Duration, error) {
			if warmIndex >= len(warm) {
				return 0, errors.New("warm measurement failed")
			}
			value := warm[warmIndex]
			warmIndex++
			return value, nil
		},
		measureCold: func(context.Context, Options, []byte) (time.Duration, error) {
			value := cold[coldIndex]
			coldIndex++
			return value, nil
		},
	}

	report, err := runLive(context.Background(), Options{Mode: "live", Samples: 5, ConfirmLive: true}, dependencies)
	if err == nil {
		t.Fatal("runLive() error = nil, want failure")
	}
	if report.Samples != 2 {
		t.Fatalf("report.Samples = %d, want 2 retained samples", report.Samples)
	}
	if report.WarmP50MS == nil || report.ColdP50MS == nil {
		t.Fatalf("report discarded collected percentiles: %+v", report)
	}
	if report.Passed {
		t.Fatalf("partial report unexpectedly passed: %+v", report)
	}
}

func TestRunLiveReportsNoSamplesWhenTheFirstMeasurementFails(t *testing.T) {
	dependencies := liveDependencies{
		readToken: func(string) ([]byte, error) { return []byte("live-local-token"), nil },
		measureWarm: func(context.Context, Options, []byte) (time.Duration, error) {
			return 0, errors.New("warm measurement failed")
		},
		measureCold: func(context.Context, Options, []byte) (time.Duration, error) { return 0, nil },
	}

	report, err := runLive(context.Background(), Options{Mode: "live", Samples: 3, ConfirmLive: true}, dependencies)
	if err == nil {
		t.Fatal("runLive() error = nil, want failure")
	}
	if report.Samples != 0 || report.WarmP50MS != nil || report.Passed {
		t.Fatalf("unexpected empty live report: %+v", report)
	}
}

func TestLiveReportJSONOmitsControlledMetrics(t *testing.T) {
	report := Report{
		SchemaVersion:   "1",
		Mode:            "live",
		Samples:         1,
		WarmP50MS:       metric(100),
		WarmP95MS:       metric(100),
		ColdP50MS:       metric(500),
		ColdP95MS:       metric(500),
		WarmThresholdMS: metric(liveWarmThresholdMS),
		ColdThresholdMS: metric(liveColdThresholdMS),
		Passed:          true,
	}
	encoded, err := json.Marshal(report)
	if err != nil {
		t.Fatal(err)
	}
	for _, controlledField := range []string{"direct_p50_ms", "relay_p50_ms", "relay_overhead_p95_ms", "threshold_ms"} {
		if strings.Contains(string(encoded), `"`+controlledField+`"`) {
			t.Fatalf("live report contains controlled field %q: %s", controlledField, encoded)
		}
	}
}

func TestControlledReportJSONIncludesLegitimateZeroMetric(t *testing.T) {
	report := Report{
		SchemaVersion:      "1",
		Mode:               "controlled",
		Samples:            1,
		RelayOverheadP50MS: metric(0),
		ThresholdMS:        metric(controlledThresholdMS),
		Passed:             true,
	}
	encoded, err := json.Marshal(report)
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(string(encoded), `"relay_overhead_p50_ms":0`) {
		t.Fatalf("controlled report dropped zero metric: %s", encoded)
	}
}

func TestControlledBenchmarkReportIsSecretFreeWithoutWallClockAssertion(t *testing.T) {
	report, err := runControlled(context.Background(), 3)
	if err != nil {
		t.Fatal(err)
	}
	encoded, err := json.Marshal(report)
	if err != nil {
		t.Fatal(err)
	}
	if report.RelayOverheadP95MS == nil {
		t.Fatal("controlled report omitted relay overhead p95")
	}
	for _, forbidden := range []string{controlledClientID, controlledClientSecret, controlledLocalToken} {
		if strings.Contains(string(encoded), forbidden) {
			t.Fatalf("controlled report contained sentinel %q", forbidden)
		}
	}
}

func TestRunLiveFailsThresholdBudget(t *testing.T) {
	dependencies := liveDependencies{
		readToken:   func(string) ([]byte, error) { return []byte("live-local-token"), nil },
		measureWarm: func(context.Context, Options, []byte) (time.Duration, error) { return 751 * time.Millisecond, nil },
		measureCold: func(context.Context, Options, []byte) (time.Duration, error) { return 1501 * time.Millisecond, nil },
	}
	report, err := runLive(context.Background(), Options{Samples: 1, ConfirmLive: true}, dependencies)
	if err != nil {
		t.Fatal(err)
	}
	if report.Passed {
		t.Fatalf("report unexpectedly passed: %+v", report)
	}
}

func TestParseOptionsAcceptsExplicitLiveInputs(t *testing.T) {
	options, err := ParseOptions([]string{
		"--mode", "live",
		"--confirm-live",
		"--samples", "2",
		"--relay-url", "http://127.0.0.1:9876/mcp",
		"--upstream-url", "https://cairn.example/mcp",
		"--local-token-path", "/tmp/relay-token",
		"--cf-client-id-path", "/tmp/client-id",
		"--cf-client-secret-path", "/tmp/client-secret",
		"--binary", "/tmp/cairn-mcp",
	})
	if err != nil {
		t.Fatal(err)
	}
	if options.RelayURL != "http://127.0.0.1:9876/mcp" ||
		options.UpstreamURL != "https://cairn.example/mcp" ||
		options.BinaryPath != "/tmp/cairn-mcp" ||
		options.CFClientIDPath != "/tmp/client-id" ||
		options.CFClientSecretPath != "/tmp/client-secret" {
		t.Fatalf("unexpected live options: %+v", options)
	}
}

func TestParseOptionsDefaultsComeFromTheConfigPackage(t *testing.T) {
	home := t.TempDir()
	t.Setenv("HOME", home)

	options, err := ParseOptions(nil)
	if err != nil {
		t.Fatal(err)
	}
	configDir := filepath.Join(home, ".config", "cairn")
	if options.UpstreamURL != config.DefaultUpstreamURL {
		t.Fatalf("UpstreamURL = %q, want %q", options.UpstreamURL, config.DefaultUpstreamURL)
	}
	if want := filepath.Join(configDir, "relay-token"); options.LocalTokenPath != want {
		t.Fatalf("LocalTokenPath = %q, want %q", options.LocalTokenPath, want)
	}
	if want := filepath.Join(configDir, "cf-access-client-id"); options.CFClientIDPath != want {
		t.Fatalf("CFClientIDPath = %q, want %q", options.CFClientIDPath, want)
	}
	if want := filepath.Join(configDir, "cf-access-client-secret"); options.CFClientSecretPath != want {
		t.Fatalf("CFClientSecretPath = %q, want %q", options.CFClientSecretPath, want)
	}
}

func TestParseOptionsRefusesToGuessWhenHomeIsUnavailable(t *testing.T) {
	t.Setenv("HOME", "")

	options, err := ParseOptions(nil)
	if err == nil {
		t.Fatalf("ParseOptions() error = nil, want failure; options = %+v", options)
	}
	if strings.Contains(options.LocalTokenPath, "/root") {
		t.Fatalf("ParseOptions() fell back to a root credential path: %+v", options)
	}
}

func TestParseOptionsRejectsLiveUpstreamsTheConfigPackageRefuses(t *testing.T) {
	t.Setenv("HOME", t.TempDir())

	for _, upstream := range []string{
		"http://cairn.example/mcp",
		"https://user:pw@cairn.example/mcp",
		"https://cairn.example/mcp?x=1",
		"https://cairn.example/mcp#x",
		"ftp://cairn.example/mcp",
	} {
		if _, err := ParseOptions([]string{"--mode", "live", "--confirm-live", "--upstream-url", upstream}); err == nil {
			t.Fatalf("ParseOptions() accepted upstream %q", upstream)
		}
	}
}

func TestParseOptionsRejectsNonLoopbackLiveRelay(t *testing.T) {
	_, err := ParseOptions([]string{
		"--mode", "live",
		"--confirm-live",
		"--relay-url", "http://192.0.2.1:8765/mcp",
	})
	if err == nil || !strings.Contains(err.Error(), "loopback") {
		t.Fatalf("ParseOptions() error = %v", err)
	}
}

func TestRunRejectsZeroSamplesInsteadOfPanicking(t *testing.T) {
	_, err := Run(context.Background(), Options{Mode: "controlled", Samples: 0})
	if err == nil {
		t.Fatal("Run() error = nil, want validation failure for zero samples")
	}
}

func TestParseOptionsControlledModeDoesNotResolveBinaryPath(t *testing.T) {
	options, err := ParseOptions([]string{"--mode", "controlled"})
	if err != nil {
		t.Fatal(err)
	}
	if options.BinaryPath != "" {
		t.Fatalf("BinaryPath = %q, want empty for controlled mode (os.Executable() must not be consulted)", options.BinaryPath)
	}
}

func TestParseOptionsLiveModeStillResolvesDefaultBinaryPath(t *testing.T) {
	options, err := ParseOptions([]string{"--mode", "live", "--confirm-live"})
	if err != nil {
		t.Fatal(err)
	}
	if options.BinaryPath == "" || !strings.HasPrefix(options.BinaryPath, "/") {
		t.Fatalf("BinaryPath = %q, want a resolved absolute default for live mode", options.BinaryPath)
	}
}

func TestRunLiveRejectsUnavailableTokenWithoutDisclosingPath(t *testing.T) {
	missing := "/tmp/cairn-mcp-benchmark-missing-token"
	_, err := Run(context.Background(), Options{
		Mode:           "live",
		Samples:        1,
		ConfirmLive:    true,
		LocalTokenPath: missing,
	})
	if err == nil || !strings.Contains(err.Error(), "load live benchmark token") {
		t.Fatalf("Run() error = %v", err)
	}
	if strings.Contains(err.Error(), missing) {
		t.Fatalf("Run() error disclosed token path: %v", err)
	}
}
