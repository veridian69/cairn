package attention

import (
	"context"
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"os/exec"
)

// DoctorReport contains connection results without credentials or chat content.
type DoctorReport struct {
	Garden      string `json:"garden"`
	Participant string `json:"participant"`
	Adapter     string `json:"adapter"`
	Host        string `json:"host"`
}

// Doctor verifies the configured binding and optionally checks the selected
// host session. It never sends messages, polls an inbox, or resumes a task.
func Doctor(ctx context.Context, p Profile, checkHost bool) (DoctorReport, error) {
	result := DoctorReport{Participant: p.Participant, Adapter: p.Adapter, Host: "not_checked"}
	remote, err := Connect(ctx, p)
	if err != nil {
		return result, err
	}
	defer remote.Close()
	result.Garden = "authenticated_binding_verified"
	if !checkHost {
		return result, nil
	}
	switch p.Adapter {
	case "codex":
		binary := p.CodexBinary
		if binary == "" {
			binary = "codex"
		}
		err = checkCodexCommand(ctx, exec.CommandContext(ctx, binary, "app-server", "proxy", "--sock", p.CodexSocket), p.SessionID)
	case "opencode":
		err = checkOpenCode(ctx, p)
	case "claude":
		result.Host = "channel_requires_host_consent"
		return result, nil
	case "stdio":
		result.Host = "tools_only"
		return result, nil
	default:
		return result, errors.New("unsupported adapter")
	}
	if err != nil {
		return result, err
	}
	result.Host = "selected_session_readable"
	return result, nil
}

func checkOpenCode(ctx context.Context, p Profile) error {
	password := ""
	if p.HostCredentialFile != "" {
		var err error
		password, err = ReadCredential(p.HostCredentialFile)
		if err != nil {
			return err
		}
	}
	host, err := NewOpenCode(p.HostEndpoint, p.SessionID, p.HostUsername, password)
	if err != nil {
		return err
	}
	defer host.http.CloseIdleConnections()
	resp, err := host.request(ctx, http.MethodGet, "/session/"+p.SessionID, nil)
	if err != nil {
		return ErrUnavailable
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return rejectedStatus(resp.StatusCode, false)
	}
	data, err := io.ReadAll(io.LimitReader(resp.Body, 128*1024+1))
	if err != nil || len(data) > 128*1024 {
		return ErrRejected
	}
	var session struct {
		ID string `json:"id"`
	}
	if json.Unmarshal(data, &session) != nil || session.ID != p.SessionID {
		return ErrRejected
	}
	return nil
}

func checkCodexCommand(ctx context.Context, command *exec.Cmd, thread string) error {
	host, err := startCodexConnection(ctx, command, thread, false)
	if err != nil {
		return err
	}
	defer host.Close()
	data, err := host.call(ctx, "thread/read", map[string]any{"threadId": thread, "includeTurns": false})
	if err != nil {
		return err
	}
	var response struct {
		Thread struct {
			ID string `json:"id"`
		} `json:"thread"`
	}
	if json.Unmarshal(data, &response) != nil || response.Thread.ID != thread {
		return ErrRejected
	}
	return nil
}
