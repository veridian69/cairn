package attention

import (
	"bytes"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"slices"
)

// AddIdentityResult reports what AddIdentity did. It holds names and paths only.
type AddIdentityResult struct {
	Home       string
	Name       string
	Present    bool
	Written    []string
	Harness    string
	DefaultSet bool
}

// AddIdentity installs one token, issued on the Garden server, as identities/<name>.token
// in an existing Garden home, and optionally makes it the default for harness. The name is
// checked before anything else. The source must be a private regular file owned by this
// user, read through one checked descriptor. The home lock is held for the whole command,
// so every check (token bytes, the default, garden.json) reads state no other command can
// change meanwhile. The token is published without replacement and garden.json last.
func AddIdentity(dest, name, tokenFile, harness string) (AddIdentityResult, error) {
	result := AddIdentityResult{Name: name, Harness: harness}
	if !participantName.MatchString(name) {
		return result, errInvalidIdentity
	}
	if harness != "" && !slices.Contains(legacyHarnesses, harness) {
		return result, fmt.Errorf("--default: unknown harness %q", harness)
	}
	dst, err := filepath.Abs(dest)
	if err != nil {
		return result, errors.New("invalid Garden home")
	}
	result.Home = dst
	root, err := existingHome(dst)
	if err != nil {
		return result, err
	}
	defer closeRoot(root)
	if root == nil {
		return result, fmt.Errorf("no Garden home at %s; run `garden home import --from <dir>` first, or write garden.json from the template in the plugin README", dst)
	}
	unlock, err := lockHome(root)
	if err != nil {
		return result, err
	}
	defer unlock()
	if _, err := LoadHome(dst); err != nil {
		return result, fmt.Errorf("no usable garden.json in %s (%v); run `garden home import --from <dir>` first, or write garden.json from the template in the plugin README", dst, err)
	}
	raw, err := privateSourceBytes(tokenFile, 4096)
	if err != nil {
		return result, err
	}

	// Plan, under the lock: the identities directory, any existing token, and the default.
	if err := rootExistingPrivateDir(root, "identities"); err != nil {
		return result, err
	}
	rel := "identities/" + name + ".token"
	current, exists, err := rootReadFile(root, rel, 64*1024, true)
	if err != nil {
		return result, err
	}
	if exists && !sameBytes(current, raw) {
		return result, fmt.Errorf("existing file differs; refusing to overwrite: %s", rel)
	}
	var defaultData []byte
	if harness != "" {
		defaultData, err = defaultsUpdate(root, harness, name)
		if err != nil {
			return result, err
		}
	}

	// Write phase: token first, then garden.json through a temporary name.
	if !exists {
		if err := rootPrivateDir(root, "identities"); err != nil {
			return result, err
		}
		made, err := rootPublishNew(root, rel, raw, true)
		if err != nil {
			return result, err
		}
		if made {
			result.Written = append(result.Written, filepath.Join(dst, rel))
		} else {
			result.Present = true
		}
	} else {
		result.Present = true
	}
	value, err := ReadCredential(filepath.Join(dst, rel))
	if err != nil || value != string(bytes.TrimSpace(raw)) {
		return result, fmt.Errorf("installed token for %s is not readable", name)
	}

	if defaultData != nil {
		if err := rootReplace(root, "garden.json", defaultData); err != nil {
			return result, err
		}
		if reloaded, err := LoadHome(dst); err != nil || reloaded.Config.Defaults[harness] != name {
			return result, errors.New("updated garden.json does not load with the new default")
		}
		result.DefaultSet = true
	}
	return result, nil
}

// defaultsUpdate reads garden.json under the home lock and returns it with one harness default
// set. It refuses a different default already present, so a conflicting default set by an
// earlier command is never replaced. Every other field is kept as it was, and a null or absent
// defaults object is initialised before the assignment, so no write can panic on a nil map.
func defaultsUpdate(root *os.Root, harness, name string) ([]byte, error) {
	data, exists, err := rootReadFile(root, "garden.json", 32*1024, false)
	if err != nil {
		return nil, err
	}
	if !exists {
		return nil, errors.New("cannot read garden.json")
	}
	fields, err := exactHomeFields(data)
	if err != nil {
		return nil, err
	}
	defaults := map[string]string{}
	if raw, ok := fields["defaults"]; ok {
		var decoded map[string]string
		if err := json.Unmarshal(raw, &decoded); err != nil {
			return nil, errors.New("invalid garden.json")
		}
		for key, value := range decoded {
			defaults[key] = value
		}
	}
	if current, ok := defaults[harness]; ok && current != name {
		return nil, fmt.Errorf("%s already defaults to %s; edit garden.json deliberately to change it", harness, current)
	}
	if defaults[harness] == name {
		return nil, nil
	}
	defaults[harness] = name
	encoded, err := json.Marshal(defaults)
	if err != nil {
		return nil, errors.New("cannot encode garden.json")
	}
	fields["defaults"] = encoded
	out, err := json.MarshalIndent(fields, "", "  ")
	if err != nil {
		return nil, errors.New("cannot encode garden.json")
	}
	return append(out, '\n'), nil
}

// exactHomeFields decodes the top level of garden.json, keeping every value raw.
func exactHomeFields(data []byte) (map[string]json.RawMessage, error) {
	return exactKeys(data, "garden_endpoint", "garden_tls_ca_file", "garden_tls_server_name", "instance_id", "scope", "classification", "defaults")
}
