package benchmark

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"log"
	"net"
	"net/http"
	"net/http/httptest"
	"net/url"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"sort"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/veridian69/cairn/cairn-mcp/internal/config"
	"github.com/veridian69/cairn/cairn-mcp/internal/credentials"
	"github.com/veridian69/cairn/cairn-mcp/internal/relay"
	"github.com/veridian69/cairn/cairn-mcp/internal/upstream"
)

const controlledThresholdMS = 50.0

const (
	liveWarmThresholdMS = 750.0
	liveColdThresholdMS = 1500.0
)

// correlationTimeout bounds the wait for one JSON-RPC reply so a live run
// cannot hang on an unroutable or mismatched reply; it matches the live HTTP
// client timeout.
const correlationTimeout = 30 * time.Second

const (
	coldStartAttempts   = 3
	coldHealthTimeout   = 5 * time.Second
	maxChildStderrBytes = 4096
	maxDiagnosticBytes  = 256
)

const (
	controlledClientID     = "benchmark-client-id"
	controlledClientSecret = "benchmark-client-secret"
	controlledLocalToken   = "benchmark-local-token"
)

type Options struct {
	Mode               string
	Samples            int
	ConfirmLive        bool
	RelayURL           string
	UpstreamURL        string
	LocalTokenPath     string
	CFClientIDPath     string
	CFClientSecretPath string
	BinaryPath         string
}

type Environment struct {
	GoVersion string `json:"go_version"`
	GOOS      string `json:"goos"`
	GOARCH    string `json:"goarch"`
}

type Report struct {
	SchemaVersion      string      `json:"schema_version"`
	Mode               string      `json:"mode"`
	Environment        Environment `json:"environment"`
	Samples            int         `json:"samples"`
	DirectP50MS        *float64    `json:"direct_p50_ms,omitempty"`
	DirectP95MS        *float64    `json:"direct_p95_ms,omitempty"`
	RelayP50MS         *float64    `json:"relay_p50_ms,omitempty"`
	RelayP95MS         *float64    `json:"relay_p95_ms,omitempty"`
	RelayOverheadP50MS *float64    `json:"relay_overhead_p50_ms,omitempty"`
	RelayOverheadP95MS *float64    `json:"relay_overhead_p95_ms,omitempty"`
	ThresholdMS        *float64    `json:"threshold_ms,omitempty"`
	WarmP50MS          *float64    `json:"warm_initialize_get_status_p50_ms,omitempty"`
	WarmP95MS          *float64    `json:"warm_initialize_get_status_p95_ms,omitempty"`
	ColdP50MS          *float64    `json:"cold_start_initialize_get_status_p50_ms,omitempty"`
	ColdP95MS          *float64    `json:"cold_start_initialize_get_status_p95_ms,omitempty"`
	WarmThresholdMS    *float64    `json:"warm_threshold_ms,omitempty"`
	ColdThresholdMS    *float64    `json:"cold_threshold_ms,omitempty"`
	Passed             bool        `json:"passed"`
}

type liveDependencies struct {
	readToken   func(string) ([]byte, error)
	measureWarm func(context.Context, Options, []byte) (time.Duration, error)
	measureCold func(context.Context, Options, []byte) (time.Duration, error)
}

type commandFactory func(context.Context, string, ...string) *exec.Cmd

func PrintUsage(writer io.Writer) {
	fmt.Fprintln(writer, "cairn-mcp benchmark [options]")
	fmt.Fprintln(writer)
	fmt.Fprintln(writer, "  --mode controlled|live (default controlled)")
	fmt.Fprintln(writer, "  --samples int (default 20; live maximum 100)")
	fmt.Fprintln(writer, "  --confirm-live (required for live mode)")
	fmt.Fprintln(writer, "  --relay-url URL (live mode; loopback HTTP /mcp only)")
	fmt.Fprintln(writer, "  --upstream-url URL (live mode; HTTPS only)")
	fmt.Fprintln(writer, "  --local-token-path path")
	fmt.Fprintln(writer, "  --cf-client-id-path path")
	fmt.Fprintln(writer, "  --cf-client-secret-path path")
	fmt.Fprintln(writer, "  --binary path (live mode; defaults to the running executable)")
}

func ParseOptions(argv []string) (Options, error) {
	configDir, err := config.DefaultConfigDir()
	if err != nil {
		return Options{}, err
	}
	parser := flag.NewFlagSet("cairn-mcp benchmark", flag.ContinueOnError)
	parser.SetOutput(io.Discard)
	options := Options{}
	parser.StringVar(&options.Mode, "mode", "controlled", "Benchmark mode")
	parser.IntVar(&options.Samples, "samples", 20, "Number of samples")
	parser.BoolVar(&options.ConfirmLive, "confirm-live", false, "Confirm access to the live endpoint and credentials")
	parser.StringVar(&options.RelayURL, "relay-url", "http://127.0.0.1:8765/mcp", "Existing loopback relay URL")
	parser.StringVar(&options.UpstreamURL, "upstream-url", config.DefaultUpstreamURL, "Live upstream MCP URL")
	parser.StringVar(&options.LocalTokenPath, "local-token-path", filepath.Join(configDir, config.RelayTokenFileName), "Local bearer token path")
	parser.StringVar(&options.CFClientIDPath, "cf-client-id-path", filepath.Join(configDir, config.CFClientIDFileName), "Cloudflare client ID path")
	parser.StringVar(&options.CFClientSecretPath, "cf-client-secret-path", filepath.Join(configDir, config.CFClientSecretFileName), "Cloudflare client secret path")
	parser.StringVar(&options.BinaryPath, "binary", "", "cairn-mcp binary for cold starts (live mode; defaults to the running executable)")
	if err := parser.Parse(argv); err != nil {
		return Options{}, fmt.Errorf("cannot parse benchmark flags")
	}
	if options.Mode != "controlled" && options.Mode != "live" {
		return Options{}, fmt.Errorf("benchmark mode must be controlled or live")
	}
	if options.Samples < 1 || options.Samples > 1000 {
		return Options{}, fmt.Errorf("samples must be between 1 and 1000")
	}
	if options.Mode == "live" && !options.ConfirmLive {
		return Options{}, fmt.Errorf("live mode requires --confirm-live")
	}
	if options.Mode == "live" {
		if options.BinaryPath == "" {
			binaryPath, err := os.Executable()
			if err != nil {
				return Options{}, fmt.Errorf("resolve benchmark binary")
			}
			options.BinaryPath = binaryPath
		}
		if options.Samples > 100 {
			return Options{}, fmt.Errorf("live samples must be between 1 and 100")
		}
		if err := validateLiveOptions(options); err != nil {
			return Options{}, err
		}
	}
	return options, nil
}

func validateLiveOptions(options Options) error {
	relayURL, err := url.Parse(options.RelayURL)
	if err != nil || relayURL.Scheme != "http" || relayURL.Path != "/mcp" || relayURL.User != nil || relayURL.RawQuery != "" || relayURL.Fragment != "" {
		return fmt.Errorf("relay URL must be loopback HTTP at /mcp")
	}
	relayIP := net.ParseIP(relayURL.Hostname())
	if relayIP == nil || !relayIP.IsLoopback() {
		return fmt.Errorf("relay URL must be loopback HTTP at /mcp")
	}
	// The live benchmark never relaxes the upstream rules, so it asks the config
	// package rather than restating them: plain HTTP is refused outright.
	if err := config.ValidateUpstream(options.UpstreamURL, false); err != nil {
		return fmt.Errorf("live upstream URL must be absolute HTTPS")
	}
	for _, path := range []string{options.LocalTokenPath, options.CFClientIDPath, options.CFClientSecretPath, options.BinaryPath} {
		if !filepath.IsAbs(path) {
			return fmt.Errorf("live benchmark paths must be absolute")
		}
	}
	return nil
}

func Run(ctx context.Context, options Options) (Report, error) {
	if options.Samples < 1 || options.Samples > 1000 {
		return Report{}, fmt.Errorf("samples must be between 1 and 1000")
	}
	if options.Mode == "live" {
		return runLive(ctx, options, defaultLiveDependencies())
	}
	return runControlled(ctx, options.Samples)
}

func defaultLiveDependencies() liveDependencies {
	return liveDependencies{
		readToken: func(path string) ([]byte, error) {
			return credentials.ReadSecretFile(path, "local token", os.Geteuid(), credentials.MaxSecretBytes)
		},
		measureWarm: measureWarmLive,
		measureCold: measureColdLive,
	}
}

func measureWarmLive(ctx context.Context, options Options, token []byte) (time.Duration, error) {
	client := &http.Client{Timeout: 30 * time.Second}
	return measureMCPCall(ctx, options.RelayURL, token, client)
}

func measureColdLive(ctx context.Context, options Options, token []byte) (time.Duration, error) {
	return measureColdLiveWithCommand(ctx, options, token, exec.CommandContext)
}

// measureColdLiveWithCommand retries the spawn, never the MCP call: the loopback
// port is only reserved, so another process can take it between the reservation
// and the child's bind. A retry happens only when the child died during startup,
// before any JSON-RPC request was sent.
func measureColdLiveWithCommand(ctx context.Context, options Options, token []byte, newCommand commandFactory) (time.Duration, error) {
	var startupErr error
	for attempt := 0; attempt < coldStartAttempts; attempt++ {
		duration, err := coldStartAttempt(ctx, options, token, newCommand)
		if err == nil {
			return duration, nil
		}
		if !errors.Is(err, errChildStartup) || ctx.Err() != nil {
			return 0, err
		}
		startupErr = err
	}
	return 0, startupErr
}

func coldStartAttempt(ctx context.Context, options Options, token []byte, newCommand commandFactory) (time.Duration, error) {
	port, err := availableLoopbackPort()
	if err != nil {
		return 0, fmt.Errorf("reserve cold benchmark port")
	}
	arguments := []string{
		"serve",
		"--bind-host", "127.0.0.1",
		"--port", strconv.Itoa(port),
		"--upstream-url", options.UpstreamURL,
		"--local-token-path", options.LocalTokenPath,
		"--cf-client-id-path", options.CFClientIDPath,
		"--cf-client-secret-path", options.CFClientSecretPath,
	}
	command := newCommand(ctx, options.BinaryPath, arguments...)
	start := time.Now()
	child, err := startChild(command)
	if err != nil {
		return 0, fmt.Errorf("start cold benchmark relay")
	}
	defer child.stop()

	relayBaseURL := "http://127.0.0.1:" + strconv.Itoa(port)
	client := &http.Client{Timeout: 30 * time.Second}
	if err := waitForHealth(ctx, client, relayBaseURL+"/healthz", coldHealthTimeout, child); err != nil {
		return 0, err
	}
	if _, err := measureMCPCall(ctx, relayBaseURL+"/mcp", token, client); err != nil {
		return 0, fmt.Errorf("call cold benchmark relay: %w", err)
	}
	return time.Since(start), nil
}

func availableLoopbackPort() (int, error) {
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		return 0, err
	}
	defer listener.Close()
	return listener.Addr().(*net.TCPAddr).Port, nil
}

// errChildStartup marks a child that died during startup, before any JSON-RPC
// request was sent — the observable form of a lost port race — so the spawn may
// be retried without replaying a call.
var errChildStartup = errors.New("cold benchmark relay exited during startup")

type childProcess struct {
	command *exec.Cmd
	stderr  *boundedBuffer
	exited  chan struct{}
}

func startChild(command *exec.Cmd) (*childProcess, error) {
	stderr := &boundedBuffer{limit: maxChildStderrBytes}
	command.Stdout = io.Discard
	command.Stderr = stderr
	if err := command.Start(); err != nil {
		return nil, err
	}
	child := &childProcess{command: command, stderr: stderr, exited: make(chan struct{})}
	go func() {
		_ = command.Wait()
		close(child.exited)
	}()
	return child, nil
}

func (child *childProcess) stop() {
	_ = child.command.Process.Signal(os.Interrupt)
	select {
	case <-child.exited:
	case <-time.After(2 * time.Second):
		_ = child.command.Process.Kill()
		<-child.exited
	}
}

// failure keeps the child's own diagnostic while holding the module's no-paths
// rule: the child never prints credential values, and any path it does print is
// redacted before it reaches an error string.
func (child *childProcess) failure(reason string) string {
	diagnostic := redactPaths(child.stderr.lastLine())
	if diagnostic == "" {
		return reason
	}
	return reason + ": " + diagnostic
}

func waitForHealth(ctx context.Context, client *http.Client, healthURL string, timeout time.Duration, child *childProcess) error {
	deadline := time.NewTimer(timeout)
	defer deadline.Stop()
	ticker := time.NewTicker(10 * time.Millisecond)
	defer ticker.Stop()
	for {
		request, err := http.NewRequestWithContext(ctx, http.MethodGet, healthURL, nil)
		if err == nil {
			response, requestErr := client.Do(request)
			if requestErr == nil {
				_, _ = io.Copy(io.Discard, response.Body)
				_ = response.Body.Close()
				if response.StatusCode == http.StatusOK {
					return nil
				}
			}
		}
		select {
		case <-ctx.Done():
			return fmt.Errorf("cold benchmark relay cancelled")
		case <-child.exited:
			return fmt.Errorf("%w: %s", errChildStartup, child.failure("before readiness"))
		case <-deadline.C:
			return fmt.Errorf("cold benchmark relay %s", child.failure("did not become ready"))
		case <-ticker.C:
		}
	}
}

type boundedBuffer struct {
	mutex sync.Mutex
	limit int
	data  []byte
}

func (buffer *boundedBuffer) Write(chunk []byte) (int, error) {
	buffer.mutex.Lock()
	defer buffer.mutex.Unlock()
	if remaining := buffer.limit - len(buffer.data); remaining > 0 {
		if len(chunk) > remaining {
			buffer.data = append(buffer.data, chunk[:remaining]...)
		} else {
			buffer.data = append(buffer.data, chunk...)
		}
	}
	return len(chunk), nil
}

func (buffer *boundedBuffer) lastLine() string {
	buffer.mutex.Lock()
	defer buffer.mutex.Unlock()
	lines := strings.Split(strings.TrimSpace(string(buffer.data)), "\n")
	last := strings.TrimSpace(lines[len(lines)-1])
	if len(last) > maxDiagnosticBytes {
		last = last[:maxDiagnosticBytes]
	}
	return last
}

func redactPaths(diagnostic string) string {
	fields := strings.Fields(diagnostic)
	for index, field := range fields {
		if strings.HasPrefix(field, "/") || strings.HasPrefix(field, "~/") {
			fields[index] = "[path]"
		}
	}
	return strings.Join(fields, " ")
}

func runControlled(ctx context.Context, samples int) (Report, error) {
	tempDir, err := os.MkdirTemp("/tmp", "cairn-mcp-benchmark-")
	if err != nil {
		return Report{}, fmt.Errorf("create benchmark workspace")
	}
	defer os.RemoveAll(tempDir)

	clientIDPath, err := writeSecret(tempDir, "client-id", controlledClientID)
	if err != nil {
		return Report{}, err
	}
	clientSecretPath, err := writeSecret(tempDir, "client-secret", controlledClientSecret)
	if err != nil {
		return Report{}, err
	}
	localTokenPath, err := writeSecret(tempDir, "local-token", controlledLocalToken)
	if err != nil {
		return Report{}, err
	}

	upstream := httptest.NewServer(http.HandlerFunc(func(writer http.ResponseWriter, request *http.Request) {
		if request.Header.Get("CF-Access-Client-Id") != controlledClientID ||
			request.Header.Get("CF-Access-Client-Secret") != controlledClientSecret {
			writer.WriteHeader(http.StatusForbidden)
			return
		}
		writer.Header().Set("Content-Type", "application/json")
		_, _ = writer.Write([]byte(`{"jsonrpc":"2.0","id":1,"result":{}}`))
	}))
	defer upstream.Close()

	cfg := config.RelayConfig{
		UpstreamURL:          upstream.URL,
		LocalTokenPath:       localTokenPath,
		CFClientIDPath:       clientIDPath,
		CFClientSecretPath:   clientSecretPath,
		ConnectTimeout:       2 * time.Second,
		HeaderReadTimeout:    2 * time.Second,
		ReadTimeout:          2 * time.Second,
		PoolTimeout:          2 * time.Second,
		ShutdownGraceSeconds: time.Second,
	}
	store, err := credentials.NewRelayStore(clientIDPath, clientSecretPath, localTokenPath)
	if err != nil {
		return Report{}, fmt.Errorf("load benchmark credentials")
	}
	app := relay.NewRelayApp(cfg, store, nil, log.New(io.Discard, "", 0))
	app.SetStarted(true)
	defer app.Close()
	relayServer := httptest.NewServer(app)
	defer relayServer.Close()

	client := &http.Client{Timeout: 2 * time.Second}
	directDurations := make([]time.Duration, 0, samples)
	relayDurations := make([]time.Duration, 0, samples)
	overheads := make([]time.Duration, 0, samples)

	if _, err := timedRequest(ctx, client, upstream.URL, false); err != nil {
		return Report{}, fmt.Errorf("warm direct benchmark path")
	}
	if _, err := timedRequest(ctx, client, relayServer.URL+"/mcp", true); err != nil {
		return Report{}, fmt.Errorf("warm relay benchmark path")
	}

	for range samples {
		directDuration, err := timedRequest(ctx, client, upstream.URL, false)
		if err != nil {
			return Report{}, fmt.Errorf("direct benchmark request")
		}
		relayDuration, err := timedRequest(ctx, client, relayServer.URL+"/mcp", true)
		if err != nil {
			return Report{}, fmt.Errorf("relay benchmark request")
		}
		overhead := relayDuration - directDuration
		if overhead < 0 {
			overhead = 0
		}
		directDurations = append(directDurations, directDuration)
		relayDurations = append(relayDurations, relayDuration)
		overheads = append(overheads, overhead)
	}

	overheadP95 := durationMS(nearestRank(overheads, 0.95))
	return Report{
		SchemaVersion:      "1",
		Mode:               "controlled",
		Environment:        currentEnvironment(),
		Samples:            samples,
		DirectP50MS:        metric(durationMS(nearestRank(directDurations, 0.50))),
		DirectP95MS:        metric(durationMS(nearestRank(directDurations, 0.95))),
		RelayP50MS:         metric(durationMS(nearestRank(relayDurations, 0.50))),
		RelayP95MS:         metric(durationMS(nearestRank(relayDurations, 0.95))),
		RelayOverheadP50MS: metric(durationMS(nearestRank(overheads, 0.50))),
		RelayOverheadP95MS: metric(overheadP95),
		ThresholdMS:        metric(controlledThresholdMS),
		Passed:             overheadP95 <= controlledThresholdMS,
	}, nil
}

func runLive(ctx context.Context, options Options, dependencies liveDependencies) (Report, error) {
	if !options.ConfirmLive {
		return Report{}, fmt.Errorf("live benchmark is not confirmed")
	}
	token, err := dependencies.readToken(options.LocalTokenPath)
	if err != nil {
		return Report{}, fmt.Errorf("load live benchmark token")
	}
	warmDurations := make([]time.Duration, 0, options.Samples)
	coldDurations := make([]time.Duration, 0, options.Samples)
	for range options.Samples {
		warm, err := dependencies.measureWarm(ctx, options, token)
		if err != nil {
			return liveReport(warmDurations, coldDurations, false), fmt.Errorf("measure warm live benchmark: %w", err)
		}
		cold, err := dependencies.measureCold(ctx, options, token)
		if err != nil {
			return liveReport(warmDurations, coldDurations, false), fmt.Errorf("measure cold live benchmark: %w", err)
		}
		warmDurations = append(warmDurations, warm)
		coldDurations = append(coldDurations, cold)
	}
	return liveReport(warmDurations, coldDurations, true), nil
}

func liveReport(warmDurations, coldDurations []time.Duration, complete bool) Report {
	report := Report{
		SchemaVersion:   "1",
		Mode:            "live",
		Environment:     currentEnvironment(),
		Samples:         len(warmDurations),
		WarmThresholdMS: metric(liveWarmThresholdMS),
		ColdThresholdMS: metric(liveColdThresholdMS),
	}
	if len(warmDurations) == 0 || len(coldDurations) == 0 {
		return report
	}
	warmP95 := durationMS(nearestRank(warmDurations, 0.95))
	coldP95 := durationMS(nearestRank(coldDurations, 0.95))
	report.WarmP50MS = metric(durationMS(nearestRank(warmDurations, 0.50)))
	report.WarmP95MS = metric(warmP95)
	report.ColdP50MS = metric(durationMS(nearestRank(coldDurations, 0.50)))
	report.ColdP95MS = metric(coldP95)
	report.Passed = complete && warmP95 <= liveWarmThresholdMS && coldP95 <= liveColdThresholdMS
	return report
}

func timedRequest(ctx context.Context, client *http.Client, target string, throughRelay bool) (time.Duration, error) {
	payload := []byte(`{"jsonrpc":"2.0","id":1,"method":"ping"}`)
	request, err := http.NewRequestWithContext(ctx, http.MethodPost, target, bytes.NewReader(payload))
	if err != nil {
		return 0, err
	}
	request.Header.Set("Accept", "application/json, text/event-stream")
	request.Header.Set("Content-Type", "application/json")
	if throughRelay {
		request.Header.Set("Authorization", "Bearer "+controlledLocalToken)
	} else {
		request.Header.Set("CF-Access-Client-Id", controlledClientID)
		request.Header.Set("CF-Access-Client-Secret", controlledClientSecret)
	}
	start := time.Now()
	response, err := client.Do(request)
	duration := time.Since(start)
	if err != nil {
		return 0, err
	}
	defer response.Body.Close()
	_, _ = io.Copy(io.Discard, response.Body)
	if response.StatusCode != http.StatusOK {
		return 0, fmt.Errorf("unexpected benchmark response")
	}
	return duration, nil
}

type emptyAccessSource struct{}

func (emptyAccessSource) AccessSnapshot() credentials.AccessCredentials {
	return credentials.AccessCredentials{}
}

type bearerRoundTripper struct {
	base  http.RoundTripper
	token []byte
}

func (transport bearerRoundTripper) RoundTrip(request *http.Request) (*http.Response, error) {
	cloned := request.Clone(request.Context())
	cloned.Header.Set("Authorization", "Bearer "+string(transport.token))
	return transport.base.RoundTrip(cloned)
}

func measureMCPCall(ctx context.Context, endpoint string, token []byte, client *http.Client) (time.Duration, error) {
	return measureMCPCallWithTimeout(ctx, endpoint, token, client, correlationTimeout)
}

func measureMCPCallWithTimeout(ctx context.Context, endpoint string, token []byte, client *http.Client, timeout time.Duration) (time.Duration, error) {
	baseTransport := client.Transport
	if baseTransport == nil {
		baseTransport = http.DefaultTransport
	}
	authenticatedClient := *client
	authenticatedClient.Transport = bearerRoundTripper{base: baseTransport, token: token}

	session, err := upstream.NewSession(endpoint, &authenticatedClient, emptyAccessSource{}, upstream.WithoutLegacyListener())
	if err != nil {
		return 0, fmt.Errorf("create benchmark MCP session")
	}
	start := time.Now()
	initialize := json.RawMessage(`{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-11-25","capabilities":{},"clientInfo":{"name":"cairn-mcp-benchmark","version":"1"}}}`)
	if err := sendAndReceive(ctx, session, initialize, "1", timeout); err != nil {
		_ = session.Close(context.Background())
		return 0, err
	}
	initialized := json.RawMessage(`{"jsonrpc":"2.0","method":"notifications/initialized"}`)
	if err := session.Send(ctx, initialized); err != nil {
		_ = session.Close(context.Background())
		return 0, fmt.Errorf("send benchmark initialized notification")
	}
	getStatus := json.RawMessage(`{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"get_status","arguments":{}}}`)
	if err := sendAndReceive(ctx, session, getStatus, "2", timeout); err != nil {
		_ = session.Close(context.Background())
		return 0, err
	}
	duration := time.Since(start)
	_ = session.Close(ctx)
	return duration, nil
}

func sendAndReceive(ctx context.Context, session *upstream.Session, message json.RawMessage, expectedID string, timeout time.Duration) error {
	if err := session.Send(ctx, message); err != nil {
		return fmt.Errorf("send benchmark MCP request")
	}
	deadline := time.NewTimer(timeout)
	defer deadline.Stop()
	for {
		select {
		case event, open := <-session.Events():
			if !open || event.Err != nil {
				return fmt.Errorf("receive benchmark MCP response")
			}
			var response struct {
				ID     json.RawMessage `json:"id"`
				Method string          `json:"method"`
				Result json.RawMessage `json:"result"`
				Error  json.RawMessage `json:"error"`
			}
			if err := json.Unmarshal(event.Message, &response); err != nil {
				return fmt.Errorf("decode benchmark MCP response")
			}
			if response.Method != "" {
				continue
			}
			if unroutableID(response.ID) {
				return fmt.Errorf("benchmark MCP response is unroutable")
			}
			if string(bytes.TrimSpace(response.ID)) != expectedID {
				continue
			}
			if len(response.Error) > 0 && string(response.Error) != "null" {
				return fmt.Errorf("benchmark MCP request failed")
			}
			if len(response.Result) == 0 {
				return fmt.Errorf("benchmark MCP response has no result")
			}
			return nil
		case <-deadline.C:
			return fmt.Errorf("benchmark MCP response did not arrive")
		case <-ctx.Done():
			return fmt.Errorf("benchmark MCP request cancelled")
		}
	}
}

func unroutableID(id json.RawMessage) bool {
	trimmed := string(bytes.TrimSpace(id))
	return trimmed == "" || trimmed == "null"
}

func writeSecret(directory, name, value string) (string, error) {
	path := filepath.Join(directory, name)
	if err := os.WriteFile(path, []byte(value+"\n"), 0o600); err != nil {
		return "", fmt.Errorf("write benchmark credential")
	}
	return path, nil
}

func nearestRank(values []time.Duration, percentile float64) time.Duration {
	ordered := append([]time.Duration(nil), values...)
	sort.Slice(ordered, func(left, right int) bool { return ordered[left] < ordered[right] })
	rank := int(float64(len(ordered))*percentile + 0.999999999)
	if rank < 1 {
		rank = 1
	}
	return ordered[rank-1]
}

func durationMS(value time.Duration) float64 {
	return float64(value.Nanoseconds()) / float64(time.Millisecond)
}

func metric(value float64) *float64 {
	return &value
}

func currentEnvironment() Environment {
	return Environment{GoVersion: runtime.Version(), GOOS: runtime.GOOS, GOARCH: runtime.GOARCH}
}
