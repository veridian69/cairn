// Package remotecli opens a human's remote Garden session from a Garden home, for the --remote
// mode of say, history, watch and chat. It reads the home and the identity's token only; it never
// reads the local daemon's files.
package remotecli

import (
	"context"
	"errors"
	"fmt"
	"io/fs"
	"os"
	"path/filepath"
	"reflect"
	"sync"

	"github.com/veridian69/cairn/garden/internal/attention"
	"github.com/veridian69/cairn/garden/internal/garden"
)

// Gateway is the part of the Garden client the remote commands use. *garden.Client satisfies it.
type Gateway interface {
	Status(context.Context) (garden.StatusResult, error)
	Read(context.Context, garden.ReadArgs) (garden.ReadResult, error)
	Send(context.Context, garden.SendArgs) (garden.SendResult, error)
	Who(context.Context) (garden.WhoResult, error)
	Supports(tool string) bool
	Close() error
}

// Session is an authenticated connection for one Garden identity, whose binding matched the
// home's garden.json when it was opened.
type Session struct {
	// Identity is the Garden participant name the session speaks as.
	Identity string
	// Binding is the instance, scope and classification the gateway reported at open.
	Binding garden.Binding

	gateway   Gateway
	closeOnce sync.Once
	closeErr  error
}

// NewSession wraps an already authenticated gateway. Open is the normal entry point; this exists
// so callers can substitute a gateway in tests.
func NewSession(identity string, binding garden.Binding, gateway Gateway) *Session {
	return &Session{Identity: identity, Binding: binding, gateway: gateway}
}

// Seams for tests. They are the only paths Open uses to reach the environment, the dialer and the
// terminal.
var (
	homeDir        = attention.ResolveHomeDir
	dial           = dialGateway
	readPassphrase = attention.ReadPassphrase
	getenv         = os.Getenv
)

func dialGateway(ctx context.Context, cfg garden.ClientConfig) (Gateway, error) {
	client, err := garden.Dial(ctx, cfg)
	if err != nil {
		return nil, err
	}
	return client, nil
}

// Open reads the Garden home, selects the identity (GARDEN_IDENTITY, else the cli default),
// unlocks its token (a sealed identity asks for the passphrase; a .token file is read directly),
// dials the gateway and requires its binding and participant to match the home exactly. The
// token is held only in memory. The context must stay alive for the life of the session.
func Open(ctx context.Context) (*Session, error) {
	dir, err := homeDir()
	if err != nil {
		return nil, err
	}
	if _, err := os.Lstat(filepath.Join(dir, "garden.json")); errors.Is(err, fs.ErrNotExist) {
		return nil, fmt.Errorf("no Garden home at %s; run `garden join` with an invite first", dir)
	}
	home, err := attention.LoadHome(dir)
	if err != nil {
		return nil, fmt.Errorf("Garden home %s: %w", dir, err)
	}
	name, err := selectIdentity(home)
	if err != nil {
		return nil, err
	}
	if err := checkCAFile(home.Config.GardenTLSCAFile); err != nil {
		return nil, err
	}
	token, err := identityToken(home, name)
	if err != nil {
		return nil, err
	}
	gateway, err := dial(ctx, garden.ClientConfig{
		Endpoint:      home.Config.GardenEndpoint,
		Token:         token,
		TLSCAFile:     home.Config.GardenTLSCAFile,
		TLSServerName: home.Config.GardenTLSServerName,
	})
	if err != nil {
		return nil, fmt.Errorf("connecting to Garden: %w", err)
	}
	status, err := gateway.Status(ctx)
	if err != nil {
		_ = gateway.Close()
		return nil, fmt.Errorf("Garden status: %w", err)
	}
	if status.Participant != name {
		_ = gateway.Close()
		return nil, fmt.Errorf("the Garden token is for %q, not the identity %q; refusing to use it", status.Participant, name)
	}
	if !bindingMatches(status.Binding, home.Config) {
		_ = gateway.Close()
		return nil, errors.New("the Garden binding (instance, scope or classification) differs from garden.json; refusing to use this gateway")
	}
	return NewSession(name, status.Binding, gateway), nil
}

// selectIdentity applies GARDEN_IDENTITY, else the home's cli default.
func selectIdentity(home attention.Home) (string, error) {
	explicit := getenv("GARDEN_IDENTITY")
	if explicit == "" {
		if _, ok := home.Config.Defaults["cli"]; !ok {
			return "", errors.New("no cli default identity in garden.json; run `garden join` with --harness cli, or set GARDEN_IDENTITY")
		}
	}
	choice, err := attention.ChooseIdentity(explicit, home.Config.Defaults, "cli")
	if err != nil {
		return "", err
	}
	return choice.Name, nil
}

// bindingMatches requires the gateway's instance, scope and classification to equal the home's.
func bindingMatches(b garden.Binding, c attention.HomeConfig) bool {
	return b.InstanceID == c.InstanceID && b.Classification == c.Classification && reflect.DeepEqual(b.Scope, c.Scope)
}

// Read, Send, Who and Supports delegate to the gateway.

func (s *Session) Read(ctx context.Context, a garden.ReadArgs) (garden.ReadResult, error) {
	return s.gateway.Read(ctx, a)
}

func (s *Session) Send(ctx context.Context, a garden.SendArgs) (garden.SendResult, error) {
	return s.gateway.Send(ctx, a)
}

func (s *Session) Who(ctx context.Context) (garden.WhoResult, error) {
	return s.gateway.Who(ctx)
}

func (s *Session) Supports(tool string) bool { return s.gateway.Supports(tool) }

// Close releases the gateway. It is safe to call more than once.
func (s *Session) Close() error {
	s.closeOnce.Do(func() { s.closeErr = s.gateway.Close() })
	return s.closeErr
}
