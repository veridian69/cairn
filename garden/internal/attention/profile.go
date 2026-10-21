package attention

import (
	"bytes"
	"encoding/json"
	"errors"
	"io"
	"os"
	"path/filepath"
	"strings"

	"github.com/google/uuid"
	"github.com/veridian69/cairn/garden/internal/gardenauth"
)

// Profile selects one service binding and one agent session explicitly. Tokens
// live in protected files, never inline in this configuration or in arguments.
type Profile struct {
	GardenTLSCAFile     string           `json:"garden_tls_ca_file,omitempty"`
	GardenTLSServerName string           `json:"garden_tls_server_name,omitempty"`
	GardenEndpoint      string           `json:"garden_endpoint"`
	CredentialFile      string           `json:"credential_file"`
	InstanceID          string           `json:"instance_id"`
	Scope               gardenauth.Scope `json:"scope"`
	Classification      string           `json:"classification"`
	Participant         string           `json:"participant"`
	Adapter             string           `json:"adapter"`
	SessionID           string           `json:"session_id,omitempty"`
	HostEndpoint        string           `json:"host_endpoint,omitempty"`
	HostUsername        string           `json:"host_username,omitempty"`
	HostAPIVersion      string           `json:"host_api_version,omitempty"`
	HostCredentialFile  string           `json:"host_credential_file,omitempty"`
	CodexSocket         string           `json:"codex_socket,omitempty"`
	// CodexBinary is accepted so existing profiles still load, but it is ignored:
	// Garden dials the control socket itself and runs no Codex binary.
	CodexBinary string `json:"codex_binary,omitempty"`
}

func LoadProfile(path string) (Profile, error) {
	data, err := readRegular(path, 32*1024, false)
	if err != nil {
		return Profile{}, errors.New("cannot read adapter profile")
	}
	return parseProfile(data, path)
}

// parseProfile validates profile bytes already read from path; relative paths resolve
// against path's directory.
func parseProfile(data []byte, path string) (Profile, error) {
	var p Profile
	if err := strictJSON(data, &p); err != nil {
		return p, errors.New("invalid adapter profile JSON")
	}
	var fields map[string]json.RawMessage
	_ = json.Unmarshal(data, &fields)
	allowed := map[string]bool{}
	for _, name := range []string{"garden_endpoint", "garden_tls_ca_file", "garden_tls_server_name", "credential_file", "instance_id", "scope", "classification", "participant", "adapter", "session_id", "host_endpoint", "host_username", "host_api_version", "host_credential_file", "codex_socket", "codex_binary"} {
		allowed[name] = true
	}
	for name := range fields {
		if !allowed[name] {
			return p, errors.New("unknown adapter profile field")
		}
	}
	// An explicit null decodes to "" and would otherwise pass as omitted.
	if raw, ok := fields["host_api_version"]; ok && string(bytes.TrimSpace(raw)) == "null" {
		return p, errors.New("invalid adapter profile field")
	}
	base, err := filepath.Abs(filepath.Dir(path))
	if err != nil {
		return p, errors.New("invalid profile location")
	}
	return validateProfile(p, base)
}

// validateBinding checks the pinned Garden endpoint, instance, scope and classification.
// Harness homes share it, so both entry points refuse the same bindings.
func validateBinding(p Profile) error {
	u, err := endpoint(p.GardenEndpoint)
	if err != nil {
		return err
	}
	if u.Path != "/mcp" || u.RawPath != "" {
		return errors.New("Garden endpoint must end in /mcp")
	}
	if u.Scheme != "https" && (p.GardenTLSCAFile != "" || p.GardenTLSServerName != "") {
		return errors.New("Garden TLS settings require HTTPS")
	}
	id, err := uuid.Parse(p.InstanceID)
	if err != nil || id.String() != p.InstanceID || id.Version() != 4 || id.Variant() != uuid.RFC4122 {
		return errors.New("invalid expected Cairn instance")
	}
	if p.Scope.Realm == "" || p.Scope.Segments == nil {
		return errors.New("explicit expected scope is required")
	}
	for _, segment := range p.Scope.Segments {
		if segment.Kind == "" || segment.Identifier == "" {
			return errors.New("invalid expected scope segment")
		}
	}
	if p.Classification != "public" && p.Classification != "internal" && p.Classification != "restricted" {
		return errors.New("invalid expected classification")
	}
	return nil
}

// validateProfile checks one adapter profile and resolves its relative paths against base.
func validateProfile(p Profile, base string) (Profile, error) {
	if err := validateBinding(p); err != nil {
		return p, err
	}
	if p.Participant == "" || p.CredentialFile == "" {
		return p, errors.New("participant and credential file are required")
	}
	switch p.Adapter {
	case "stdio", "claude":
	case "codex":
		if !safeSession(p.SessionID) || !filepath.IsAbs(p.CodexSocket) {
			return p, errors.New("Codex requires a task ID and absolute control socket")
		}
	case "opencode":
		if !safeSession(p.SessionID) {
			return p, errors.New("OpenCode requires a session ID")
		}
		if _, err := endpoint(p.HostEndpoint); err != nil {
			return p, err
		}
		if p.HostUsername == "" {
			p.HostUsername = "opencode"
		}
		if p.HostAPIVersion == "" {
			p.HostAPIVersion = "1"
		}
		if p.HostAPIVersion != "1" && p.HostAPIVersion != "2" {
			return p, errors.New("OpenCode host API version must be 1 or 2")
		}
	default:
		return p, errors.New("adapter must be stdio, claude, codex or opencode")
	}
	if p.Adapter != "opencode" && p.HostAPIVersion != "" {
		return p, errors.New("host API version applies only to OpenCode")
	}
	if !filepath.IsAbs(p.CredentialFile) {
		p.CredentialFile = filepath.Join(base, p.CredentialFile)
	}
	if p.GardenTLSCAFile != "" && !filepath.IsAbs(p.GardenTLSCAFile) {
		p.GardenTLSCAFile = filepath.Join(base, p.GardenTLSCAFile)
	}
	if p.HostCredentialFile != "" && !filepath.IsAbs(p.HostCredentialFile) {
		p.HostCredentialFile = filepath.Join(base, p.HostCredentialFile)
	}
	return p, nil
}

func ReadCredential(path string) (string, error) {
	data, err := readRegular(path, 4096, true)
	if err != nil {
		return "", errors.New("cannot read protected credential file")
	}
	value := strings.TrimSpace(string(data))
	if value == "" || strings.ContainsAny(value, "\r\n\x00") {
		return "", errors.New("invalid credential file")
	}
	return value, nil
}

func readRegular(path string, limit int64, private bool) ([]byte, error) {
	abs, err := filepath.Abs(path)
	if err != nil {
		return nil, err
	}
	for current := abs; ; current = filepath.Dir(current) {
		info, err := os.Lstat(current)
		if err != nil {
			return nil, err
		}
		if info.Mode()&os.ModeSymlink != 0 {
			return nil, errors.New("symlink path refused")
		}
		if current == filepath.Dir(current) {
			break
		}
	}
	before, err := os.Lstat(abs)
	if err != nil {
		return nil, err
	}
	if !before.Mode().IsRegular() {
		return nil, errors.New("regular file required")
	}
	f, err := os.Open(abs)
	if err != nil {
		return nil, err
	}
	defer f.Close()
	after, err := f.Stat()
	if err != nil {
		return nil, err
	}
	if !os.SameFile(before, after) || !after.Mode().IsRegular() || (private && after.Mode().Perm()&0077 != 0) {
		return nil, errors.New("unsafe file permissions or changed file")
	}
	data, err := io.ReadAll(io.LimitReader(f, limit+1))
	if err != nil {
		return nil, err
	}
	if int64(len(data)) > limit {
		return nil, errors.New("file too large")
	}
	return data, nil
}

func strictJSON(data []byte, out any) error {
	check := json.NewDecoder(bytes.NewReader(data))
	var walk func(int) error
	walk = func(depth int) error {
		if depth > 32 {
			return errors.New("JSON too deep")
		}
		token, err := check.Token()
		if err != nil {
			return err
		}
		if delimiter, ok := token.(json.Delim); ok {
			switch delimiter {
			case '{':
				seen := map[string]bool{}
				for check.More() {
					key, err := check.Token()
					if err != nil {
						return err
					}
					name, ok := key.(string)
					if !ok || seen[name] {
						return errors.New("duplicate JSON field")
					}
					seen[name] = true
					if err := walk(depth + 1); err != nil {
						return err
					}
				}
			case '[':
				for check.More() {
					if err := walk(depth + 1); err != nil {
						return err
					}
				}
			default:
				return errors.New("unexpected JSON delimiter")
			}
			_, err = check.Token()
			return err
		}
		return nil
	}
	if err := walk(0); err != nil {
		return err
	}
	if _, err := check.Token(); err != io.EOF {
		return errors.New("trailing JSON")
	}
	decoder := json.NewDecoder(bytes.NewReader(data))
	decoder.DisallowUnknownFields()
	return decoder.Decode(out)
}
