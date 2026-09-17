package provider

import (
	"context"
	"errors"
	"fmt"
	"net/http"
	"time"

	"github.com/veridian69/cairn/a2a/internal/model"
)

// Common error types — providers normalize API errors into these.
var (
	ErrTransient = errors.New("transient provider error")
	ErrAuth      = errors.New("authentication error")
	ErrMalformed = errors.New("malformed response")
	ErrRefused   = errors.New("model refused")
)

var defaultHTTPClient = &http.Client{Timeout: 120 * time.Second}

// RequestError preserves provider request diagnostics while retaining an error category.
type RequestError struct {
	Category     error
	StatusCode   int
	ResponseBody string
	Cause        error
}

// Error returns a provider request summary that intentionally omits the response body.
func (e *RequestError) Error() string {
	if e.StatusCode > 0 {
		status := http.StatusText(e.StatusCode)
		if status == "" {
			return fmt.Sprintf("%v: HTTP %d", e.Category, e.StatusCode)
		}
		return fmt.Sprintf("%v: HTTP %d %s", e.Category, e.StatusCode, status)
	}
	if e.Cause != nil {
		return fmt.Sprintf("%v: %v", e.Category, e.Cause)
	}
	return e.Category.Error()
}

// Unwrap exposes the category so callers can classify the error with errors.Is.
func (e *RequestError) Unwrap() error { return e.Category }

func newResponseError(category error, statusCode int, body []byte, cause error) *RequestError {
	return &RequestError{
		Category: category, StatusCode: statusCode,
		ResponseBody: string(body), Cause: cause,
	}
}

func newTransportError(category, cause error) *RequestError {
	return &RequestError{Category: category, Cause: cause}
}

func IsTransient(err error) bool {
	return errors.Is(err, ErrTransient)
}

type ChatMessage struct {
	Role    string
	Content string
}

type ControlBlock struct {
	Responsiveness *float64                `json:"responsiveness,omitempty"`
	Remember       *string                 `json:"remember,omitempty"`
	Accountant     *model.AccountantRecord `json:"accountant,omitempty"`
}

type CompletionRequest struct {
	System      string
	Messages    []ChatMessage
	Model       string
	Temperature float64
}

type CompletionResponse struct {
	Content   string
	Control   *ControlBlock
	TokensIn  int
	TokensOut int
}

type Provider interface {
	Complete(ctx context.Context, req CompletionRequest) (CompletionResponse, error)
	Name() string
}

type MockProvider struct {
	Resp  CompletionResponse
	Err   error
	Calls []CompletionRequest
}

func (m *MockProvider) Complete(ctx context.Context, req CompletionRequest) (CompletionResponse, error) {
	m.Calls = append(m.Calls, req)
	return m.Resp, m.Err
}

func (m *MockProvider) Name() string { return "mock" }

func httpClient(client *http.Client) *http.Client {
	if client != nil {
		return client
	}
	return defaultHTTPClient
}

// NewProvider creates a provider by name.
func NewProvider(name, apiKey, baseURL string) (Provider, error) {
	switch name {
	case "anthropic":
		return &AnthropicProvider{APIKey: apiKey, BaseURL: baseURL}, nil
	case "google":
		return &GoogleProvider{APIKey: apiKey, BaseURL: baseURL}, nil
	case "openai":
		return &OpenAIProvider{APIKey: apiKey, BaseURL: baseURL, ProviderName: "openai"}, nil
	case "deepseek":
		url := baseURL
		if url == "" {
			url = "https://api.deepseek.com"
		}
		return &OpenAIProvider{APIKey: apiKey, BaseURL: url, ProviderName: "deepseek"}, nil
	default:
		return nil, fmt.Errorf("unknown provider: %q", name)
	}
}
