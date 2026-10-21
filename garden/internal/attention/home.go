package attention

import (
	"encoding/json"
	"errors"
	"os"
	"path/filepath"
	"regexp"
	"slices"

	"github.com/veridian69/cairn/garden/internal/gardenauth"
)

// participantName mirrors the gateway rule in internal/garden/config.go.
var participantName = regexp.MustCompile(`^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,63}$`)

var errInvalidIdentity = errors.New("invalid Garden identity name")

// HomeConfig is garden.json: the Garden binding and the default identity per
// harness. Everything here is shared by every harness plugin on this client.
type HomeConfig struct {
	GardenEndpoint      string            `json:"garden_endpoint"`
	GardenTLSCAFile     string            `json:"garden_tls_ca_file,omitempty"`
	GardenTLSServerName string            `json:"garden_tls_server_name,omitempty"`
	InstanceID          string            `json:"instance_id"`
	Scope               gardenauth.Scope  `json:"scope"`
	Classification      string            `json:"classification"`
	Defaults            map[string]string `json:"defaults,omitempty"`
}

// Home is a validated Garden home. Identity tokens live in Dir/identities.
type Home struct {
	Dir    string
	Config HomeConfig
}

// ResolveHomeDir returns $GARDEN_HOME, else ~/.garden. An empty GARDEN_HOME counts
// as unset; a relative override is an error. ~/.garden is also the local daemon's
// directory; the harness files (garden.json, tls/, identities/, run/) do not
// collide with the daemon's config.yaml, data/, daemon.url or daemon.pid.
func ResolveHomeDir() (string, error) {
	if dir := os.Getenv("GARDEN_HOME"); dir != "" {
		if !filepath.IsAbs(dir) {
			return "", errors.New("GARDEN_HOME must be an absolute path")
		}
		return filepath.Clean(dir), nil
	}
	user, err := os.UserHomeDir()
	if err != nil {
		return "", errors.New("cannot find the user home directory")
	}
	return filepath.Join(user, ".garden"), nil
}

// LoadHome reads and validates dir/garden.json. Relative paths resolve against dir.
func LoadHome(dir string) (Home, error) {
	var c HomeConfig
	data, err := readRegular(filepath.Join(dir, "garden.json"), 32*1024, false)
	if err != nil {
		return Home{}, errors.New("cannot read garden.json")
	}
	if err := exactHomeNames(data); err != nil {
		return Home{}, err
	}
	if err := strictJSON(data, &c); err != nil {
		return Home{}, errors.New("invalid garden.json")
	}
	if err := validateBinding(Profile{GardenEndpoint: c.GardenEndpoint, GardenTLSCAFile: c.GardenTLSCAFile, GardenTLSServerName: c.GardenTLSServerName, InstanceID: c.InstanceID, Scope: c.Scope, Classification: c.Classification}); err != nil {
		return Home{}, err
	}
	for _, name := range c.Defaults {
		if !participantName.MatchString(name) {
			return Home{}, errInvalidIdentity
		}
	}
	if c.GardenTLSCAFile != "" && !filepath.IsAbs(c.GardenTLSCAFile) {
		c.GardenTLSCAFile = filepath.Join(dir, c.GardenTLSCAFile)
	}
	return Home{Dir: dir, Config: c}, nil
}

// exactHomeNames refuses any key that is not spelled exactly as documented.
// encoding/json matches field names case-insensitively, so an alias such as
// GARDEN_ENDPOINT would otherwise silently override garden_endpoint. Defaults
// keys are harness names, so they are checked against the participant rule.
func exactHomeNames(data []byte) error {
	top, err := exactKeys(data, "garden_endpoint", "garden_tls_ca_file", "garden_tls_server_name", "instance_id", "scope", "classification", "defaults")
	if err != nil {
		return err
	}
	if raw, ok := top["scope"]; ok {
		scope, err := exactKeys(raw, "realm", "segments")
		if err != nil {
			return err
		}
		if raw, ok := scope["segments"]; ok {
			var segments []json.RawMessage
			if err := json.Unmarshal(raw, &segments); err != nil {
				return errors.New("invalid garden.json")
			}
			for _, segment := range segments {
				if _, err := exactKeys(segment, "kind", "identifier"); err != nil {
					return err
				}
			}
		}
	}
	if raw, ok := top["defaults"]; ok {
		var defaults map[string]json.RawMessage
		if err := json.Unmarshal(raw, &defaults); err != nil {
			return errors.New("invalid garden.json")
		}
		for name := range defaults {
			if !participantName.MatchString(name) {
				return errors.New("invalid default harness name")
			}
		}
	}
	return nil
}

// exactKeys decodes one JSON object and refuses keys outside allowed, case-sensitively.
func exactKeys(data []byte, allowed ...string) (map[string]json.RawMessage, error) {
	var fields map[string]json.RawMessage
	if err := json.Unmarshal(data, &fields); err != nil {
		return nil, errors.New("invalid garden.json")
	}
	for name := range fields {
		if !slices.Contains(allowed, name) {
			return nil, errors.New("unknown or misspelled garden.json field")
		}
	}
	return fields, nil
}

// TokenFile is the protected credential for one identity.
func (h Home) TokenFile(identity string) string {
	return filepath.Join(h.Dir, "identities", identity+".token")
}

// Choice is the Garden identity selected for one harness session.
type Choice struct {
	Name     string
	Explicit bool
}

// ChooseIdentity applies GARDEN_IDENTITY first, then the harness default.
func ChooseIdentity(explicit string, defaults map[string]string, harness string) (Choice, error) {
	if explicit != "" {
		if !participantName.MatchString(explicit) {
			return Choice{}, errInvalidIdentity
		}
		return Choice{Name: explicit, Explicit: true}, nil
	}
	name, ok := defaults[harness]
	if !ok {
		return Choice{}, errors.New("no default identity for " + harness)
	}
	if !participantName.MatchString(name) {
		return Choice{}, errInvalidIdentity
	}
	return Choice{Name: name}, nil
}
