package attention

import (
	"bytes"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"reflect"
	"slices"
	"strings"
)

// legacyHarnesses are the harness names a profile adapter may map to.
var legacyHarnesses = []string{"claude", "codex", "opencode"}

// ImportIdentity is one participant copied into the Garden home.
type ImportIdentity struct {
	Name    string
	Harness string
	Default bool
}

// ImportResult lists what an import wrote or deliberately left alone. It holds
// paths and identity names only, never token contents.
type ImportResult struct {
	Home        string
	Already     bool
	Written     []string
	Identities  []ImportIdentity
	Ignored     []string
	NotMigrated []string
}

// ImportLegacyHome migrates the per-profile layout (<from>/<name>.json, credentials/,
// tls/) into the Garden home at dest. It is a one-shot, idempotent copy. Every refusal
// is decided before the first write, so a refused import creates nothing. Writes are
// confined to the home through an os.Root; garden.json is written last, so an interrupted
// import can be re-run to completion. defaults maps a harness to its default identity and
// is required only where two profiles share an adapter.
func ImportLegacyHome(from, dest string, defaults map[string]string) (ImportResult, error) {
	result := ImportResult{Home: dest}
	src, err := filepath.Abs(from)
	if err != nil {
		return result, errors.New("invalid source directory")
	}
	dst, err := filepath.Abs(dest)
	if err != nil {
		return result, errors.New("invalid Garden home")
	}
	if nested(resolveExisting(src), resolveExisting(dst)) {
		return result, errors.New("the source and the Garden home must be separate directories, neither inside the other")
	}
	for name := range defaults {
		if !slices.Contains(legacyHarnesses, name) {
			return result, fmt.Errorf("--default: unknown harness %q", name)
		}
	}
	entries, err := os.ReadDir(src)
	if err != nil {
		return result, errors.New("cannot read the source directory")
	}

	var profiles []Profile
	var names []string
	for _, entry := range entries {
		name := entry.Name()
		if strings.Contains(name, ".bak-") {
			result.Ignored = append(result.Ignored, name)
			continue
		}
		if entry.IsDir() || !strings.HasSuffix(name, ".json") {
			continue
		}
		path := filepath.Join(src, name)
		data, err := ownedSourceBytes(path, 32*1024, "profile", false)
		if err != nil {
			return result, fmt.Errorf("profile %s: %w", name, err)
		}
		p, err := parseProfile(data, path)
		if err != nil {
			return result, fmt.Errorf("profile %s: %w", name, err)
		}
		// The participant becomes a path component under identities/, so it must pass the
		// gateway rule before anything else is decided.
		if !participantName.MatchString(p.Participant) {
			return result, fmt.Errorf("profile %s: participant %q is not a valid Garden identity name", name, p.Participant)
		}
		profiles = append(profiles, p)
		names = append(names, name)
	}
	if len(profiles) == 0 {
		return result, errors.New("no profiles found in the source directory")
	}

	cas := make([][]byte, len(profiles))
	for i, p := range profiles {
		if p.GardenTLSCAFile == "" {
			continue
		}
		cas[i], err = ownedSourceBytes(p.GardenTLSCAFile, 64*1024, "TLS CA file", false)
		if err != nil {
			return result, fmt.Errorf("profile %s: %w", names[i], err)
		}
	}
	shared := profiles[0]
	for i, p := range profiles[1:] {
		field := ""
		switch {
		case p.GardenEndpoint != shared.GardenEndpoint:
			field = "garden_endpoint"
		case p.GardenTLSServerName != shared.GardenTLSServerName:
			field = "garden_tls_server_name"
		case p.InstanceID != shared.InstanceID:
			field = "instance_id"
		case !reflect.DeepEqual(p.Scope, shared.Scope):
			field = "scope"
		case p.Classification != shared.Classification:
			field = "classification"
		case !bytes.Equal(cas[0], cas[i+1]) || (cas[0] == nil) != (cas[i+1] == nil):
			field = "garden_tls_ca_file"
		}
		if field != "" {
			return result, fmt.Errorf("profiles %s and %s disagree on %s; refusing to import", names[0], names[i+1], field)
		}
	}

	// Harness defaults: one profile per adapter, or an explicit --default.
	byHarness := map[string][]int{}
	for i, p := range profiles {
		if !slices.Contains(legacyHarnesses, p.Adapter) {
			return result, fmt.Errorf("profile %s uses adapter %q, which has no harness default", names[i], p.Adapter)
		}
		byHarness[p.Adapter] = append(byHarness[p.Adapter], i)
	}
	for harness := range defaults {
		if len(byHarness[harness]) == 0 {
			return result, fmt.Errorf("--default %s: no profile uses that adapter", harness)
		}
	}
	chosen := map[string]string{}
	for _, harness := range legacyHarnesses {
		indexes := byHarness[harness]
		switch {
		case len(indexes) == 0:
			continue
		case len(indexes) == 1 && defaults[harness] == "":
			chosen[harness] = profiles[indexes[0]].Participant
		case defaults[harness] == "":
			var both []string
			for _, i := range indexes {
				both = append(both, profiles[i].Participant)
			}
			return result, fmt.Errorf("profiles %s share adapter %s; choose one with --default %s=<name>", strings.Join(both, ", "), harness, harness)
		default:
			found := false
			for _, i := range indexes {
				found = found || profiles[i].Participant == defaults[harness]
			}
			if !found {
				return result, fmt.Errorf("--default %s=%s: no %s profile has that participant", harness, defaults[harness], harness)
			}
			chosen[harness] = defaults[harness]
		}
	}

	// Participants must be unique; read each token now, through the descriptor check.
	seen := map[string]bool{}
	type token struct {
		name string
		raw  []byte
	}
	var tokens []token
	for i, p := range profiles {
		if seen[p.Participant] {
			return result, fmt.Errorf("two profiles name participant %s", p.Participant)
		}
		seen[p.Participant] = true
		raw, err := privateSourceBytes(p.CredentialFile, 4096)
		if err != nil {
			return result, fmt.Errorf("profile %s: %w", names[i], err)
		}
		tokens = append(tokens, token{name: p.Participant, raw: raw})
	}

	// Obsolete material is reported, never migrated.
	if _, err := os.Lstat(filepath.Join(src, "credentials", "opencode-server.password")); err == nil {
		result.NotMigrated = append(result.NotMigrated, "credentials/opencode-server.password (obsolete)")
	}

	first := profiles[0]
	config := HomeConfig{
		GardenEndpoint:      first.GardenEndpoint,
		GardenTLSServerName: first.GardenTLSServerName,
		InstanceID:          first.InstanceID,
		Scope:               first.Scope,
		Classification:      first.Classification,
		Defaults:            chosen,
	}
	ca := cas[0]
	if ca != nil {
		config.GardenTLSCAFile = "tls/internal-ca.pem"
	}
	configJSON, err := json.MarshalIndent(config, "", "  ")
	if err != nil {
		return result, errors.New("cannot encode garden.json")
	}
	configJSON = append(configJSON, '\n')

	type write struct {
		rel     string
		data    []byte
		private bool
		dir     string
	}
	var writes []write
	if ca != nil {
		writes = append(writes, write{rel: "tls/internal-ca.pem", data: ca, dir: "tls"})
	}
	for _, t := range tokens {
		writes = append(writes, write{rel: "identities/" + t.name + ".token", data: t.raw, private: true, dir: "identities"})
	}
	writes = append(writes, write{rel: "garden.json", data: configJSON})

	// One lock covers the whole transaction: planning reads, every write, rollback and
	// verification. Another import or add-identity on this home waits, then sees our state.
	root, err := existingHome(dst)
	if err != nil {
		return result, err
	}
	if root == nil {
		if root, err = ensureHome(dst); err != nil {
			return result, err
		}
	}
	defer closeRoot(root)
	unlock, err := lockHome(root)
	if err != nil {
		return result, err
	}
	defer unlock()
	for _, dir := range []string{"tls", "identities"} {
		if err := rootExistingPrivateDir(root, dir); err != nil {
			return result, err
		}
	}
	var pending []write
	for _, w := range writes {
		current, exists, err := rootReadFile(root, w.rel, max(int64(len(w.data)), 64*1024), w.private)
		if err != nil {
			return result, err
		}
		switch {
		case !exists:
			pending = append(pending, w)
		case !sameBytes(current, w.data):
			return result, fmt.Errorf("existing file differs; refusing to overwrite: %s", w.rel)
		}
	}

	// Write phase. Conflicts were refused above under the lock, so the CA and tokens are written
	// first and garden.json, published without replacement, is the commit point: a harness that
	// sees garden.json also finds what it references. Anything this run created is removed if a
	// later step refuses.
	var created []string
	fail := func(err error) (ImportResult, error) {
		for i := len(created) - 1; i >= 0; i-- {
			_ = root.Remove(created[i])
		}
		return ImportResult{Home: dst}, err
	}
	for _, w := range pending {
		if w.dir != "" {
			if err := rootPrivateDir(root, w.dir); err != nil {
				return fail(err)
			}
		}
		var made bool
		if w.rel == "garden.json" {
			publishHook(w.rel)
			made, err = rootPublishLink(root, w.rel, w.data, false)
		} else {
			made, err = rootPublishNew(root, w.rel, w.data, w.private)
		}
		if err != nil {
			return fail(err)
		}
		if made {
			created = append(created, w.rel)
			result.Written = append(result.Written, filepath.Join(dst, w.rel))
		}
	}
	result.Already = len(created) == 0

	// Verify the result the way the runtime reads it.
	home, err := LoadHome(dst)
	if err != nil {
		return result, fmt.Errorf("imported garden.json does not load: %w", err)
	}
	for _, t := range tokens {
		value, err := ReadCredential(home.TokenFile(t.name))
		if err != nil || value != string(bytes.TrimSpace(t.raw)) {
			return result, fmt.Errorf("imported token for %s is not readable", t.name)
		}
	}

	for _, name := range legacyHarnesses {
		if participant, ok := chosen[name]; ok {
			result.Identities = append(result.Identities, ImportIdentity{Name: participant, Harness: name, Default: true})
		}
	}
	for _, t := range tokens {
		if !slices.ContainsFunc(result.Identities, func(id ImportIdentity) bool { return id.Name == t.name }) {
			result.Identities = append(result.Identities, ImportIdentity{Name: t.name})
		}
	}
	return result, nil
}

// publishHook is a test seam: it runs before garden.json is published.
var publishHook = func(string) {}

// closeRoot closes a possibly nil root.
func closeRoot(root *os.Root) {
	if root != nil {
		_ = root.Close()
	}
}

// resolveExisting resolves symlinks in the longest existing prefix of an absolute path, so a
// Garden home that does not exist yet still compares by where it will be created.
func resolveExisting(path string) string {
	rest := ""
	for {
		if resolved, err := filepath.EvalSymlinks(path); err == nil {
			return filepath.Join(resolved, rest)
		}
		parent := filepath.Dir(path)
		if parent == path {
			return filepath.Join(path, rest)
		}
		rest = filepath.Join(filepath.Base(path), rest)
		path = parent
	}
}

// nested reports whether a and b are the same directory or one contains the other.
func nested(a, b string) bool {
	inside := func(outer, inner string) bool {
		rel, err := filepath.Rel(outer, inner)
		return err == nil && rel != ".." && !strings.HasPrefix(rel, ".."+string(filepath.Separator))
	}
	return inside(a, b) || inside(b, a)
}
