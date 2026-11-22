package attention

import (
	"context"
	"encoding/json"
	"errors"
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
		err = checkCodexSocket(ctx, p.CodexSocket, p.SessionID)
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

func checkCodexSocket(ctx context.Context, socket, thread string) error {
	host, err := startCodexConnection(ctx, socket, thread, false)
	if err != nil {
		return err
	}
	defer host.Close()
	data, err := host.call(ctx, "thread/read", map[string]any{"threadId": thread, "includeTurns": false})
	if err != nil {
		return beforeSubmission(err)
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
