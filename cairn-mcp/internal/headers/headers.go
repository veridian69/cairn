package headers

import (
	"net/http"
	"net/textproto"
	"strings"

	"github.com/veridian69/cairn/cairn-mcp/internal/credentials"
)

var requestHeaderSet = map[string]struct{}{
	"accept":               {},
	"content-type":         {},
	"content-length":       {},
	"mcp-protocol-version": {},
	"mcp-session-id":       {},
	"last-event-id":        {},
	"user-agent":           {},
	"cache-control":        {},
	"if-none-match":        {},
}

var responseHeaderSet = map[string]struct{}{
	"content-type":         {},
	"content-length":       {},
	"content-encoding":     {},
	"mcp-protocol-version": {},
	"mcp-session-id":       {},
	"cache-control":        {},
	"retry-after":          {},
	"etag":                 {},
	"last-modified":        {},
}

func Authorised(headers http.Header, token []byte) bool {
	values := headers.Values("Authorization")
	if len(values) != 1 {
		return false
	}
	value := values[0]
	if !strings.HasPrefix(value, "Bearer ") {
		return false
	}
	return credentials.TokenMatches(token, []byte(value[7:]))
}

func UpstreamHeaders(raw http.Header, snapshot credentials.CredentialSnapshot) http.Header {
	filtered := make(http.Header)
	for name, values := range raw {
		lower := strings.ToLower(name)
		if _, ok := requestHeaderSet[lower]; !ok {
			continue
		}
		canonical := textproto.CanonicalMIMEHeaderKey(lower)
		for _, value := range values {
			filtered.Add(canonical, value)
		}
	}
	filtered.Set("CF-Access-Client-Id", snapshot.CFClientID)
	filtered.Set("CF-Access-Client-Secret", snapshot.CFClientSecret)
	return filtered
}

func DownstreamHeaders(raw http.Header) http.Header {
	filtered := make(http.Header)
	for name, values := range raw {
		lower := strings.ToLower(name)
		if _, ok := responseHeaderSet[lower]; !ok {
			continue
		}
		canonical := textproto.CanonicalMIMEHeaderKey(lower)
		for _, value := range values {
			filtered.Add(canonical, value)
		}
	}
	return filtered
}
