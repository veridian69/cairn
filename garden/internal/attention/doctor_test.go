package attention

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"testing"
)

func TestDoctorOpenCodeIsReadOnlyAndChecksSelectedSession(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Method != "GET" || r.URL.Path != "/session/ses_target" {
			t.Error("doctor attempted unexpected operation")
		}
		_ = json.NewEncoder(w).Encode(map[string]string{"id": "ses_target"})
	}))
	defer srv.Close()
	p := Profile{Adapter: "opencode", HostEndpoint: srv.URL, SessionID: "ses_target", HostAPIVersion: "1"}
	if err := checkOpenCode(context.Background(), p); err != nil {
		t.Fatal(err)
	}
}

func TestDoctorCodexNeverResumesOrStartsTurn(t *testing.T) {
	fake := newFakeCodex(t, "msg-one", "probe")
	if err := checkCodexSocket(context.Background(), fake.socket, "thread-target"); err != nil {
		t.Fatal(err)
	}
	for _, method := range fake.received() {
		if method == "thread/resume" || method == "turn/start" {
			t.Fatalf("doctor sent %s", method)
		}
	}
}
