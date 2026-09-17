package provider

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
)

type OpenAIProvider struct {
	APIKey       string
	BaseURL      string
	ProviderName string
	Client       *http.Client
}

func (p *OpenAIProvider) Complete(ctx context.Context, req CompletionRequest) (CompletionResponse, error) {
	baseURL := p.BaseURL
	if baseURL == "" {
		baseURL = "https://api.openai.com"
	}

	type msg struct {
		Role    string `json:"role"`
		Content string `json:"content"`
	}
	var msgs []msg
	if req.System != "" {
		msgs = append(msgs, msg{Role: "system", Content: req.System})
	}
	for _, m := range req.Messages {
		msgs = append(msgs, msg{Role: m.Role, Content: m.Content})
	}

	tokenLimitParameter := "max_tokens"
	if p.ProviderName == "openai" {
		tokenLimitParameter = "max_completion_tokens"
	}
	body := map[string]any{
		"model":             req.Model,
		"messages":          msgs,
		tokenLimitParameter: 4096,
	}
	if req.Temperature > 0 {
		body["temperature"] = req.Temperature
	}

	data, err := json.Marshal(body)
	if err != nil {
		return CompletionResponse{}, newTransportError(ErrMalformed, err)
	}
	httpReq, err := http.NewRequestWithContext(ctx, "POST", baseURL+"/v1/chat/completions", bytes.NewReader(data))
	if err != nil {
		return CompletionResponse{}, newTransportError(ErrTransient, err)
	}
	httpReq.Header.Set("Content-Type", "application/json")
	httpReq.Header.Set("Authorization", "Bearer "+p.APIKey)

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
		Choices []struct {
			Message struct {
				Content string `json:"content"`
			} `json:"message"`
		} `json:"choices"`
		Usage struct {
			PromptTokens     int `json:"prompt_tokens"`
			CompletionTokens int `json:"completion_tokens"`
		} `json:"usage"`
	}
	if err := json.Unmarshal(respData, &result); err != nil {
		return CompletionResponse{}, newResponseError(ErrMalformed, resp.StatusCode, respData, err)
	}
	if len(result.Choices) == 0 {
		return CompletionResponse{}, fmt.Errorf("%w: no choices", ErrRefused)
	}

	return CompletionResponse{
		Content:   result.Choices[0].Message.Content,
		TokensIn:  result.Usage.PromptTokens,
		TokensOut: result.Usage.CompletionTokens,
	}, nil
}

func (p *OpenAIProvider) Name() string { return p.ProviderName }
