package attention

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"net/url"
	"strings"
	"time"
)

// OpenCode delivers through the HTTP session API, without TUI injection. The
// API generation is explicit: "1" for the root 1.x routes and "2" for the
// 2.x /api routes with {"data": ...} envelopes.
type OpenCode struct {
	base                        *url.URL
	api                         string
	session, username, password string
	http                        *http.Client
	confirmTimeout              time.Duration
}

func NewOpenCode(raw, session, username, password, api string) (*OpenCode, error) {
	u, err := endpoint(raw)
	if err != nil {
		return nil, err
	}
	if !safeSession(session) {
		return nil, errors.New("invalid OpenCode session ID")
	}
	if api != "1" && api != "2" {
		return nil, errors.New("OpenCode host API version must be 1 or 2")
	}
	return &OpenCode{base: u, api: api, session: session, username: username, password: password, http: httpClient(), confirmTimeout: 10 * time.Second}, nil
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
	id := h.messageID(event.ID)
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
	busy, err := h.busy(ctx)
	if err != nil {
		return err
	}
	if busy {
		return ErrBusy
	}
	metadata := map[string]string{"source": "garden", "trust": "external_untrusted", "message_id": event.ID}
	var payload []byte
	submitPath := "/session/" + h.session + "/prompt_async"
	if h.api == "2" {
		// 2.x steers by default, injecting into whatever turn is running. Queue
		// instead, so a session that turned busy after the check above is not
		// steered; an idle session still wakes and takes the queued item.
		payload, err = json.Marshal(map[string]any{"id": id, "text": text, "metadata": metadata, "delivery": "queue"})
		submitPath = "/api/session/" + h.session + "/prompt"
	} else {
		payload, err = json.Marshal(map[string]any{"messageID": id, "parts": []map[string]any{{"type": "text", "text": text, "metadata": metadata}}})
	}
	if err != nil {
		return ErrRejected
	}
	resp2, err := h.request(ctx, http.MethodPost, submitPath, payload)
	if err == nil {
		_ = resp2.Body.Close()
		// A 2.x 409 is a durable conflict on this message ID, not a busy session.
		// Retrying would resubmit forever, so it is a permanent rejection.
		if h.api == "2" && resp2.StatusCode == http.StatusConflict {
			return ErrRejected
		}
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

// messageID is the caller-chosen ID for one Garden event in this session. 2.x
// rejects an ID reused in another session, and Garden broadcasts share one event
// ID across recipients, so the 2.x ID is bound to the session as well.
func (h *OpenCode) messageID(eventID string) string {
	if h.api == "2" {
		sum := sha256.Sum256([]byte(h.session + "\x00" + eventID))
		return "msg_garden_" + hex.EncodeToString(sum[:16])
	}
	return "msg_garden_" + strings.ReplaceAll(eventID, "-", "")
}

// busy reports whether the selected session has a running agent loop. Sessions
// absent from the 2.x active list are idle.
func (h *OpenCode) busy(ctx context.Context) (bool, error) {
	statusPath := "/session/status"
	if h.api == "2" {
		statusPath = "/api/session/active"
	}
	resp, err := h.request(ctx, http.MethodGet, statusPath, nil)
	if err != nil {
		if errors.Is(err, ErrRejected) {
			return false, ErrRejected
		}
		return false, ErrUnavailable
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return false, rejectedStatus(resp.StatusCode, false)
	}
	body, err := io.ReadAll(io.LimitReader(resp.Body, 128*1024+1))
	if err != nil || len(body) > 128*1024 {
		return false, ErrUnavailable
	}
	if h.api == "2" {
		var active struct {
			Data map[string]json.RawMessage `json:"data"`
		}
		if json.Unmarshal(body, &active) != nil || active.Data == nil {
			return false, ErrRejected
		}
		_, running := active.Data[h.session]
		return running, nil
	}
	var statuses map[string]struct {
		Type string `json:"type"`
	}
	if json.Unmarshal(body, &statuses) != nil || statuses == nil {
		return false, ErrUnavailable
	}
	status, ok := statuses[h.session]
	return ok && status.Type != "idle", nil
}

// acceptedV2 checks the durable inbox before the projected message. Admitted
// work moves from the inbox to the message list, so this order cannot miss it.
func (h *OpenCode) acceptedV2(ctx context.Context, id, text string) (bool, error) {
	var inbox struct {
		Data []struct {
			ID      string `json:"id"`
			Type    string `json:"type"`
			Payload struct {
				Text string `json:"text"`
			} `json:"payload"`
		} `json:"data"`
	}
	found, err := h.getV2(ctx, "/api/session/"+h.session+"/inbox", &inbox)
	if err != nil {
		return false, err
	}
	if !found || inbox.Data == nil {
		return false, ErrRejected
	}
	for _, item := range inbox.Data {
		if item.ID == id {
			if item.Type == "user" && item.Payload.Text == text {
				return true, nil
			}
			return false, ErrRejected
		}
	}
	var message struct {
		Data struct {
			ID   string `json:"id"`
			Type string `json:"type"`
			Text string `json:"text"`
		} `json:"data"`
	}
	found, err = h.getV2(ctx, "/api/session/"+h.session+"/message/"+id, &message)
	if err != nil || !found {
		return false, err
	}
	if message.Data.ID != id || message.Data.Type != "user" || message.Data.Text != text {
		return false, ErrRejected
	}
	return true, nil
}

// getV2 reads one 2.x JSON envelope into out. A 404 is reported as absent;
// other failures are classified as the 1.x routes classify them.
func (h *OpenCode) getV2(ctx context.Context, path string, out any) (bool, error) {
	resp, err := h.request(ctx, http.MethodGet, path, nil)
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
	if json.Unmarshal(body, out) != nil {
		return false, ErrRejected
	}
	return true, nil
}

func (h *OpenCode) accepted(ctx context.Context, id, text string) (bool, error) {
	if h.api == "2" {
		return h.acceptedV2(ctx, id, text)
	}
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
