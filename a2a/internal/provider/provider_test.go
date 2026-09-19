package provider

import (
	"context"
	"encoding/json"
	"errors"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

func TestMockProvider(t *testing.T) {
	mock := &MockProvider{
		Resp: CompletionResponse{Content: "Hello back!", TokensIn: 10, TokensOut: 5},
	}
	resp, err := mock.Complete(context.Background(), CompletionRequest{
		System:   "test",
		Messages: []ChatMessage{{Role: "user", Content: "Hi"}},
	})
	if err != nil {
		t.Fatalf("Complete: %v", err)
	}
	if resp.Content != "Hello back!" {
		t.Errorf("Content = %q", resp.Content)
	}
	if resp.TokensIn != 10 {
		t.Errorf("TokensIn = %d", resp.TokensIn)
	}
}

func TestIsTransient(t *testing.T) {
	if !IsTransient(ErrTransient) {
		t.Error("ErrTransient should be transient")
	}
	if IsTransient(ErrAuth) {
		t.Error("ErrAuth should not be transient")
	}
}

func TestAnthropicProvider(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Header.Get("x-api-key") != "test-key" {
			t.Error("missing api key")
		}
		var body map[string]any
		if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
			t.Fatal(err)
		}
		if _, exists := body["temperature"]; exists {
			t.Fatalf("zero temperature must be omitted; body = %#v", body)
		}
		w.Header().Set("Content-Type", "application/json")
		json.NewEncoder(w).Encode(map[string]any{
			"content": []map[string]any{
				{"type": "text", "text": "Hello from Claude"},
			},
			"usage": map[string]any{
				"input_tokens":  15,
				"output_tokens": 8,
			},
		})
	}))
	defer server.Close()

	p := &AnthropicProvider{APIKey: "test-key", BaseURL: server.URL}
	resp, err := p.Complete(context.Background(), CompletionRequest{
		System:   "test",
		Messages: []ChatMessage{{Role: "user", Content: "Hi"}},
		Model:    "claude-opus-4-6",
	})
	if err != nil {
		t.Fatalf("Complete: %v", err)
	}
	if resp.Content != "Hello from Claude" {
		t.Errorf("Content = %q", resp.Content)
	}
	if resp.TokensIn != 15 || resp.TokensOut != 8 {
		t.Errorf("Tokens = %d/%d", resp.TokensIn, resp.TokensOut)
	}
}

func TestOpenAIAndDeepSeekUseTheirSupportedTokenLimitParameters(t *testing.T) {
	tests := []struct {
		name            string
		providerName    string
		wantParameter   string
		rejectParameter string
	}{
		{
			name:            "openai",
			providerName:    "openai",
			wantParameter:   "max_completion_tokens",
			rejectParameter: "max_tokens",
		},
		{
			name:            "deepseek",
			providerName:    "deepseek",
			wantParameter:   "max_tokens",
			rejectParameter: "max_completion_tokens",
		},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				var body map[string]any
				if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
					t.Fatal(err)
				}
				if got := body[tt.wantParameter]; got != float64(4096) {
					t.Fatalf("%s = %#v, want 4096; body = %#v", tt.wantParameter, got, body)
				}
				if _, exists := body[tt.rejectParameter]; exists {
					t.Fatalf("unsupported %s present; body = %#v", tt.rejectParameter, body)
				}
				if _, exists := body["temperature"]; exists {
					t.Fatalf("zero temperature must be omitted; body = %#v", body)
				}
				_ = json.NewEncoder(w).Encode(map[string]any{
					"choices": []map[string]any{{
						"message": map[string]any{"content": "ok"},
					}},
					"usage": map[string]any{
						"prompt_tokens": 1, "completion_tokens": 1,
					},
				})
			}))
			defer server.Close()

			p := &OpenAIProvider{
				APIKey: "test-key", BaseURL: server.URL, ProviderName: tt.providerName,
			}
			if _, err := p.Complete(context.Background(), CompletionRequest{
				Model: "test-model",
			}); err != nil {
				t.Fatal(err)
			}
		})
	}
}

func TestOpenAIProviderInvalidURLReturnsTransient(t *testing.T) {
	p := &OpenAIProvider{APIKey: "test-key", BaseURL: "://bad-url", ProviderName: "openai"}
	_, err := p.Complete(context.Background(), CompletionRequest{Model: "gpt-test"})
	if err == nil {
		t.Fatal("expected error")
	}
	if !errors.Is(err, ErrTransient) {
		t.Fatalf("error = %v, want transient", err)
	}
}

func TestGoogleProviderIncludesBodyOnTransientErrors(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		http.Error(w, "rate limit details", http.StatusTooManyRequests)
	}))
	defer server.Close()

	p := &GoogleProvider{APIKey: "test-key", BaseURL: server.URL}
	_, err := p.Complete(context.Background(), CompletionRequest{Model: "gemini-test"})
	if err == nil {
		t.Fatal("expected error")
	}
	if !errors.Is(err, ErrTransient) {
		t.Fatalf("error = %v, want transient", err)
	}
	var requestErr *RequestError
	if !errors.As(err, &requestErr) {
		t.Fatalf("error type = %T, want *RequestError", err)
	}
	if !strings.Contains(requestErr.ResponseBody, "rate limit details") {
		t.Fatalf("body = %q, want provider detail", requestErr.ResponseBody)
	}
}

func TestNewProviderVariants(t *testing.T) {
	tests := []struct {
		name    string
		want    string
		baseURL string
	}{
		{name: "anthropic", want: "anthropic"},
		{name: "google", want: "google"},
		{name: "openai", want: "openai"},
		{name: "deepseek", want: "deepseek"},
	}

	for _, tt := range tests {
		p, err := NewProvider(tt.name, "k", tt.baseURL)
		if err != nil {
			t.Fatalf("NewProvider(%q): %v", tt.name, err)
		}
		if got := p.Name(); got != tt.want {
			t.Fatalf("NewProvider(%q).Name() = %q, want %q", tt.name, got, tt.want)
		}
	}

	if _, err := NewProvider("unknown", "k", ""); err == nil {
		t.Fatal("NewProvider should fail for unknown providers")
	}
}

func TestOpenAIProviderMapsAuthMalformedAndRefusedErrors(t *testing.T) {
	t.Run("auth", func(t *testing.T) {
		server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			http.Error(w, "invalid key", http.StatusUnauthorized)
		}))
		defer server.Close()

		p := &OpenAIProvider{APIKey: "test-key", BaseURL: server.URL, ProviderName: "openai"}
		_, err := p.Complete(context.Background(), CompletionRequest{Model: "gpt-test"})
		var requestErr *RequestError
		if !errors.As(err, &requestErr) {
			t.Fatalf("error type = %T, want *RequestError", err)
		}
		if requestErr.StatusCode != http.StatusUnauthorized {
			t.Fatalf("status = %d, want %d", requestErr.StatusCode, http.StatusUnauthorized)
		}
		if !strings.Contains(requestErr.ResponseBody, "invalid key") {
			t.Fatalf("body = %q, want provider detail", requestErr.ResponseBody)
		}
		if strings.Contains(err.Error(), "invalid key") {
			t.Fatalf("ordinary error leaked response body: %q", err)
		}
		if !errors.Is(err, ErrAuth) {
			t.Fatalf("error = %v, want ErrAuth", err)
		}
	})

	t.Run("malformed-json", func(t *testing.T) {
		server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			w.WriteHeader(http.StatusOK)
			_, _ = w.Write([]byte("{not-json"))
		}))
		defer server.Close()

		p := &OpenAIProvider{APIKey: "test-key", BaseURL: server.URL, ProviderName: "openai"}
		_, err := p.Complete(context.Background(), CompletionRequest{Model: "gpt-test"})
		if !errors.Is(err, ErrMalformed) {
			t.Fatalf("error = %v, want ErrMalformed", err)
		}
		var requestErr *RequestError
		if !errors.As(err, &requestErr) {
			t.Fatalf("error type = %T, want *RequestError", err)
		}
		if requestErr.StatusCode != http.StatusOK ||
			requestErr.ResponseBody != "{not-json" ||
			requestErr.Cause == nil {
			t.Fatalf("request error = %#v", requestErr)
		}
	})

	t.Run("refused-no-choices", func(t *testing.T) {
		server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			_ = json.NewEncoder(w).Encode(map[string]any{
				"choices": []any{},
				"usage":   map[string]any{"prompt_tokens": 1, "completion_tokens": 0},
			})
		}))
		defer server.Close()

		p := &OpenAIProvider{APIKey: "test-key", BaseURL: server.URL, ProviderName: "openai"}
		_, err := p.Complete(context.Background(), CompletionRequest{Model: "gpt-test"})
		if !errors.Is(err, ErrRefused) {
			t.Fatalf("error = %v, want ErrRefused", err)
		}
	})
}

func TestAnthropicProviderMapsMalformedAndRefusedErrors(t *testing.T) {
	t.Run("server-error", func(t *testing.T) {
		server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			http.Error(w, "upstream exploded", http.StatusInternalServerError)
		}))
		defer server.Close()

		p := &AnthropicProvider{APIKey: "test-key", BaseURL: server.URL}
		_, err := p.Complete(context.Background(), CompletionRequest{Model: "claude-test"})
		if !errors.Is(err, ErrTransient) {
			t.Fatalf("error = %v, want ErrTransient", err)
		}
	})

	t.Run("auth", func(t *testing.T) {
		server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			http.Error(w, "invalid key", http.StatusUnauthorized)
		}))
		defer server.Close()

		p := &AnthropicProvider{APIKey: "test-key", BaseURL: server.URL}
		_, err := p.Complete(context.Background(), CompletionRequest{Model: "claude-test"})
		var requestErr *RequestError
		if !errors.As(err, &requestErr) {
			t.Fatalf("error type = %T, want *RequestError", err)
		}
		if requestErr.StatusCode != http.StatusUnauthorized {
			t.Fatalf("status = %d, want %d", requestErr.StatusCode, http.StatusUnauthorized)
		}
		if !strings.Contains(requestErr.ResponseBody, "invalid key") {
			t.Fatalf("body = %q, want provider detail", requestErr.ResponseBody)
		}
		if strings.Contains(err.Error(), "invalid key") {
			t.Fatalf("ordinary error leaked response body: %q", err)
		}
		if !errors.Is(err, ErrAuth) {
			t.Fatalf("error = %v, want ErrAuth", err)
		}
	})

	t.Run("refused-empty-content", func(t *testing.T) {
		server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			_ = json.NewEncoder(w).Encode(map[string]any{
				"content": []any{},
				"usage":   map[string]any{"input_tokens": 1, "output_tokens": 0},
			})
		}))
		defer server.Close()

		p := &AnthropicProvider{APIKey: "test-key", BaseURL: server.URL}
		_, err := p.Complete(context.Background(), CompletionRequest{Model: "claude-test"})
		if !errors.Is(err, ErrRefused) {
			t.Fatalf("error = %v, want ErrRefused", err)
		}
	})
}

func TestGoogleProviderMapsAuthMalformedAndRefusedErrors(t *testing.T) {
	t.Run("auth", func(t *testing.T) {
		server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			http.Error(w, "invalid key", http.StatusUnauthorized)
		}))
		defer server.Close()

		p := &GoogleProvider{APIKey: "test-key", BaseURL: server.URL}
		_, err := p.Complete(context.Background(), CompletionRequest{Model: "gemini-test"})
		var requestErr *RequestError
		if !errors.As(err, &requestErr) {
			t.Fatalf("error type = %T, want *RequestError", err)
		}
		if requestErr.StatusCode != http.StatusUnauthorized {
			t.Fatalf("status = %d, want %d", requestErr.StatusCode, http.StatusUnauthorized)
		}
		if !strings.Contains(requestErr.ResponseBody, "invalid key") {
			t.Fatalf("body = %q, want provider detail", requestErr.ResponseBody)
		}
		if strings.Contains(err.Error(), "invalid key") {
			t.Fatalf("ordinary error leaked response body: %q", err)
		}
		if !errors.Is(err, ErrAuth) {
			t.Fatalf("error = %v, want ErrAuth", err)
		}
	})

	t.Run("malformed-json", func(t *testing.T) {
		server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			w.WriteHeader(http.StatusOK)
			_, _ = w.Write([]byte("{not-json"))
		}))
		defer server.Close()

		p := &GoogleProvider{APIKey: "test-key", BaseURL: server.URL}
		_, err := p.Complete(context.Background(), CompletionRequest{Model: "gemini-test"})
		if !errors.Is(err, ErrMalformed) {
			t.Fatalf("error = %v, want ErrMalformed", err)
		}
	})

	t.Run("refused-empty-candidates", func(t *testing.T) {
		server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			_ = json.NewEncoder(w).Encode(map[string]any{
				"candidates":    []any{},
				"usageMetadata": map[string]any{"promptTokenCount": 1, "candidatesTokenCount": 0},
			})
		}))
		defer server.Close()

		p := &GoogleProvider{APIKey: "test-key", BaseURL: server.URL}
		_, err := p.Complete(context.Background(), CompletionRequest{Model: "gemini-test"})
		if !errors.Is(err, ErrRefused) {
			t.Fatalf("error = %v, want ErrRefused", err)
		}
	})

	t.Run("request-timeout-is-transient", func(t *testing.T) {
		server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			http.Error(w, "slow upstream", http.StatusRequestTimeout)
		}))
		defer server.Close()

		p := &GoogleProvider{APIKey: "test-key", BaseURL: server.URL}
		_, err := p.Complete(context.Background(), CompletionRequest{Model: "gemini-test"})
		if !errors.Is(err, ErrTransient) {
			t.Fatalf("error = %v, want ErrTransient", err)
		}
	})
}

func TestOpenAIAndAnthropicTreatRequestTimeoutAsTransient(t *testing.T) {
	tests := []struct {
		name string
		run  func(string) error
	}{
		{
			name: "openai",
			run: func(url string) error {
				p := &OpenAIProvider{APIKey: "test-key", BaseURL: url, ProviderName: "openai"}
				_, err := p.Complete(context.Background(), CompletionRequest{Model: "gpt-test"})
				return err
			},
		},
		{
			name: "anthropic",
			run: func(url string) error {
				p := &AnthropicProvider{APIKey: "test-key", BaseURL: url}
				_, err := p.Complete(context.Background(), CompletionRequest{Model: "claude-test"})
				return err
			},
		},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				http.Error(w, "slow upstream", http.StatusRequestTimeout)
			}))
			defer server.Close()

			err := tt.run(server.URL)
			if !errors.Is(err, ErrTransient) {
				t.Fatalf("error = %v, want ErrTransient", err)
			}
		})
	}
}
