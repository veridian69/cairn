package attention

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os"
	"os/exec"
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
	p := Profile{Adapter: "opencode", HostEndpoint: srv.URL, SessionID: "ses_target"}
	if err := checkOpenCode(context.Background(), p); err != nil {
		t.Fatal(err)
	}
}

func TestDoctorCodexNeverResumesOrStartsTurn(t *testing.T) {
	ctx := context.Background()
	command := exec.Command(os.Args[0], "-test.run=TestCodexWireHelper")
	command.Env = append(os.Environ(), "GARDEN_CODEX_TEST_HELPER=probe")
	if err := checkCodexCommand(ctx, command, "thread-target"); err != nil {
		t.Fatal(err)
	}
}
