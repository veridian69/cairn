package garden

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"io"
	"mime"
	"net"
	"net/http"
	"net/netip"
	"net/url"
	"strings"
	"time"

	"github.com/modelcontextprotocol/go-sdk/mcp"
)

// ClientConfig supplies a remote endpoint and an in-memory opaque credential.
type ClientConfig struct {
	Endpoint      string
	Token         string
	TLSCAFile     string
	TLSServerName string
}

// Client calls the Garden tools over bounded authenticated stateless MCP HTTP.
type Client struct {
	session   *mcp.ClientSession
	transport *http.Transport
}

// Dial authenticates the MCP handshake. It never retries an ambiguous operation.
func Dial(ctx context.Context, cfg ClientConfig) (*Client, error) {
	u, err := url.Parse(cfg.Endpoint)
	if err != nil || u.Hostname() == "" || u.User != nil || u.RawQuery != "" || u.ForceQuery || u.Fragment != "" || u.RawPath != "" || u.Path != "/mcp" {
		return nil, failure("invalid_argument", "Invalid Garden endpoint")
	}
	if u.Scheme != "https" {
		addr, e := netip.ParseAddr(u.Hostname())
		if u.Scheme != "http" || e != nil || !addr.IsLoopback() {
			return nil, failure("invalid_argument", "Garden endpoint requires HTTPS or numeric loopback HTTP")
		}
	}
	if cfg.Token == "" || len(cfg.Token) > 4096 {
		return nil, failure("invalid_argument", "Invalid Garden credential")
	}
	for _, b := range []byte(cfg.Token) {
		if b < 33 || b > 126 || b == ',' {
			return nil, failure("invalid_argument", "Invalid Garden credential")
		}
	}
	tlsConfig, err := clientTLS(cfg, u.Scheme)
	if err != nil {
		return nil, err
	}
	base := &http.Transport{DialContext: (&net.Dialer{Timeout: 5 * time.Second, KeepAlive: 30 * time.Second}).DialContext, TLSHandshakeTimeout: 5 * time.Second, ResponseHeaderTimeout: 40 * time.Second, IdleConnTimeout: 30 * time.Second, MaxIdleConns: 10, MaxIdleConnsPerHost: 5, MaxResponseHeaderBytes: 16384, ForceAttemptHTTP2: true, DisableCompression: true, TLSClientConfig: tlsConfig}
	hc := &http.Client{Transport: &authenticatedTransport{base: base, token: cfg.Token}, Timeout: 40 * time.Second, CheckRedirect: func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse }}
	client := mcp.NewClient(&mcp.Implementation{Name: "garden-adapter", Version: "0.1.0"}, nil)
	session, err := client.Connect(ctx, &mcp.StreamableClientTransport{Endpoint: cfg.Endpoint, HTTPClient: hc, MaxRetries: -1, DisableStandaloneSSE: true}, nil)
	if err != nil {
		base.CloseIdleConnections()
		return nil, clientError(err)
	}
	return &Client{session: session, transport: base}, nil
}

type authenticatedTransport struct {
	base  http.RoundTripper
	token string
}

func (t *authenticatedTransport) RoundTrip(req *http.Request) (*http.Response, error) {
	req = req.Clone(req.Context())
	req.Header.Set("Authorization", "Bearer "+t.token)
	req.Header.Set("Accept-Encoding", "identity")
	resp, err := t.base.RoundTrip(req)
	if err != nil {
		return nil, failure("unavailable", "Garden request failed; delivery outcome may be uncertain")
	}
	if resp.StatusCode == 401 || resp.StatusCode == 403 {
		resp.Body.Close()
		return nil, failure("forbidden", "Garden authentication denied")
	}
	if resp.StatusCode != 200 && resp.StatusCode != 202 {
		resp.Body.Close()
		return nil, failure("unavailable", "Garden returned an unsuccessful response")
	}
	defer resp.Body.Close()
	encodings := resp.Header.Values("Content-Encoding")
	if len(encodings) > 1 || (len(encodings) == 1 && !strings.EqualFold(strings.TrimSpace(encodings[0]), "identity")) {
		return nil, failure("unavailable", "Garden response encoding is unsupported")
	}
	data, err := io.ReadAll(io.LimitReader(resp.Body, 8*1024*1024+1))
	if err != nil || len(data) > 8*1024*1024 {
		return nil, failure("unavailable", "Invalid or oversized Garden response")
	}
	if resp.StatusCode == 200 {
		media, _, e := mime.ParseMediaType(resp.Header.Get("Content-Type"))
		if e != nil || media != "application/json" {
			return nil, failure("unavailable", "Garden response must be JSON")
		}
	}
	resp.Body = io.NopCloser(bytes.NewReader(data))
	return resp, nil
}
func clientError(err error) error {
	var e *Error
	if errors.As(err, &e) {
		return e
	}
	if errors.Is(err, context.Canceled) || errors.Is(err, context.DeadlineExceeded) {
		return err
	}
	return failure("unavailable", "Garden MCP operation failed; delivery outcome may be uncertain")
}
func (c *Client) call(ctx context.Context, name string, args, out any) error {
	ctx, cancel := context.WithTimeout(ctx, 40*time.Second)
	defer cancel()
	result, err := c.session.CallTool(ctx, &mcp.CallToolParams{Name: name, Arguments: args})
	if err != nil {
		return clientError(err)
	}
	raw, err := json.Marshal(result.StructuredContent)
	if err != nil || string(raw) == "null" {
		return failure("unavailable", "Garden returned invalid structured content")
	}
	if result.IsError {
		var e Error
		if json.Unmarshal(raw, &e) != nil || e.Code == "" {
			return failure("unavailable", "Garden returned an invalid error")
		}
		switch e.Code {
		case "invalid_argument", "forbidden", "inbox_busy", "invalid_receipt", "retention_gap", "stream_reset", "unavailable":
			return &e
		default:
			return failure("unavailable", "Garden returned an unknown error")
		}
	}
	dec := json.NewDecoder(strings.NewReader(string(raw)))
	dec.DisallowUnknownFields()
	if err = dec.Decode(out); err != nil {
		return failure("unavailable", "Garden returned invalid tool data")
	}
	return nil
}

// Status returns the current authenticated participant and room binding.
func (c *Client) Status(ctx context.Context) (out StatusResult, err error) {
	err = c.call(ctx, "status", struct{}{}, &out)
	return
}

// Send publishes once; callers must not automatically retry uncertain outcomes.
func (c *Client) Send(ctx context.Context, a SendArgs) (out Message, err error) {
	err = c.call(ctx, "send_message", a, &out)
	return
}

// Read fetches a history page without advancing delivery state.
func (c *Client) Read(ctx context.Context, a ReadArgs) (out ReadResult, err error) {
	err = c.call(ctx, "read_messages", a, &out)
	return
}

// Poll acquires or renews an adapter lease and returns pending delivery.
func (c *Client) Poll(ctx context.Context, a PollArgs) (out PollResult, err error) {
	err = c.call(ctx, "poll_inbox", a, &out)
	return
}

// Acknowledge advances the inbox only for its active consumer and receipt.
func (c *Client) Acknowledge(ctx context.Context, a AckArgs) error {
	var out struct {
		Acknowledged bool `json:"acknowledged"`
	}
	if err := c.call(ctx, "acknowledge", a, &out); err != nil {
		return err
	}
	if !out.Acknowledged {
		return failure("unavailable", "Garden did not acknowledge delivery")
	}
	return nil
}

// Close releases the MCP client and HTTP idle connections.
func (c *Client) Close() error {
	err := c.session.Close()
	c.transport.CloseIdleConnections()
	return err
}
