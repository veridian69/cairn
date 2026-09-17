package provider

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
)

type Embedder interface {
	Embed(ctx context.Context, text string) ([]float32, error)
	ModelName() string
}

func NewEmbedder(providerName, apiKey, model, baseURL string) (Embedder, error) {
	if providerName != "openai" {
		return nil, fmt.Errorf("unknown embedding provider: %q", providerName)
	}
	if model == "" {
		return nil, fmt.Errorf("embedding model is required")
	}
	return &OpenAIEmbedder{APIKey: apiKey, BaseURL: baseURL, Model: model}, nil
}

type OpenAIEmbedder struct {
	APIKey  string
	BaseURL string
	Model   string
	Client  *http.Client
}

func (e *OpenAIEmbedder) ModelName() string { return e.Model }

func (e *OpenAIEmbedder) Embed(ctx context.Context, text string) ([]float32, error) {
	baseURL := e.BaseURL
	if baseURL == "" {
		baseURL = "https://api.openai.com"
	}
	body, err := json.Marshal(map[string]any{"model": e.Model, "input": text})
	if err != nil {
		return nil, fmt.Errorf("%w: %v", ErrMalformed, err)
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, baseURL+"/v1/embeddings", bytes.NewReader(body))
	if err != nil {
		return nil, fmt.Errorf("%w: %v", ErrTransient, err)
	}
	req.Header.Set("Authorization", "Bearer "+e.APIKey)
	req.Header.Set("Content-Type", "application/json")
	resp, err := httpClient(e.Client).Do(req)
	if err != nil {
		return nil, fmt.Errorf("%w: %v", ErrTransient, err)
	}
	defer resp.Body.Close()
	responseBody, err := io.ReadAll(io.LimitReader(resp.Body, 10<<20))
	if err != nil {
		return nil, fmt.Errorf("%w: %v", ErrTransient, err)
	}
	switch {
	case resp.StatusCode == http.StatusUnauthorized || resp.StatusCode == http.StatusForbidden:
		return nil, fmt.Errorf("%w: status %d", ErrAuth, resp.StatusCode)
	case resp.StatusCode == http.StatusRequestTimeout || resp.StatusCode == http.StatusTooManyRequests || resp.StatusCode >= 500:
		return nil, fmt.Errorf("%w: status %d: %s", ErrTransient, resp.StatusCode, responseBody)
	case resp.StatusCode != http.StatusOK:
		return nil, fmt.Errorf("%w: status %d: %s", ErrMalformed, resp.StatusCode, responseBody)
	}
	var decoded struct {
		Data []struct {
			Embedding []float64 `json:"embedding"`
		} `json:"data"`
	}
	if err := json.Unmarshal(responseBody, &decoded); err != nil {
		return nil, fmt.Errorf("%w: %v", ErrMalformed, err)
	}
	if len(decoded.Data) == 0 || len(decoded.Data[0].Embedding) == 0 {
		return nil, fmt.Errorf("%w: no embedding in response", ErrMalformed)
	}
	vector := make([]float32, len(decoded.Data[0].Embedding))
	for i, value := range decoded.Data[0].Embedding {
		vector[i] = float32(value)
	}
	return vector, nil
}
