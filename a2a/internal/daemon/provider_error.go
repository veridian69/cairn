package daemon

import (
	"context"
	"errors"
	"fmt"
	"net/http"
	"strings"
	"unicode"
	"unicode/utf8"

	"github.com/veridian69/cairn/a2a/internal/provider"
)

const maxDiagnosticBytes = 1024
const providerErrorPrefixRedaction = "REDACTED"

func formatProviderError(
	providerName, modelName, apiKey string,
	verbose bool,
	err error,
) string {
	status := "unavailable"
	var body string
	var cause error

	var requestErr *provider.RequestError
	if errors.As(err, &requestErr) {
		if requestErr.StatusCode > 0 {
			status = fmt.Sprintf("%d", requestErr.StatusCode)
			if statusText := http.StatusText(requestErr.StatusCode); statusText != "" {
				status += " " + statusText
			}
		}
		body = requestErr.ResponseBody
		cause = requestErr.Cause
	} else {
		cause = err
	}

	fields := []string{
		fmt.Sprintf("provider=%q", sanitiseDiagnostic(providerName, apiKey)),
		fmt.Sprintf("model=%q", sanitiseDiagnostic(modelName, apiKey)),
		fmt.Sprintf("status=%q", status),
		fmt.Sprintf("error=%q", providerErrorCategory(err)),
	}
	if cause != nil {
		fields = append(fields, fmt.Sprintf(
			"cause=%q",
			sanitiseDiagnostic(cause.Error(), apiKey),
		))
	}
	if verbose {
		if diagnosticBody := sanitiseDiagnostic(body, apiKey); diagnosticBody != "" {
			fields = append(fields, fmt.Sprintf("body=%q", diagnosticBody))
		}
	}
	return strings.Join(fields, " ")
}

func providerErrorCategory(err error) string {
	switch {
	case errors.Is(err, provider.ErrAuth):
		return "authentication"
	case errors.Is(err, provider.ErrTransient):
		return "transient"
	case errors.Is(err, provider.ErrMalformed):
		return "malformed"
	case errors.Is(err, context.Canceled), errors.Is(err, context.DeadlineExceeded):
		return "cancelled"
	default:
		return "unknown"
	}
}

func sanitiseDiagnostic(value, apiKey string) string {
	if apiKey != "" {
		value = strings.ReplaceAll(value, apiKey, "[REDACTED]")
	}
	value = strings.ToValidUTF8(value, "�")
	value = strings.Join(strings.Fields(value), " ")
	if len(value) <= maxDiagnosticBytes {
		return value
	}

	limit := maxDiagnosticBytes - len("…")
	end := 0
	for offset, r := range value {
		next := offset + utf8.RuneLen(r)
		if next > limit {
			break
		}
		end = next
	}
	return value[:end] + "…"
}

func redactProviderErrorPrefixValue(value, apiKey string) string {
	if apiKey == "" {
		return value
	}
	return strings.ReplaceAll(value, apiKey, providerErrorPrefixRedaction)
}

func sanitiseProviderErrorPrefixToken(value, apiKey string) string {
	value = redactProviderErrorPrefixValue(value, apiKey)
	value = strings.ToValidUTF8(value, "_")
	value = strings.Map(func(r rune) rune {
		if unicode.IsSpace(r) {
			return ' '
		}
		if unicode.IsControl(r) {
			return '_'
		}
		switch r {
		case '[', ']', ':':
			return '_'
		default:
			return r
		}
	}, value)
	return strings.Join(strings.Fields(value), " ")
}
