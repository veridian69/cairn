package cmd

import (
	"bytes"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/spf13/cobra"
)

func TestRemoteCommandsDoNotAcquireLocalGardenRuntime(t *testing.T) {
	for _, command := range []*cobra.Command{newConnectCommand(), newAttendCommand(), newDoctorCommand()} {
		called := false
		root := &cobra.Command{Use: "a2a", PersistentPreRunE: func(*cobra.Command, []string) error { called = true; return nil }}
		root.SetOut(io.Discard)
		root.SetErr(io.Discard)
		root.AddCommand(command)
		root.SetArgs([]string{command.Name(), "--profile", "/definitely-missing-garden-profile"})
		if err := root.Execute(); err == nil {
			t.Fatal("missing profile accepted")
		}
		if called {
			t.Fatal("remote command touched the local daemon bootstrap")
		}
	}
}

func TestDoctorReadinessCannotCheckOrResumeHost(t *testing.T) {
	command := newDoctorCommand()
	command.SetOut(io.Discard)
	command.SetErr(io.Discard)
	command.SetArgs([]string{"--readiness", "--host", "--profile", "/missing-profile"})
	if err := command.Execute(); err == nil || !strings.Contains(err.Error(), "group") {
		t.Fatalf("readiness did not reject mutually exclusive host flag: %v", err)
	}
}

func TestDoctorReadinessEmitsObservationRatherThanTreatingExitZeroAsReady(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) { w.WriteHeader(http.StatusServiceUnavailable) }))
	defer server.Close()
	directory := t.TempDir()
	token := filepath.Join(directory, "token")
	if err := os.WriteFile(token, []byte("synthetic-readiness-token"), 0600); err != nil {
		t.Fatal(err)
	}
	profile := map[string]any{"garden_endpoint": server.URL + "/mcp", "credential_file": token, "instance_id": "11111111-1111-4111-8111-111111111111", "scope": map[string]any{"realm": "example", "segments": []map[string]string{{"kind": "project", "identifier": "garden"}}}, "classification": "internal", "participant": "worker", "adapter": "stdio"}
	data, err := json.Marshal(profile)
	if err != nil {
		t.Fatal(err)
	}
	path := filepath.Join(directory, "profile.json")
	if err := os.WriteFile(path, data, 0600); err != nil {
		t.Fatal(err)
	}
	command := newDoctorCommand()
	var output bytes.Buffer
	command.SetOut(&output)
	command.SetErr(io.Discard)
	command.SetArgs([]string{"--profile", path, "--readiness"})
	if err := command.Execute(); err != nil {
		t.Fatal(err)
	}
	var observation struct {
		Schema    string `json:"schema_version"`
		Ready     bool   `json:"ready"`
		Reason    string `json:"reason"`
		Retryable bool   `json:"retryable"`
	}
	if err := json.Unmarshal(output.Bytes(), &observation); err != nil {
		t.Fatal(err)
	}
	if observation.Schema != "garden.readiness/v1" || observation.Ready || observation.Reason != "unavailable" || !observation.Retryable {
		t.Fatalf("wrong observation: %+v", observation)
	}
	if strings.Contains(output.String(), "synthetic-readiness-token") {
		t.Fatal("readiness leaked a credential")
	}
}
