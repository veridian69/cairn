package attention

import (
	"context"
	"errors"
)

// ReadinessReport is a content-free observation for bounded worker startup.
// Ready requires the maintained remote's complete authenticated binding check.
// A false result never grants permission to begin model work.
type ReadinessReport struct {
	SchemaVersion string `json:"schema_version"`
	Ready         bool   `json:"ready"`
	Reason        string `json:"reason"`
	Retryable     bool   `json:"retryable"`
}

// Readiness only connects and observes status. It never sends, polls attention,
// acknowledges delivery, checks a host session, or starts/resumes model work.
// The caller owns the total retry budget; this function performs one attempt.
func Readiness(ctx context.Context, profile Profile) ReadinessReport {
	result := ReadinessReport{SchemaVersion: "garden.readiness/v1", Reason: "rejected"}
	if ctx.Err() != nil {
		result.Reason = "deadline"
		return result
	}
	_, err := Doctor(ctx, profile, false)
	if ctx.Err() != nil {
		result.Reason = "deadline"
		return result
	}
	if err == nil {
		result.Ready = true
		result.Reason = "ready"
		return result
	}
	if errors.Is(err, ErrUnavailable) || errors.Is(err, ErrBusy) {
		result.Reason = "unavailable"
		result.Retryable = true
	}
	return result
}
