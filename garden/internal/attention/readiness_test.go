package attention

import (
	"context"
	"encoding/json"
	"strings"
	"testing"
)

func TestReadinessRefusesLocalCredentialFailureWithoutLeakingDetails(t *testing.T) {
	profile := Profile{Adapter: "stdio", CredentialFile: "/missing-synthetic-private-token"}
	report := Readiness(context.Background(), profile)
	if report.Ready || report.Retryable || report.Reason != "rejected" {
		t.Fatalf("unsafe readiness result: %+v", report)
	}
	payload, err := json.Marshal(report)
	if err != nil {
		t.Fatal(err)
	}
	if strings.Contains(string(payload), "missing-synthetic") {
		t.Fatal("readiness exposed local path")
	}
}

func TestReadinessCancelledBudgetDoesNotRequestRetry(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	report := Readiness(ctx, Profile{Adapter: "stdio"})
	if report.Ready || report.Retryable || report.Reason != "deadline" {
		t.Fatalf("cancelled readiness: %+v", report)
	}
}
