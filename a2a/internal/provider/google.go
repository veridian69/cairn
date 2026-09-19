package provider

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
)

type GoogleProvider struct {
	APIKey  string
	BaseURL string
	Client  *http.Client
}

func (p *GoogleProvider) Complete(ctx context.Context, req CompletionRequest) (CompletionResponse, error) {
	baseURL := p.BaseURL
	if baseURL == "" {
		baseURL = "https://generativelanguage.googleapis.com"
	}

	type part struct {
		Text string `json:"text"`
	}
	type content struct {
		Role  string `json:"role,omitempty"`
		Parts []part `json:"parts"`
	}

	var contents []content
	for _, m := range req.Messages {
		role := m.Role
		if role == "assistant" {
			role = "model"
		}
		contents = append(contents, content{Role: role, Parts: []part{{Text: m.Content}}})
	}

	body := map[string]any{
		"contents":         contents,
		"generationConfig": map[string]any{"temperature": req.Temperature, "maxOutputTokens": 4096},
	}
	if req.System != "" {
		body["systemInstruction"] = content{Parts: []part{{Text: req.System}}}
	}

	data, err := json.Marshal(body)
	if err != nil {
		return CompletionResponse{}, newTransportError(ErrMalformed, err)
	}
	// Gemini's REST API commonly accepts the API key as a query parameter.
	// Keep that behavior explicit here so the tradeoff is documented.
	url := fmt.Sprintf("%s/v1beta/models/%s:generateContent?key=%s", baseURL, req.Model, p.APIKey)
	httpReq, err := http.NewRequestWithContext(ctx, "POST", url, bytes.NewReader(data))
	if err != nil {
		return CompletionResponse{}, newTransportError(ErrTransient, err)
	}
	httpReq.Header.Set("Content-Type", "application/json")

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
		Candidates []struct {
			Content struct {
				Parts []struct {
					Text string `json:"text"`
				} `json:"parts"`
			} `json:"content"`
		} `json:"candidates"`
		UsageMetadata struct {
			PromptTokenCount     int `json:"promptTokenCount"`
			CandidatesTokenCount int `json:"candidatesTokenCount"`
		} `json:"usageMetadata"`
	}
	if err := json.Unmarshal(respData, &result); err != nil {
		return CompletionResponse{}, newResponseError(ErrMalformed, resp.StatusCode, respData, err)
	}
	if len(result.Candidates) == 0 || len(result.Candidates[0].Content.Parts) == 0 {
		return CompletionResponse{}, fmt.Errorf("%w: empty", ErrRefused)
	}

	return CompletionResponse{
		Content:   result.Candidates[0].Content.Parts[0].Text,
		TokensIn:  result.UsageMetadata.PromptTokenCount,
		TokensOut: result.UsageMetadata.CandidatesTokenCount,
	}, nil
}

func (p *GoogleProvider) Name() string { return "google" }
