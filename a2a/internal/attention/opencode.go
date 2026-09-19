package attention

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"net/url"
	"strings"
	"time"
)

// OpenCode delivers through the v1 HTTP session API, without TUI injection.
type OpenCode struct {
	base                        *url.URL
	session, username, password string
	http                        *http.Client
	confirmTimeout              time.Duration
}

func NewOpenCode(raw, session, username, password string) (*OpenCode, error) {
	u, err := endpoint(raw)
	if err != nil {
		return nil, err
	}
	if !safeSession(session) {
		return nil, errors.New("invalid OpenCode session ID")
	}
	return &OpenCode{base: u, session: session, username: username, password: password, http: httpClient(), confirmTimeout: 10 * time.Second}, nil
}

func (h *OpenCode) request(ctx context.Context, method, path string, body []byte) (*http.Response, error) {
	u := *h.base
	u.Path += path
	req, err := http.NewRequestWithContext(ctx, method, u.String(), bytes.NewReader(body))
	if err != nil {
		return nil, ErrRejected
	}
	if h.password != "" {
		req.SetBasicAuth(h.username, h.password)
	}
	req.Header.Set("Accept", "application/json")
	req.Header.Set("Accept-Encoding", "identity")
	if body != nil {
		req.Header.Set("Content-Type", "application/json")
	}
	resp, err := h.http.Do(req)
	if err == nil && resp.Header.Get("Content-Encoding") != "" && resp.Header.Get("Content-Encoding") != "identity" {
		_ = resp.Body.Close()
		return nil, ErrRejected
	}
	return resp, err
}

func (h *OpenCode) Deliver(ctx context.Context, event Event) error {
	text, err := event.Text()
	if err != nil {
		return err
	}
	id := "msg_garden_" + strings.ReplaceAll(event.ID, "-", "")
	if !safeSession(id) {
		return ErrRejected
	}
	accepted, err := h.accepted(ctx, id, text)
	if err != nil {
		return err
	}
	if accepted {
		return nil
	}
	resp, err := h.request(ctx, http.MethodGet, "/session/status", nil)
	if err != nil {
		if errors.Is(err, ErrRejected) {
			return ErrRejected
		}
		return ErrUnavailable
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return rejectedStatus(resp.StatusCode, false)
	}
	body, err := io.ReadAll(io.LimitReader(resp.Body, 128*1024+1))
	if err != nil || len(body) > 128*1024 {
		return ErrUnavailable
	}
	var statuses map[string]struct {
		Type string `json:"type"`
	}
	if json.Unmarshal(body, &statuses) != nil || statuses == nil {
		return ErrUnavailable
	}
	if status, ok := statuses[h.session]; ok && status.Type != "idle" {
		return ErrBusy
	}
	payload, err := json.Marshal(map[string]any{"messageID": id, "parts": []map[string]any{{"type": "text", "text": text, "metadata": map[string]string{"source": "garden", "trust": "external_untrusted", "message_id": event.ID}}}})
	if err != nil {
		return ErrRejected
	}
	resp2, err := h.request(ctx, http.MethodPost, "/session/"+h.session+"/prompt_async", payload)
	if err == nil {
		_ = resp2.Body.Close()
		if resp2.StatusCode != http.StatusNoContent && errors.Is(rejectedStatus(resp2.StatusCode, true), ErrRejected) {
			return ErrRejected
		}
	}
	// prompt_async returns before persistence. Reconcile even an ambiguous POST,
	// but never submit it again automatically.
	confirmCtx, cancel := context.WithTimeout(ctx, h.confirmTimeout)
	defer cancel()
	for {
		accepted, err := h.accepted(confirmCtx, id, text)
		if err != nil {
			return ErrUncertain
		}
		if accepted {
			return nil
		}
		select {
		case <-confirmCtx.Done():
			return ErrUncertain
		case <-time.After(100 * time.Millisecond):
		}
	}
}

func (h *OpenCode) accepted(ctx context.Context, id, text string) (bool, error) {
	resp, err := h.request(ctx, http.MethodGet, "/session/"+h.session+"/message/"+id, nil)
	if err != nil {
		if errors.Is(err, ErrRejected) {
			return false, ErrRejected
		}
		return false, ErrUnavailable
	}
	defer resp.Body.Close()
	if resp.StatusCode == http.StatusNotFound {
		return false, nil
	}
	if resp.StatusCode != http.StatusOK {
		return false, rejectedStatus(resp.StatusCode, false)
	}
	body, err := io.ReadAll(io.LimitReader(resp.Body, 256*1024+1))
	if err != nil || len(body) > 256*1024 {
		return false, ErrUnavailable
	}
	var message struct {
		Info struct {
			ID        string `json:"id"`
			Role      string `json:"role"`
			SessionID string `json:"sessionID"`
		} `json:"info"`
		Parts []struct {
			Type string `json:"type"`
			Text string `json:"text"`
		} `json:"parts"`
	}
	if json.Unmarshal(body, &message) != nil || message.Info.ID != id || message.Info.Role != "user" || message.Info.SessionID != h.session {
		return false, ErrRejected
	}
	for _, part := range message.Parts {
		if part.Type == "text" && part.Text == text {
			return true, nil
		}
	}
	return false, ErrRejected
}
