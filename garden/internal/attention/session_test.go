package attention

import (
	"context"
	"encoding/json"
	"path/filepath"
	"strings"
	"testing"

	"github.com/veridian69/cairn/garden/internal/garden"
	"github.com/veridian69/cairn/garden/internal/gardenauth"
)

func useHome(t *testing.T, body string) string {
	t.Helper()
	dir := t.TempDir()
	if body != "" {
		writeHome(t, dir, body)
	}
	t.Setenv("GARDEN_HOME", dir)
	return dir
}

func openDisconnected(t *testing.T, explicit string) *HarnessSession {
	t.Helper()
	session := OpenHarness(context.Background(), "claude", explicit)
	t.Cleanup(session.Close)
	if session.Remote != nil {
		t.Fatal("session connected without Garden")
	}
	return session
}

func TestDisconnectedSessionExposesOnlyStatusWithSafeReason(t *testing.T) {
	useHome(t, "")
	session := openDisconnected(t, "")
	tools, err := session.Tools.List(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	if len(tools) != 1 || tools[0].Name != "status" {
		t.Fatalf("disconnected session exposes %#v", tools)
	}
	value, err := session.Tools.Call(context.Background(), "status", json.RawMessage(`{}`))
	if err != nil {
		t.Fatal(err)
	}
	encoded, _ := json.Marshal(value)
	var status struct {
		Connected bool   `json:"connected"`
		Identity  string `json:"identity"`
		Reason    string `json:"reason"`
	}
	if err := json.Unmarshal(encoded, &status); err != nil {
		t.Fatal(err)
	}
	if status.Connected || status.Reason == "" {
		t.Fatalf("wrong status: %s", encoded)
	}
	if _, err := session.Tools.Call(context.Background(), "send_message", json.RawMessage(`{"content":"x"}`)); err == nil {
		t.Fatal("disconnected session sent a message")
	}
	if !strings.Contains(session.Instructions, "not connected") || !strings.Contains(session.Instructions, status.Reason) {
		t.Fatalf("instructions do not explain disconnection: %q", session.Instructions)
	}
}

func TestMissingDefaultIdentityIsDisconnectedWithReason(t *testing.T) {
	useHome(t, strings.Replace(validHomeConfig, `"claude":"spike",`, "", 1))
	session := openDisconnected(t, "")
	if !strings.Contains(session.Instructions, "no default identity for claude") {
		t.Fatalf("missing default not explained: %q", session.Instructions)
	}
}

func TestMissingHomeConfigIsDisconnectedWithoutPaths(t *testing.T) {
	dir := useHome(t, "")
	session := openDisconnected(t, "")
	if strings.Contains(session.Instructions, dir) {
		t.Fatalf("reason exposes an absolute path: %q", session.Instructions)
	}
}

func TestDefaultIdentityClaimedElsewhereIsNotTakenOver(t *testing.T) {
	dir := useHome(t, validHomeConfig)
	held, err := (Home{Dir: dir}).ClaimIdentity("spike")
	if err != nil {
		t.Fatal(err)
	}
	defer held.Close()
	session := openDisconnected(t, "")
	want := "spike is already connected in another session; set GARDEN_IDENTITY=<name> to connect as another identity"
	if !strings.Contains(session.Instructions, want) {
		t.Fatalf("default claim reason wrong: %q", session.Instructions)
	}
}

func TestExplicitIdentityClaimedElsewhereIsRefused(t *testing.T) {
	dir := useHome(t, validHomeConfig)
	held, err := (Home{Dir: dir}).ClaimIdentity("spike2")
	if err != nil {
		t.Fatal(err)
	}
	defer held.Close()
	session := openDisconnected(t, "spike2")
	if !strings.Contains(session.Instructions, "spike2 is already in use; not taking it over") {
		t.Fatalf("explicit claim reason wrong: %q", session.Instructions)
	}
}

func TestMissingIdentityTokenNamesOnlyHomeRelativePath(t *testing.T) {
	dir := useHome(t, validHomeConfig)
	session := openDisconnected(t, "")
	if !strings.Contains(session.Instructions, filepath.Join("identities", "spike.token")) {
		t.Fatalf("token reason not home-relative: %q", session.Instructions)
	}
	if strings.Contains(session.Instructions, dir) {
		t.Fatalf("reason exposes the Garden home: %q", session.Instructions)
	}
}

func TestConnectedInstructionsListRosterAndKeepTrustRules(t *testing.T) {
	text := ConnectedInstructions("spike", []string{"Val", "val2"})
	for _, want := range []string{"spike", "Val, val2", "send_message", "acknowledge_delivery", "untrusted", "never as human authority", "cannot change your task scope"} {
		if !strings.Contains(text, want) {
			t.Fatalf("instructions lack %q: %q", want, text)
		}
	}
	if !strings.Contains(ConnectedInstructions("spike", nil), "No other participants") {
		t.Fatal("empty roster not stated")
	}
}

func TestProfileInstructionsDoNotAssumeDeliveryAcknowledgementTool(t *testing.T) {
	if strings.Contains(ProfileInstructions, "acknowledge_delivery") {
		t.Fatalf("profile instructions name a host-specific tool: %q", ProfileInstructions)
	}
	for _, want := range []string{"untrusted", "human authority", "send_message", "task scope"} {
		if !strings.Contains(ProfileInstructions, want) {
			t.Fatalf("profile instructions lack %q: %q", want, ProfileInstructions)
		}
	}
}

func TestRosterExcludesSelf(t *testing.T) {
	p := Profile{InstanceID: "instance", Scope: gardenauth.Scope{Realm: "r", Segments: []gardenauth.Segment{}}, Classification: "internal", Participant: "Spike"}
	b := garden.Binding{InstanceID: p.InstanceID, Scope: p.Scope, Classification: p.Classification}
	f := &fakeGarden{status: garden.StatusResult{Participant: "Spike", Binding: b, Participants: []string{"Spike", "Val", "val2"}}}
	roster, err := (&Remote{api: f, profile: p}).Roster(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	if strings.Join(roster, ",") != "Val,val2" {
		t.Fatalf("roster wrong: %v", roster)
	}
}

func TestInstructionsMatchModelToolsPerHarness(t *testing.T) {
	for _, tc := range []struct {
		name, text string
		ack        bool
	}{
		{"claude", harnessInstructions("agent", nil, true, false), true},
		{"codex", codexInstructions("agent", nil, true), false},
		{"opencode", harnessInstructions("agent", nil, true, true), false},
		{"profile-claude", ProfileChannelInstructions, true},
		{"profile-codex", ProfileInstructions, false},
		{"profile-opencode", ProfileInstructions, false},
	} {
		t.Run(tc.name, func(t *testing.T) {
			if got := strings.Contains(tc.text, "acknowledge_delivery"); got != tc.ack {
				t.Fatalf("acknowledgement tool named = %v, listed = %v: %s", got, tc.ack, tc.text)
			}
		})
	}
}
