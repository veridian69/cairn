package garden

import (
	"context"
	"strings"
	"sync"
	"testing"

	"github.com/google/uuid"
)

const principalCarol = "44444444-4444-4444-8444-444444444444"

// withPrincipals returns the fixture's configuration with a different principal map.
func withPrincipals(cfg Config, principals map[string]string) Config {
	cfg.Principals = principals
	return cfg
}

// rowCount counts inbox rows for a principal; removed principals must keep theirs.
func rowCount(t *testing.T, f *fixture, principal string) int {
	t.Helper()
	var n int
	if err := f.srv.sql.QueryRow("SELECT COUNT(*) FROM garden_inbox WHERE principal=?", principal).Scan(&n); err != nil {
		t.Fatal(err)
	}
	return n
}

// participantCount counts participants with a name, to prove a refused reload left no rows.
func participantCount(t *testing.T, f *fixture, name string) int {
	t.Helper()
	var n int
	if err := f.srv.sql.QueryRow("SELECT COUNT(*) FROM participants WHERE name=?", name).Scan(&n); err != nil {
		t.Fatal(err)
	}
	return n
}

func TestReloadAddsPrincipalWithInboxAtTail(t *testing.T) {
	f := newFixture(t)
	ctx := context.Background()
	f.mu.Lock()
	f.tokens["cairn1.carol"] = principalCarol
	f.mu.Unlock()
	if _, err := f.srv.authenticate(ctx, "cairn1.carol"); err == nil {
		t.Fatal("unconfigured principal authenticated before reload")
	}
	// A message sent before carol is enrolled must not replay into her new inbox.
	alice := f.client("alice")
	if _, err := alice.Send(ctx, SendArgs{Content: "before carol", Recipients: []string{"bob"}}); err != nil {
		t.Fatal(err)
	}
	principals := map[string]string{principalA: "alice", principalB: "bob", principalCarol: "carol"}
	if err := f.srv.Reload(ctx, withPrincipals(f.cfg, principals)); err != nil {
		t.Fatalf("reload: %v", err)
	}
	carol := f.client("cairn1.carol")
	status, err := carol.Status(ctx)
	if err != nil || status.Participant != "carol" || !strings.Contains(strings.Join(status.Participants, ","), "carol") {
		t.Fatalf("carol status after reload: %+v %v", status, err)
	}
	if rowCount(t, f, principalCarol) != 1 {
		t.Fatal("reload did not create carol's inbox")
	}
	owner := uuid.NewString()
	if p, err := carol.Poll(ctx, PollArgs{ConsumerID: owner}); err != nil || p.Message != nil {
		t.Fatalf("carol replayed pre-enrolment history: %+v %v", p, err)
	}
	sent, err := alice.Send(ctx, SendArgs{Content: "after carol", Recipients: []string{"carol"}})
	if err != nil {
		t.Fatal(err)
	}
	p, err := carol.Poll(ctx, PollArgs{ConsumerID: owner})
	if err != nil || p.Message == nil || p.Message.ID != sent.ID {
		t.Fatalf("carol did not receive post-reload attention: %+v %v", p, err)
	}
}

func TestReloadRefusesChangesOutsidePrincipalsAndLeavesStateUnchanged(t *testing.T) {
	cases := []struct {
		name  string
		key   string
		apply func(cfg *Config, t *testing.T)
	}{
		{"listen", "listen", func(c *Config, _ *testing.T) { c.Listen = "127.0.0.1:1" }},
		{"data_dir", "data_dir", func(c *Config, t *testing.T) { c.DataDir = t.TempDir() }},
		{"public_endpoint", "public_endpoint", func(c *Config, _ *testing.T) { c.PublicEndpoint = "https://garden.example/mcp" }},
		{"invites_dir", "invites_dir", func(c *Config, t *testing.T) { c.InvitesDir = t.TempDir() }},
		{"loop_guard", "loop_guard", func(c *Config, _ *testing.T) { c.LoopGuard = LoopGuard{ThreadDepth: 5} }},
		{"classification", "auth", func(c *Config, _ *testing.T) { c.Auth.Classification = "restricted" }},
		{"instance", "auth", func(c *Config, _ *testing.T) { c.Auth.InstanceID = uuid.NewString() }},
		{"rename_existing", "", func(c *Config, _ *testing.T) {
			c.Principals = map[string]string{principalA: "mallory", principalB: "bob"}
		}},
		{"rename_with_addition", "", func(c *Config, _ *testing.T) {
			c.Principals = map[string]string{principalA: "mallory", principalB: "bob", principalCarol: "carol"}
		}},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			f := newFixture(t)
			ctx := context.Background()
			f.mu.Lock()
			f.tokens["cairn1.carol"] = principalCarol
			f.mu.Unlock()
			cfg := f.cfg
			cfg.Principals = map[string]string{principalA: "alice", principalB: "bob", principalCarol: "carol"}
			tc.apply(&cfg, t)
			err := f.srv.Reload(ctx, cfg)
			if err == nil {
				t.Fatal("reload accepted a refused change")
			}
			if tc.key != "" && !strings.Contains(err.Error(), `"`+tc.key+`"`) {
				t.Fatalf("refusal does not name field %q: %v", tc.key, err)
			}
			if got := f.srv.principalMap(); len(got) != 2 || got[principalA] != "alice" || got[principalB] != "bob" {
				t.Fatalf("refused reload changed principals: %v", got)
			}
			if _, err := f.srv.authenticate(ctx, "cairn1.carol"); err == nil {
				t.Fatal("refused reload admitted carol")
			}
			if participantCount(t, f, "carol") != 0 || participantCount(t, f, "mallory") != 0 {
				t.Fatal("refused reload left participant rows")
			}
			if _, err := f.client("alice").Status(ctx); err != nil {
				t.Fatalf("running configuration stopped serving: %v", err)
			}
		})
	}
}

func TestReloadAcceptsUnchangedConfigurationAndVersion(t *testing.T) {
	f := newFixture(t)
	cfg := f.cfg
	cfg.Principals = map[string]string{principalA: "alice", principalB: "bob"}
	cfg.Version = "9.9.9"
	if err := f.srv.Reload(context.Background(), cfg); err != nil {
		t.Fatalf("unchanged reload refused: %v", err)
	}
}

func TestReloadRejectsInvalidPrincipalsWithoutChange(t *testing.T) {
	f := newFixture(t)
	cfg := f.cfg
	cfg.Principals = map[string]string{principalA: "alice", principalB: "alice"}
	if err := f.srv.Reload(context.Background(), cfg); err == nil {
		t.Fatal("reload accepted duplicate names")
	}
	cfg.Principals = map[string]string{}
	if err := f.srv.Reload(context.Background(), cfg); err == nil {
		t.Fatal("reload accepted an empty principal set")
	}
	if got := f.srv.principalMap(); len(got) != 2 {
		t.Fatalf("invalid reload changed principals: %v", got)
	}
}

func TestRemovedPrincipalStopsAuthenticatingButKeepsInbox(t *testing.T) {
	f := newFixture(t)
	ctx := context.Background()
	if err := f.srv.Reload(ctx, withPrincipals(f.cfg, map[string]string{principalA: "alice"})); err != nil {
		t.Fatalf("remove bob: %v", err)
	}
	if _, err := f.srv.authenticate(ctx, "bob"); err == nil {
		t.Fatal("removed principal still authenticates")
	} else {
		requireCode(t, err, "forbidden")
	}
	if rowCount(t, f, principalB) != 1 {
		t.Fatal("removing a principal deleted its inbox")
	}
	res, err := f.client("alice").Who(ctx)
	if err != nil || len(res.Participants) != 1 || res.Participants[0].Name != "alice" {
		t.Fatalf("who after removal: %+v %v", res, err)
	}
	// Re-adding the same principal and name resumes its retained inbox.
	if err = f.srv.Reload(ctx, f.cfg); err != nil {
		t.Fatalf("re-add bob: %v", err)
	}
	if _, err := f.srv.authenticate(ctx, "bob"); err != nil {
		t.Fatalf("re-added principal refused: %v", err)
	}
	if rowCount(t, f, principalB) != 1 {
		t.Fatal("re-adding a principal created a second inbox")
	}
}

func TestReloadRefusesRemovedNameReusedByNewPrincipal(t *testing.T) {
	f := newFixture(t)
	ctx := context.Background()
	f.mu.Lock()
	f.tokens["cairn1.carol"] = principalCarol
	f.mu.Unlock()
	// bob's inbox keeps the name, so a new principal cannot take it.
	err := f.srv.Reload(ctx, withPrincipals(f.cfg, map[string]string{principalA: "alice", principalCarol: "bob"}))
	if err == nil {
		t.Fatal("new principal reused a retained name")
	}
	if got := f.srv.principalMap(); len(got) != 2 || got[principalB] != "bob" {
		t.Fatalf("refused reload changed principals: %v", got)
	}
	if _, err := f.srv.authenticate(ctx, "cairn1.carol"); err == nil {
		t.Fatal("refused principal authenticates")
	}
}

func TestConcurrentAuthenticateDuringReload(t *testing.T) {
	f := newFixture(t)
	ctx := context.Background()
	f.mu.Lock()
	f.tokens["cairn1.carol"] = principalCarol
	f.mu.Unlock()
	base := f.cfg
	withCarol := withPrincipals(base, map[string]string{principalA: "alice", principalB: "bob", principalCarol: "carol"})
	baseOnly := withPrincipals(base, map[string]string{principalA: "alice", principalB: "bob"})

	var wg sync.WaitGroup
	failures := make(chan error, 64)
	for i := 0; i < 4; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			for j := 0; j < 30; j++ {
				if _, err := f.srv.authenticate(ctx, "alice"); err != nil {
					failures <- err
					return
				}
				if _, err := f.srv.status(ctx, principalA); err != nil {
					failures <- err
					return
				}
			}
		}()
	}
	for i := 0; i < 8; i++ {
		cfg := baseOnly
		if i%2 == 0 {
			cfg = withCarol
		}
		if err := f.srv.Reload(ctx, cfg); err != nil {
			t.Fatalf("reload %d: %v", i, err)
		}
		_, err := f.srv.authenticate(ctx, "cairn1.carol")
		if i%2 == 0 && err != nil {
			t.Fatalf("carol refused while configured: %v", err)
		}
		if i%2 == 1 && err == nil {
			t.Fatal("carol admitted after removal")
		}
	}
	wg.Wait()
	close(failures)
	for err := range failures {
		t.Fatalf("authenticate or status failed during reload: %v", err)
	}
}

// omitempty drops an emptied optional field from the new configuration's JSON, so removing a
// setting must be compared too: removing invites_dir would otherwise report a successful reload
// while enrolment stayed enabled, and a later restart would apply it silently.
func TestReloadRefusesRemovingOptionalSettings(t *testing.T) {
	cases := []struct {
		name  string
		key   string
		clear func(*Config)
	}{
		{"invites_dir", "invites_dir", func(c *Config) { c.InvitesDir = "" }},
		{"public_endpoint", "public_endpoint", func(c *Config) { c.PublicEndpoint = "" }},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			invites := t.TempDir()
			f := fixtureBeforeStart(t, func(f *fixture) {
				f.cfg.InvitesDir = invites
				f.cfg.PublicEndpoint = "https://garden.example/mcp"
			})
			cfg := f.cfg
			tc.clear(&cfg)
			err := f.srv.Reload(context.Background(), cfg)
			if err == nil || !strings.Contains(err.Error(), `"`+tc.key+`"`) {
				t.Fatalf("removing %s: %v", tc.key, err)
			}
		})
	}
}
