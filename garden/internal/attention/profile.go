package attention

import (
	"bytes"
	"encoding/json"
	"errors"
	"io"
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
	return parseProfile(data, path, false)
}

// errOpenCodeAdapterRemoved refuses the retired OpenCode HTTP adapter. OpenCode is supported only
// through the plugin, which needs no attend profile.
var errOpenCodeAdapterRemoved = errors.New("the OpenCode HTTP adapter was removed; use the OpenCode plugin under opencode --standalone (garden/plugins/opencode/README.md)")

// legacyProfile lets ImportLegacyHome read profiles written for the retired OpenCode adapter.
// The host_* values are accepted and discarded: the identity moves to the home's opencode default,
// which the plugin uses, and nothing contacts the old server.
type legacyProfile struct {
	*Profile
	HostEndpoint       json.RawMessage `json:"host_endpoint,omitempty"`
	HostUsername       json.RawMessage `json:"host_username,omitempty"`
	HostAPIVersion     json.RawMessage `json:"host_api_version,omitempty"`
	HostCredentialFile json.RawMessage `json:"host_credential_file,omitempty"`
}

// parseProfile validates profile bytes already read from path; relative paths resolve
// against path's directory. Only a legacy home import (legacy true) accepts the retired
// OpenCode adapter.
func parseProfile(data []byte, path string, legacy bool) (Profile, error) {
	// Checked before strict decoding, which would otherwise report a retired profile's host_* fields
	// as invalid JSON rather than naming the removal.
	var probe struct {
		Adapter string `json:"adapter"`
	}
	_ = json.Unmarshal(data, &probe)
	if probe.Adapter == "opencode" && !legacy {
		return Profile{}, errOpenCodeAdapterRemoved
	}
	var p Profile
	var target any = &p
	names := []string{"garden_endpoint", "garden_tls_ca_file", "garden_tls_server_name", "credential_file", "instance_id", "scope", "classification", "participant", "adapter", "session_id", "codex_socket", "codex_binary"}
	if legacy {
		target = &legacyProfile{Profile: &p}
		names = append(names, "host_endpoint", "host_username", "host_api_version", "host_credential_file")
	}
	if err := strictJSON(data, target); err != nil {
		return p, errors.New("invalid adapter profile JSON")
	}
	var fields map[string]json.RawMessage
	_ = json.Unmarshal(data, &fields)
	allowed := map[string]bool{}
	for _, name := range names {
		allowed[name] = true
	}
	for name := range fields {
		if !allowed[name] {
			return p, errors.New("unknown adapter profile field")
		}
	}
	base, err := filepath.Abs(filepath.Dir(path))
	if err != nil {
		return p, errors.New("invalid profile location")
	}
	return validateProfile(p, base, legacy)
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
func validateProfile(p Profile, base string, legacy bool) (Profile, error) {
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
		// Only a legacy home import reaches this case. It keeps the identity and drops the retired host binding.
		if !legacy {
			return p, errOpenCodeAdapterRemoved
		}
	default:
		return p, errors.New("adapter must be stdio, claude or codex")
	}
	if !filepath.IsAbs(p.CredentialFile) {
		p.CredentialFile = filepath.Join(base, p.CredentialFile)
	}
	if p.GardenTLSCAFile != "" && !filepath.IsAbs(p.GardenTLSCAFile) {
		p.GardenTLSCAFile = filepath.Join(base, p.GardenTLSCAFile)
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

// readRegular reads one protected input (an adapter profile or a credential) with the import
// sources' rule: trusted parents, one O_NOFOLLOW descriptor, a regular file owned by this user,
// private when asked, otherwise not writable by group or others.
func readRegular(path string, limit int64, private bool) ([]byte, error) {
	return ownedSourceBytes(path, limit, "file", private)
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
