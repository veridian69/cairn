package headers

import (
	"net/http"
	"net/textproto"
	"testing"

	"github.com/veridian69/cairn/cairn-mcp/internal/credentials"
)

func TestAuthorised(t *testing.T) {
	tests := []struct {
		name    string
		headers http.Header
		token   []byte
		allowed bool
	}{
		{
			name:    "valid bearer",
			headers: http.Header{"Authorization": []string{"Bearer token"}},
			token:   []byte("token"),
			allowed: true,
		},
		{
			name:    "missing header",
			headers: http.Header{},
			token:   []byte("token"),
			allowed: false,
		},
		{
			name:    "wrong scheme",
			headers: http.Header{"Authorization": []string{"Basic token"}},
			token:   []byte("token"),
			allowed: false,
		},
		{
			name:    "wrong token",
			headers: http.Header{"Authorization": []string{"Bearer bad"}},
			token:   []byte("token"),
			allowed: false,
		},
		{
			name: "multiple headers",
			headers: http.Header{
				"Authorization": []string{"Bearer token", "Bearer token"},
			},
			token:   []byte("token"),
			allowed: false,
		},
	}

	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			got := Authorised(test.headers, test.token)
			if got != test.allowed {
				t.Fatalf("Authorised() = %v, want %v", got, test.allowed)
			}
		})
	}
}

func TestUpstreamHeaders(t *testing.T) {
	snapshot := credentials.CredentialSnapshot{
		CFClientID:     "client-id",
		CFClientSecret: "client-secret",
	}
	input := http.Header{
		"Accept":               {"application/json"},
		"Content-Type":         {"application/json"},
		"MCP-Protocol-Version": {"2024-11-05"},
		"Mcp-Session-ID":       {"abc"},
		"X-Ignored":            {"ignore"},
		"User-Agent":           {"go-test"},
	}

	output := UpstreamHeaders(input, snapshot)
	if output.Get("Accept") != "application/json" {
		t.Fatalf("Accept header not forwarded")
	}
	if output.Get("X-Ignored") != "" {
		t.Fatalf("unexpected forwarded header: X-Ignored")
	}
	if got, want := output.Get("CF-Access-Client-Id"), snapshot.CFClientID; got != want {
		t.Fatalf("CF id %q, want %q", got, want)
	}
	if got, want := output.Get("CF-Access-Client-Secret"), snapshot.CFClientSecret; got != want {
		t.Fatalf("CF secret %q, want %q", got, want)
	}

	contentType := output[http.CanonicalHeaderKey("content-type")]
	if len(contentType) != 1 || contentType[0] != "application/json" {
		t.Fatalf("content-type forwarded as %v", contentType)
	}
	if len(output[http.CanonicalHeaderKey("mcp-session-id")]) != 1 || output[http.CanonicalHeaderKey("mcp-session-id")][0] != "abc" {
		t.Fatalf("mcp-session-id forwarded as %v", output[http.CanonicalHeaderKey("mcp-session-id")])
	}
}

func TestDownstreamHeaders(t *testing.T) {
	input := http.Header{
		"Content-Type":      {"text/plain"},
		"Retry-After":       {"10"},
		"X-Ignored":         {"ignore"},
		"ETag":              {"\"abc\""},
		"Transfer-Encoding": {"chunked"},
	}
	output := DownstreamHeaders(input)

	if got, want := output.Get("Content-Type"), "text/plain"; got != want {
		t.Fatalf("content-type %q, want %q", got, want)
	}
	if output.Get("ETag") != "\"abc\"" {
		t.Fatal("expected ETag to be forwarded")
	}
	if output.Get("Retry-After") != "10" {
		t.Fatal("expected Retry-After to be forwarded")
	}
	if output.Get("X-Ignored") != "" {
		t.Fatal("unexpected forwarded header: X-Ignored")
	}
	if output.Get("Transfer-Encoding") != "" {
		t.Fatal("unexpected forwarded header: Transfer-Encoding")
	}

	canonical := http.CanonicalHeaderKey("retry-after")
	if textproto.CanonicalMIMEHeaderKey(canonical) != canonical {
		t.Fatalf("unexpected canonicalisation behaviour for retry-after")
	}
}
