package provider

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
)

type AnthropicProvider struct {
	APIKey  string
	BaseURL string
	Client  *http.Client
}

func (p *AnthropicProvider) Complete(ctx context.Context, req CompletionRequest) (CompletionResponse, error) {
	baseURL := p.BaseURL
	if baseURL == "" {
		baseURL = "https://api.anthropic.com"
	}

	type msg struct {
		Role    string `json:"role"`
		Content string `json:"content"`
	}
	var msgs []msg
	for _, m := range req.Messages {
		msgs = append(msgs, msg{Role: m.Role, Content: m.Content})
	}

	body := map[string]any{
		"model":      req.Model,
		"max_tokens": 4096,
		"messages":   msgs,
	}
	if req.System != "" {
		body["system"] = req.System
	}
	if req.Temperature > 0 {
		body["temperature"] = req.Temperature
	}

	data, err := json.Marshal(body)
	if err != nil {
		return CompletionResponse{}, newTransportError(ErrMalformed, err)
	}
	httpReq, err := http.NewRequestWithContext(ctx, "POST", baseURL+"/v1/messages", bytes.NewReader(data))
	if err != nil {
		return CompletionResponse{}, newTransportError(ErrTransient, err)
	}
	httpReq.Header.Set("Content-Type", "application/json")
	httpReq.Header.Set("x-api-key", p.APIKey)
	httpReq.Header.Set("anthropic-version", "2023-06-01")

	resp, err := httpClient(p.Client).Do(httpReq)
	if err != nil {
		return CompletionResponse{}, newTransportError(ErrTransient, err)
	}
	defer resp.Body.Close()

	respData, err := io.ReadAll(io.LimitReader(resp.Body, 10<<20))
	if err != nil {
		return CompletionResponse{}, newResponseError(ErrTransient, resp.StatusCode, nil, err)
	}

	switch {
	case resp.StatusCode == 401 || resp.StatusCode == 403:
		return CompletionResponse{}, newResponseError(ErrAuth, resp.StatusCode, respData, nil)
	case resp.StatusCode == http.StatusRequestTimeout || resp.StatusCode == http.StatusTooManyRequests || resp.StatusCode >= 500:
		return CompletionResponse{}, newResponseError(ErrTransient, resp.StatusCode, respData, nil)
	case resp.StatusCode != 200:
		return CompletionResponse{}, newResponseError(ErrMalformed, resp.StatusCode, respData, nil)
	}

	var result struct {
		Content []struct {
			Text string `json:"text"`
		} `json:"content"`
		Usage struct {
			InputTokens  int `json:"input_tokens"`
			OutputTokens int `json:"output_tokens"`
		} `json:"usage"`
	}
	if err := json.Unmarshal(respData, &result); err != nil {
		return CompletionResponse{}, newResponseError(ErrMalformed, resp.StatusCode, respData, err)
	}

	if len(result.Content) == 0 {
		return CompletionResponse{}, fmt.Errorf("%w: empty content", ErrRefused)
	}

	return CompletionResponse{
		Content:   result.Content[0].Text,
		TokensIn:  result.Usage.InputTokens,
		TokensOut: result.Usage.OutputTokens,
	}, nil
}

func (p *AnthropicProvider) Name() string { return "anthropic" }
