package remotecli

import (
	"context"
	"errors"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/veridian69/cairn/garden/internal/attention"
	"github.com/veridian69/cairn/garden/internal/garden"
	"github.com/veridian69/cairn/garden/internal/gardenauth"
)

const (
	testEndpoint = "https://garden.example.test/mcp"
	testToken    = "gardenid-token-for-tests-0123456789"
	testPass     = "correct horse battery"
)

// fixture is a Garden home in a temporary directory with the seams replaced.
type fixture struct {
	t       *testing.T
	dir     string
	room    *fakeRoom
	dialed  []garden.ClientConfig
	prompts []string
	answers []answer
}

type answer struct {
	pass []byte
	err  error
}

// newFixture writes a valid garden.json with cli default operator and points GARDEN_HOME at it.
func newFixture(t *testing.T) *fixture {
	t.Helper()
	dir := t.TempDir()
	t.Setenv("GARDEN_HOME", dir)
	t.Setenv("GARDEN_IDENTITY", "")
	if err := os.Mkdir(filepath.Join(dir, "identities"), 0700); err != nil {
		t.Fatal(err)
	}
	b := testBinding()
	config := `{
  "garden_endpoint": "` + testEndpoint + `",
  "instance_id": "` + b.InstanceID + `",
  "scope": {"realm": "cairn", "segments": [{"kind": "project", "identifier": "garden"}]},
  "classification": "internal",
  "defaults": {"cli": "operator", "claude": "claude"}
}`
	if err := os.WriteFile(filepath.Join(dir, "garden.json"), []byte(config), 0600); err != nil {
		t.Fatal(err)
	}
	f := &fixture{t: t, dir: dir, room: newFakeRoom("gen-1")}
	f.room.status = garden.StatusResult{Binding: b, Participant: "operator", Generation: "gen-1"}

	oldDial, oldPass := dial, readPassphrase
	dial = func(_ context.Context, cfg garden.ClientConfig) (Gateway, error) {
		f.dialed = append(f.dialed, cfg)
		return f.room, nil
	}
	readPassphrase = func(prompt string, confirm bool) ([]byte, error) {
		f.prompts = append(f.prompts, prompt)
		if confirm {
			f.t.Errorf("sealed identities must be unlocked without confirmation")
		}
		if len(f.answers) == 0 {
			return nil, errors.New("no terminal in this test")
		}
		next := f.answers[0]
		f.answers = f.answers[1:]
		return next.pass, next.err
	}
	t.Cleanup(func() { dial, readPassphrase = oldDial, oldPass })
	return f
}

func (f *fixture) writeToken(name, token string) {
	f.t.Helper()
	if err := os.WriteFile(filepath.Join(f.dir, "identities", name+".token"), []byte(token+"\n"), 0600); err != nil {
		f.t.Fatal(err)
	}
}

func (f *fixture) writeSealed(name, token string, mode os.FileMode) {
	f.t.Helper()
	data, err := attention.SealIdentityToken(name, token, []byte(testPass))
	if err != nil {
		f.t.Fatal(err)
	}
	path := filepath.Join(f.dir, "identities", name+".sealed")
	if err := os.WriteFile(path, data, mode); err != nil {
		f.t.Fatal(err)
	}
	if err := os.Chmod(path, mode); err != nil {
		f.t.Fatal(err)
	}
}

func TestOpenUsesCliDefaultIdentity(t *testing.T) {
	f := newFixture(t)
	f.writeToken("operator", testToken)

	s, err := Open(context.Background())
	if err != nil {
		t.Fatalf("Open: %v", err)
	}
	defer s.Close()
	if s.Identity != "operator" {
		t.Fatalf("Identity = %q, want operator", s.Identity)
	}
	if len(f.dialed) != 1 || f.dialed[0].Token != testToken {
		t.Fatalf("dialed = %+v, want the operator token", f.dialed)
	}
	if f.dialed[0].Endpoint != testEndpoint {
		t.Fatalf("endpoint = %q", f.dialed[0].Endpoint)
	}
	if s.Binding.InstanceID != testBinding().InstanceID {
		t.Fatalf("binding = %+v", s.Binding)
	}
}

func TestOpenGardenIdentityOverridesCliDefault(t *testing.T) {
	f := newFixture(t)
	f.writeToken("operator", "operator-token-0123456789-abcdef")
	f.writeToken("zoe", testToken)
	f.room.status.Participant = "zoe"
	t.Setenv("GARDEN_IDENTITY", "zoe")

	s, err := Open(context.Background())
	if err != nil {
		t.Fatalf("Open: %v", err)
	}
	defer s.Close()
	if s.Identity != "zoe" || f.dialed[0].Token != testToken {
		t.Fatalf("identity %q with token %q, want zoe", s.Identity, f.dialed[0].Token)
	}
}

func TestOpenRefusesWithoutCliDefault(t *testing.T) {
	f := newFixture(t)
	f.writeToken("operator", testToken)
	config := `{
  "garden_endpoint": "` + testEndpoint + `",
  "instance_id": "` + testBinding().InstanceID + `",
  "scope": {"realm": "cairn", "segments": [{"kind": "project", "identifier": "garden"}]},
  "classification": "internal",
  "defaults": {"claude": "claude"}
}`
	if err := os.WriteFile(filepath.Join(f.dir, "garden.json"), []byte(config), 0600); err != nil {
		t.Fatal(err)
	}

	_, err := Open(context.Background())
	if err == nil || !strings.Contains(err.Error(), "no cli default identity") {
		t.Fatalf("Open error = %v, want a missing cli default", err)
	}
	if len(f.dialed) != 0 {
		t.Fatal("no connection may be attempted without an identity")
	}
}

func TestOpenRefusesMissingHome(t *testing.T) {
	t.Setenv("GARDEN_HOME", t.TempDir())
	_, err := Open(context.Background())
	if err == nil || !strings.Contains(err.Error(), "no Garden home") {
		t.Fatalf("Open error = %v, want a missing home", err)
	}
}

func TestOpenRefusesMissingIdentityFile(t *testing.T) {
	f := newFixture(t)
	_, err := Open(context.Background())
	if err == nil || !strings.Contains(err.Error(), "no identity file for operator") {
		t.Fatalf("Open error = %v, want a missing identity file", err)
	}
	if len(f.prompts) != 0 || len(f.dialed) != 0 {
		t.Fatal("a missing identity must not ask for a passphrase or connect")
	}
}

func TestOpenRefusesTokenAndSealedTogether(t *testing.T) {
	f := newFixture(t)
	f.writeToken("operator", testToken)
	f.writeSealed("operator", testToken, 0600)

	_, err := Open(context.Background())
	if err == nil || !strings.Contains(err.Error(), "both a .token and a .sealed") {
		t.Fatalf("Open error = %v, want a refusal for both files", err)
	}
	if len(f.prompts) != 0 {
		t.Fatal("ambiguous identity files must not ask for a passphrase")
	}
}

func TestOpenUnlocksSealedIdentityWithPassphrase(t *testing.T) {
	f := newFixture(t)
	f.writeSealed("operator", testToken, 0600)
	f.answers = []answer{{pass: []byte(testPass)}}

	s, err := Open(context.Background())
	if err != nil {
		t.Fatalf("Open: %v", err)
	}
	defer s.Close()
	if len(f.prompts) != 1 || f.prompts[0] != "passphrase for operator: " {
		t.Fatalf("prompts = %q", f.prompts)
	}
	if len(f.dialed) != 1 || f.dialed[0].Token != testToken {
		t.Fatal("the unlocked token must be used to dial")
	}
}

func TestOpenAllowsThreePassphraseAttempts(t *testing.T) {
	f := newFixture(t)
	f.writeSealed("operator", testToken, 0600)
	f.answers = []answer{{pass: []byte("wrong passphrase one")}, {pass: []byte("wrong passphrase two")}, {pass: []byte(testPass)}}

	s, err := Open(context.Background())
	if err != nil {
		t.Fatalf("Open after two wrong passphrases: %v", err)
	}
	defer s.Close()
	if len(f.prompts) != 3 {
		t.Fatalf("passphrase prompts = %d, want 3", len(f.prompts))
	}
}

func TestOpenRefusesAfterThreeWrongPassphrases(t *testing.T) {
	f := newFixture(t)
	f.writeSealed("operator", testToken, 0600)
	f.answers = []answer{{pass: []byte("wrong passphrase one")}, {pass: []byte("wrong passphrase two")}, {pass: []byte("wrong passphrase three")}}

	_, err := Open(context.Background())
	if err == nil || !strings.Contains(err.Error(), "after 3 attempts") {
		t.Fatalf("Open error = %v, want a refusal after three attempts", err)
	}
	if len(f.prompts) != 3 {
		t.Fatalf("passphrase prompts = %d, want exactly 3", len(f.prompts))
	}
	if len(f.dialed) != 0 {
		t.Fatal("no connection may be attempted after failed unlocks")
	}
	for _, secret := range []string{testToken, "wrong passphrase one", "wrong passphrase two", "wrong passphrase three"} {
		if strings.Contains(err.Error(), secret) {
			t.Fatalf("error leaks %q: %v", secret, err)
		}
	}
}

func TestOpenSealedNeedsATerminal(t *testing.T) {
	f := newFixture(t)
	f.writeSealed("operator", testToken, 0600)
	f.answers = []answer{{err: errors.New("a sealed Garden identity needs a terminal")}}

	_, err := Open(context.Background())
	if err == nil || !strings.Contains(err.Error(), "needs a terminal") {
		t.Fatalf("Open error = %v, want the terminal refusal", err)
	}
	if len(f.prompts) != 1 || len(f.dialed) != 0 {
		t.Fatalf("prompts=%d dialed=%d", len(f.prompts), len(f.dialed))
	}
}

func TestOpenRefusesSealedFileThatIsNotPrivate(t *testing.T) {
	f := newFixture(t)
	f.writeSealed("operator", testToken, 0644)
	f.answers = []answer{{pass: []byte(testPass)}}

	_, err := Open(context.Background())
	if err == nil || !strings.Contains(err.Error(), "must be private") {
		t.Fatalf("Open error = %v, want a privacy refusal", err)
	}
	if len(f.prompts) != 0 {
		t.Fatal("an unsafe sealed file must not ask for a passphrase")
	}
}

func TestOpenRefusesSealedFileForAnotherIdentity(t *testing.T) {
	f := newFixture(t)
	// A sealed file copied under another name must not open, because the name is bound into the seal.
	data, err := attention.SealIdentityToken("zoe", testToken, []byte(testPass))
	if err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(f.dir, "identities", "operator.sealed"), data, 0600); err != nil {
		t.Fatal(err)
	}
	f.answers = []answer{{pass: []byte(testPass)}, {pass: []byte(testPass)}, {pass: []byte(testPass)}}

	_, err = Open(context.Background())
	if err == nil || len(f.dialed) != 0 {
		t.Fatalf("Open error = %v, dialed = %d; a renamed sealed file must not unlock", err, len(f.dialed))
	}
}

func TestOpenRefusesBindingMismatch(t *testing.T) {
	cases := map[string]func(*garden.Binding){
		"instance": func(b *garden.Binding) { b.InstanceID = "11111111-2222-4333-8444-555555555555" },
		"scope": func(b *garden.Binding) {
			b.Scope.Segments = []gardenauth.Segment{{Kind: "project", Identifier: "other"}}
		},
		"classification": func(b *garden.Binding) { b.Classification = "restricted" },
	}
	for name, change := range cases {
		t.Run(name, func(t *testing.T) {
			f := newFixture(t)
			f.writeToken("operator", testToken)
			change(&f.room.status.Binding)

			_, err := Open(context.Background())
			if err == nil || !strings.Contains(err.Error(), "binding") {
				t.Fatalf("Open error = %v, want a binding refusal", err)
			}
			if f.room.closed != 1 {
				t.Fatalf("gateway closed %d times, want 1", f.room.closed)
			}
		})
	}
}

func TestOpenRefusesParticipantMismatch(t *testing.T) {
	f := newFixture(t)
	f.writeToken("operator", testToken)
	f.room.status.Participant = "zoe"

	_, err := Open(context.Background())
	if err == nil || !strings.Contains(err.Error(), "not the identity") {
		t.Fatalf("Open error = %v, want a participant refusal", err)
	}
	if f.room.closed != 1 {
		t.Fatalf("gateway closed %d times, want 1", f.room.closed)
	}
}

func TestOpenRefusesCAFileThatIsWritableByOthers(t *testing.T) {
	f := newFixture(t)
	f.writeToken("operator", testToken)
	ca := filepath.Join(f.dir, "internal-ca.pem")
	if err := os.WriteFile(ca, []byte("not really a certificate"), 0666); err != nil {
		t.Fatal(err)
	}
	if err := os.Chmod(ca, 0666); err != nil {
		t.Fatal(err)
	}
	config := `{
  "garden_endpoint": "` + testEndpoint + `",
  "garden_tls_ca_file": "` + ca + `",
  "instance_id": "` + testBinding().InstanceID + `",
  "scope": {"realm": "cairn", "segments": [{"kind": "project", "identifier": "garden"}]},
  "classification": "internal",
  "defaults": {"cli": "operator"}
}`
	if err := os.WriteFile(filepath.Join(f.dir, "garden.json"), []byte(config), 0600); err != nil {
		t.Fatal(err)
	}

	_, err := Open(context.Background())
	if err == nil || !strings.Contains(err.Error(), "CA file") {
		t.Fatalf("Open error = %v, want a CA refusal", err)
	}
	if len(f.dialed) != 0 {
		t.Fatal("an unsafe CA file must stop the connection")
	}
}

func TestSessionCloseIsIdempotent(t *testing.T) {
	room := newFakeRoom("gen-1")
	s := NewSession("operator", testBinding(), room)
	if err := s.Close(); err != nil {
		t.Fatal(err)
	}
	if err := s.Close(); err != nil {
		t.Fatal(err)
	}
	if room.closed != 1 {
		t.Fatalf("gateway closed %d times, want 1", room.closed)
	}
}
