package daemon

import (
	"bytes"
	"context"
	"errors"
	"fmt"
	"log"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
	"unicode"
	"unicode/utf8"

	"github.com/veridian69/cairn/a2a/internal/agent"
	"github.com/veridian69/cairn/a2a/internal/config"
	"github.com/veridian69/cairn/a2a/internal/model"
	"github.com/veridian69/cairn/a2a/internal/provider"
)

func TestFormatProviderError(t *testing.T) {
	t.Run("normal authentication error omits response body", func(t *testing.T) {
		err := &provider.RequestError{
			Category:     provider.ErrAuth,
			StatusCode:   401,
			ResponseBody: `{"error":"invalid token"}`,
		}

		got := formatProviderError("anthropic", "claude-sonnet", "secret", false, err)
		want := `provider="anthropic" model="claude-sonnet" status="401 Unauthorized" error="authentication"`
		if got != want {
			t.Fatalf("formatProviderError() = %q, want %q", got, want)
		}
	})

	t.Run("verbose error includes normalised response body", func(t *testing.T) {
		err := &provider.RequestError{
			Category:     provider.ErrMalformed,
			StatusCode:   422,
			ResponseBody: " first\r\n\tsecond ",
		}

		got := formatProviderError("openai", "gpt-test", "", true, err)
		want := `provider="openai" model="gpt-test" status="422 Unprocessable Entity" error="malformed" body="first second"`
		if got != want {
			t.Fatalf("formatProviderError() = %q, want %q", got, want)
		}
	})

	t.Run("configured key is redacted from response body", func(t *testing.T) {
		err := &provider.RequestError{
			Category:     provider.ErrAuth,
			StatusCode:   401,
			ResponseBody: "rejected sk-live-secret immediately",
		}

		got := formatProviderError("openai", "gpt-test", "sk-live-secret", true, err)
		want := `provider="openai" model="gpt-test" status="401 Unauthorized" error="authentication" body="rejected [REDACTED] immediately"`
		if got != want {
			t.Fatalf("formatProviderError() = %q, want %q", got, want)
		}
	})

	t.Run("transport URL key is redacted and status is unavailable", func(t *testing.T) {
		err := &provider.RequestError{
			Category: provider.ErrTransient,
			Cause: errors.New(
				`Get "https://api.example.test/v1?key=sk-transport-secret": connection reset`,
			),
		}

		got := formatProviderError("google", "gemini-test", "sk-transport-secret", false, err)
		want := `provider="google" model="gemini-test" status="unavailable" error="transient" cause="Get \"https://api.example.test/v1?key=[REDACTED]\": connection reset"`
		if got != want {
			t.Fatalf("formatProviderError() = %q, want %q", got, want)
		}
	})

	t.Run("control whitespace cannot create another physical line", func(t *testing.T) {
		err := &provider.RequestError{
			Category:     provider.ErrMalformed,
			StatusCode:   400,
			ResponseBody: "bad\r\n\trequest",
			Cause:        errors.New("decode\r\n\tfailed"),
		}

		got := formatProviderError("provider\r\nname", "model\tname", "", true, err)
		if strings.ContainsAny(got, "\r\n\t") {
			t.Fatalf("formatProviderError() contains physical control whitespace: %q", got)
		}
	})
}

func TestFormatProviderErrorCategories(t *testing.T) {
	tests := []struct {
		name string
		err  error
		want string
	}{
		{name: "authentication", err: provider.ErrAuth, want: "authentication"},
		{name: "transient", err: provider.ErrTransient, want: "transient"},
		{name: "malformed", err: provider.ErrMalformed, want: "malformed"},
		{name: "cancelled", err: context.Canceled, want: "cancelled"},
		{name: "unknown", err: errors.New("other"), want: "unknown"},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			got := formatProviderError("test", "test", "", false, tt.err)
			want := `error="` + tt.want + `"`
			if !strings.Contains(got, want) {
				t.Fatalf("formatProviderError() = %q, want field %q", got, want)
			}
		})
	}
}

func TestFormatProviderErrorIncludesDirectContextCause(t *testing.T) {
	tests := []struct {
		name      string
		err       error
		wantCause string
	}{
		{
			name:      "cancelled",
			err:       context.Canceled,
			wantCause: "context canceled",
		},
		{
			name:      "deadline exceeded",
			err:       context.DeadlineExceeded,
			wantCause: "context deadline exceeded",
		},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			got := formatProviderError("test", "test-model", "", false, tt.err)
			want := `provider="test" model="test-model" status="unavailable" error="cancelled" cause="` +
				tt.wantCause + `"`
			if got != want {
				t.Fatalf("formatProviderError() = %q, want %q", got, want)
			}
		})
	}
}

func TestSanitiseDiagnostic(t *testing.T) {
	t.Run("long multibyte value stays valid and within total byte cap", func(t *testing.T) {
		got := sanitiseDiagnostic(strings.Repeat("é", 600), "")
		if !utf8.ValidString(got) {
			t.Fatalf("sanitiseDiagnostic() returned invalid UTF-8: %q", got)
		}
		if len(got) > 1024 {
			t.Fatalf("sanitiseDiagnostic() length = %d bytes, want at most 1024", len(got))
		}
		if !strings.HasSuffix(got, "…") {
			t.Fatalf("sanitiseDiagnostic() = %q, want ellipsis suffix", got)
		}
	})

	t.Run("redaction happens before truncation", func(t *testing.T) {
		got := sanitiseDiagnostic(strings.Repeat("a", 1019)+"SECRET", "SECRET")
		if strings.Contains(got, "SE") {
			t.Fatalf("sanitiseDiagnostic() exposed a truncated key prefix: %q", got)
		}
		if !strings.HasSuffix(got, "…") {
			t.Fatalf("sanitiseDiagnostic() = %q, want ellipsis suffix", got)
		}
	})

	t.Run("empty API key is not replaced", func(t *testing.T) {
		got := sanitiseDiagnostic("ordinary diagnostic", "")
		if got != "ordinary diagnostic" {
			t.Fatalf("sanitiseDiagnostic() = %q, want ordinary diagnostic", got)
		}
	})

	t.Run("invalid input becomes valid UTF-8", func(t *testing.T) {
		got := sanitiseDiagnostic(string([]byte{'a', 0xff, 'b'}), "")
		if !utf8.ValidString(got) {
			t.Fatalf("sanitiseDiagnostic() returned invalid UTF-8: %q", got)
		}
	})
}

func TestDaemonStartRedactsEnvironmentResolvedAgentAPIKey(t *testing.T) {
	const (
		envName     = "A2A_TEST_PROVIDER_DIAGNOSTIC_KEY"
		resolvedKey = "sk-effective-provider-key"
	)
	t.Setenv(envName, resolvedKey)

	dir := t.TempDir()
	configPath := filepath.Join(dir, "config.yaml")
	configYAML := fmt.Sprintf(`agents:
  agent:
    provider: mock
    model: test-model
    api_key: $%s
    system: test
    responsiveness: 1
defaults:
  context_window: 10
  responsiveness: 1
  control_contract: false
limits:
  per_agent_per_hour: 20
stream:
  data_dir: %q
memory:
  enabled: false
`, envName, filepath.Join(dir, "nats"))
	if err := os.WriteFile(configPath, []byte(configYAML), 0o600); err != nil {
		t.Fatalf("write config: %v", err)
	}

	cfg, err := config.Load(configPath)
	if err != nil {
		t.Fatalf("config.Load: %v", err)
	}

	oldFactory := newProvider
	defer func() { newProvider = oldFactory }()
	newProvider = func(name, apiKey, baseURL string) (provider.Provider, error) {
		if apiKey != resolvedKey {
			return nil, fmt.Errorf("provider API key = %q, want resolved environment value", apiKey)
		}
		return &provider.MockProvider{Err: &provider.RequestError{
			Category:     provider.ErrAuth,
			StatusCode:   401,
			ResponseBody: "rejected " + apiKey,
		}}, nil
	}

	var output bytes.Buffer
	originalWriter := log.Writer()
	originalFlags := log.Flags()
	originalPrefix := log.Prefix()
	log.SetOutput(&output)
	log.SetFlags(0)
	log.SetPrefix("")
	defer func() {
		log.SetOutput(originalWriter)
		log.SetFlags(originalFlags)
		log.SetPrefix(originalPrefix)
	}()

	d, err := New(cfg, dir, WithVerboseProviderErrors(true))
	if err != nil {
		t.Fatalf("New: %v", err)
	}
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	if err := d.Start(ctx); err != nil {
		d.Stop()
		t.Fatalf("Start: %v", err)
	}
	stopped := false
	defer func() {
		if !stopped {
			d.Stop()
		}
	}()

	worker := d.workers["agent"]
	if worker == nil {
		t.Fatal("agent worker was not started")
	}
	worker.runtime.Responsiveness = 1
	if err := d.Publish(ctx, model.NewMessage(
		model.Participant{ID: "human", Name: "operator", Kind: model.KindHuman},
		"trigger provider failure",
		nil,
	)); err != nil {
		t.Fatalf("Publish: %v", err)
	}

	deadline := time.Now().Add(5 * time.Second)
	for time.Now().Before(deadline) && worker.runtime.Snapshot().LastSeenSeq == 0 {
		time.Sleep(10 * time.Millisecond)
	}
	if worker.runtime.Snapshot().LastSeenSeq == 0 {
		t.Fatal("worker did not process the trigger message")
	}
	d.Stop()
	stopped = true

	got := output.String()
	if !strings.Contains(got, "[REDACTED]") {
		t.Fatalf("daemon log does not contain redaction marker: %q", got)
	}
	if strings.Contains(got, resolvedKey) {
		t.Fatalf("daemon log contains resolved API key: %q", got)
	}
}

func TestRunWorkerLogsProviderErrorDiagnostics(t *testing.T) {
	d, worker, cancel := newDiagnosticWorker(t, true, &provider.RequestError{
		Category:     provider.ErrAuth,
		StatusCode:   401,
		ResponseBody: "rejected resolved-secret",
	})
	worker.apiKey = "resolved-secret"

	output := captureWorkerLog(t, d, worker, model.Message{
		ID: "msg-0001", AuthorID: "human", AuthorName: "operator", Content: "hello",
	}, cancel)

	want := "[agent] skip msg-0001: provider-error: " +
		`provider="mock" model="test-model" status="401 Unauthorized" error="authentication" body="rejected [REDACTED]"` +
		"\n"
	if output != want {
		t.Fatalf("worker log = %q, want %q", output, want)
	}
}

func TestRunWorkerProviderErrorSanitisesHostileLogPrefix(t *testing.T) {
	tests := []struct {
		name    string
		verbose bool
		want    string
	}{
		{
			name:    "normal",
			verbose: false,
			want: "[agent forged] skip msg bad: provider-error: " +
				`provider="mock" model="test-model" status="401 Unauthorized" error="authentication"` +
				"\n",
		},
		{
			name:    "verbose",
			verbose: true,
			want: "[agent forged] skip msg bad: provider-error: " +
				`provider="mock" model="test-model" status="401 Unauthorized" error="authentication" body="rejected [REDACTED]"` +
				"\n",
		},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			d, worker, cancel := newDiagnosticWorker(t, tt.verbose, &provider.RequestError{
				Category:     provider.ErrAuth,
				StatusCode:   401,
				ResponseBody: "rejected resolved-secret",
			})
			worker.apiKey = "resolved-secret"
			worker.runtime.Participant.Name = "agent\r\nforged"

			output := captureWorkerLog(t, d, worker, model.Message{
				ID: "msg\r\nbad", AuthorID: "human", AuthorName: "operator", Content: "hello",
			}, cancel)

			if strings.Count(output, "\n") != 1 || !strings.HasSuffix(output, "\n") {
				t.Fatalf("worker log has more than one physical line: %q", output)
			}
			if output != tt.want {
				t.Fatalf("worker log = %q, want %q", output, tt.want)
			}
		})
	}
}

func TestRunWorkerProviderErrorSanitisesPrefixControlsAndDelimiters(t *testing.T) {
	d, worker, cancel := newDiagnosticWorker(t, false, &provider.RequestError{
		Category:   provider.ErrAuth,
		StatusCode: 401,
	})
	worker.apiKey = "resolved-secret"
	worker.runtime.Participant.Name = "agent\x1b]\x00[\a:"

	output := captureWorkerLog(t, d, worker, model.Message{
		ID: ":m\x1b]\x00[\ax", AuthorID: "human", AuthorName: "operator", Content: "hello",
	}, cancel)

	assertSingleSafeLogLine(t, output)
	wantPrefix := "[agent______] skip _m_____x: provider-error: "
	if !strings.HasPrefix(output, wantPrefix) {
		t.Fatalf("worker log prefix = %q, want prefix %q", output, wantPrefix)
	}
}

func TestRunWorkerProviderErrorRedactsFullMessageIDBeforeShortening(t *testing.T) {
	const apiKey = "sk-final-secret-value"
	d, worker, cancel := newDiagnosticWorker(t, true, &provider.RequestError{
		Category:     provider.ErrAuth,
		StatusCode:   401,
		ResponseBody: "rejected " + apiKey,
	})
	worker.apiKey = apiKey

	output := captureWorkerLog(t, d, worker, model.Message{
		ID: apiKey + ":tail", AuthorID: "human", AuthorName: "operator", Content: "hello",
	}, cancel)

	assertSingleSafeLogLine(t, output)
	if strings.Contains(output, apiKey) {
		t.Fatalf("worker log contains configured API key: %q", output)
	}
	if strings.Contains(output, apiKey[:8]) {
		t.Fatalf("worker log contains configured API key prefix: %q", output)
	}
	wantPrefix := "[agent] skip REDACTED: provider-error: "
	if !strings.HasPrefix(output, wantPrefix) {
		t.Fatalf("worker log prefix = %q, want prefix %q", output, wantPrefix)
	}
}

func TestRunWorkerRetainsOrdinarySkipLog(t *testing.T) {
	d, worker, cancel := newDiagnosticWorker(t, false, nil)
	worker.runtime.SetActive(false)

	output := captureWorkerLog(t, d, worker, model.Message{
		ID: "msg-0002", AuthorID: "human", AuthorName: "operator", Content: "hello",
	}, cancel)

	want := "[agent] skip msg-0002: paused\n"
	if output != want {
		t.Fatalf("worker log = %q, want %q", output, want)
	}
}

func assertSingleSafeLogLine(t *testing.T, output string) {
	t.Helper()

	if strings.Count(output, "\n") != 1 || !strings.HasSuffix(output, "\n") {
		t.Fatalf("worker log must have exactly one terminating newline: %q", output)
	}
	line := strings.TrimSuffix(output, "\n")
	if index := strings.IndexFunc(line, unicode.IsControl); index >= 0 {
		t.Fatalf("worker log contains a raw control character at byte %d: %q", index, output)
	}
}

func newDiagnosticWorker(
	t *testing.T,
	verbose bool,
	providerErr error,
) (*Daemon, *agentWorker, context.CancelFunc) {
	t.Helper()

	memoryEnabled := false
	dir := t.TempDir()
	cfg := &config.Config{
		Defaults: config.Defaults{ContextWindow: 10, Responsiveness: floatPtr(1)},
		Limits:   config.Limits{PerAgentPerHour: 20},
		Memory:   config.MemoryConfig{Enabled: &memoryEnabled},
	}
	d, err := New(cfg, dir, WithVerboseProviderErrors(verbose))
	if err != nil {
		t.Fatalf("New: %v", err)
	}
	t.Cleanup(d.Stop)

	participant, err := d.state.RegisterParticipant(model.Participant{
		Name: "agent", Kind: model.KindAgent, Provider: "mock", Model: "test-model",
	})
	if err != nil {
		t.Fatalf("RegisterParticipant: %v", err)
	}
	runtime := agent.NewRuntime(
		participant,
		&provider.MockProvider{Err: providerErr},
		"test-model",
		"test system",
		0,
		1,
		10,
		20,
	)
	worker := &agentWorker{
		runtime: runtime,
		inbox:   make(chan MessageWithSeq, 1),
	}
	ctx, cancel := context.WithCancel(context.Background())
	d.wg.Add(1)
	go d.runWorker(ctx, worker)
	return d, worker, cancel
}

func captureWorkerLog(
	t *testing.T,
	d *Daemon,
	worker *agentWorker,
	message model.Message,
	cancel context.CancelFunc,
) string {
	t.Helper()

	var output bytes.Buffer
	originalWriter := log.Writer()
	originalFlags := log.Flags()
	originalPrefix := log.Prefix()
	log.SetOutput(&output)
	log.SetFlags(0)
	log.SetPrefix("")
	t.Cleanup(func() {
		log.SetOutput(originalWriter)
		log.SetFlags(originalFlags)
		log.SetPrefix(originalPrefix)
	})

	worker.inbox <- MessageWithSeq{Message: message, Seq: 1}
	deadline := time.Now().Add(2 * time.Second)
	for time.Now().Before(deadline) {
		checkpoint, err := d.state.GetCheckpoint(worker.runtime.Participant.ID, 0.5)
		if err == nil && checkpoint.LastSeenSeq == 1 {
			cancel()
			d.wg.Wait()
			return output.String()
		}
		time.Sleep(10 * time.Millisecond)
	}
	cancel()
	d.wg.Wait()
	t.Fatal("worker did not save its checkpoint")
	return ""
}
