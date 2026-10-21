package attention

import (
	"bufio"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"strings"
	"testing"
	"time"

	"github.com/google/uuid"
	"github.com/veridian69/cairn/garden/internal/garden"
	"github.com/veridian69/cairn/garden/internal/gardenauth"
)

// harnessRemote is a Remote shaped the way OpenHarness leaves a Claude session, around the fake gateway.
// The named tools are the ones the fake gateway advertises.
func harnessRemote(t *testing.T, supported ...string) (*fakeGarden, *Remote) {
	t.Helper()
	p := Profile{InstanceID: "instance", Scope: gardenauth.Scope{Realm: "r", Segments: []gardenauth.Segment{}}, Classification: "internal", Participant: "Spike"}
	f := &fakeGarden{status: garden.StatusResult{Binding: garden.Binding{InstanceID: "instance", Scope: p.Scope, Classification: "internal"}, Participant: "Spike", Participants: []string{"Spike", "Val"}}, supported: map[string]bool{}}
	for _, tool := range supported {
		f.supported[tool] = true
	}
	r := &Remote{api: f, profile: p, harness: true, resolve: true, consumer: uuid.NewString(), probeID: uuid.NewString(), nudge: make(chan struct{}, 1), held: true}
	return f, r
}

func heartbeatsSnapshot(f *fakeGarden) []garden.HeartbeatArgs {
	f.mu.Lock()
	defer f.mu.Unlock()
	return append([]garden.HeartbeatArgs(nil), f.heartbeats...)
}

func TestGatedSessionDoesNotPollUntilProbeIsAcknowledged(t *testing.T) {
	f, r := harnessRemote(t, "heartbeat", "who")
	ctx, cancel := context.WithTimeout(context.Background(), 200*time.Millisecond)
	defer cancel()
	if pending, err := r.Poll(ctx, r.consumer); pending != nil || err != nil {
		t.Fatalf("gated poll = %+v, %v; want nothing", pending, err)
	}
	if f.polls != 0 {
		t.Fatalf("gated session polled the gateway %d times before deliverability", f.polls)
	}
	r.markDeliverable()
	if _, err := r.Poll(context.Background(), r.consumer); err != nil {
		t.Fatal(err)
	}
	if f.polls != 1 {
		t.Fatalf("deliverable session polls = %d, want 1", f.polls)
	}
}

func TestLegacyGatewayPollsDirectlyAndSaysSo(t *testing.T) {
	f, r := harnessRemote(t)
	r.legacy = true
	if _, err := r.Poll(context.Background(), r.consumer); err != nil {
		t.Fatal(err)
	}
	if f.polls != 1 {
		t.Fatalf("legacy poll count = %d, want a direct poll", f.polls)
	}
	if err := r.StartDelivery(context.Background(), NewStdio(io.Discard, fakeTools{}, true, "")); err != nil {
		t.Fatal(err)
	}
	if len(heartbeatsSnapshot(f)) != 0 {
		t.Fatal("legacy session sent heartbeats")
	}
	state, note := r.deliveryStatus()
	if state != "direct" || !strings.Contains(note, "predates heartbeat") {
		t.Fatalf("legacy status = %q, %q", state, note)
	}
}

func TestWhoIsListedOnlyWhenTheGatewayAdvertisesIt(t *testing.T) {
	for _, supported := range [][]string{nil, {"heartbeat", "who"}} {
		_, r := harnessRemote(t, supported...)
		tools, err := r.List(context.Background())
		if err != nil {
			t.Fatal(err)
		}
		listed := false
		for _, tool := range tools {
			listed = listed || tool.Name == "who"
		}
		if want := len(supported) > 0; listed != want {
			t.Fatalf("gateway tools %v: who listed = %v, want %v", supported, listed, want)
		}
	}
}

func TestUnconfirmedProbeAsksForChannelRelaunch(t *testing.T) {
	_, r := harnessRemote(t, "heartbeat")
	r.probeAt = time.Now().Add(-time.Minute)
	if state, _ := r.deliveryStatus(); state != "awaiting_confirmation" {
		t.Fatalf("state before two minutes = %q", state)
	}
	r.probeAt = time.Now().Add(-3 * time.Minute)
	state, note := r.deliveryStatus()
	if state != "unconfirmed" || !strings.Contains(note, "--dangerously-load-development-channels") || !strings.Contains(note, "plugin:garden@garden-local") || !strings.Contains(note, "plugin:garden@inline") {
		t.Fatalf("unconfirmed status = %q, %q", state, note)
	}
	r.markDeliverable()
	if state, _ = r.deliveryStatus(); state != "deliverable" {
		t.Fatalf("state after acknowledgement = %q", state)
	}
}

func TestProbeAcknowledgementOpensDeliveryAndIsNotForwarded(t *testing.T) {
	f, r := harnessRemote(t, "heartbeat", "who")
	input, inWriter := io.Pipe()
	output, outWriter := io.Pipe()
	defer input.Close()
	defer inWriter.Close()
	defer output.Close()
	defer outWriter.Close()
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	server := NewStdio(outWriter, r, true, "")
	finished := make(chan error, 1)
	go func() {
		finished <- server.Run(ctx, input, func(ctx context.Context) error {
			if err := r.StartDelivery(ctx, server); err != nil {
				return err
			}
			<-ctx.Done()
			return nil
		})
	}()
	decoder := json.NewDecoder(bufio.NewReader(output))
	encoder := json.NewEncoder(inWriter)
	_ = encoder.Encode(map[string]any{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": map[string]string{"protocolVersion": "2025-11-25"}})
	var frame map[string]any
	if err := decoder.Decode(&frame); err != nil {
		t.Fatal(err)
	}
	_ = encoder.Encode(map[string]any{"jsonrpc": "2.0", "method": "notifications/initialized"})
	if err := decoder.Decode(&frame); err != nil {
		t.Fatal(err)
	}
	meta := frame["params"].(map[string]any)["meta"].(map[string]any)
	if frame["method"] != "notifications/claude/channel" || meta["message_id"] != r.probeID {
		t.Fatalf("probe frame = %+v, want the channel probe with the remote's probe ID", frame)
	}
	if _, ok := meta["source"]; ok {
		t.Fatalf("probe meta = %+v, want no source key; Claude sets its own", meta)
	}
	shortCtx, cancelShort := context.WithTimeout(context.Background(), 200*time.Millisecond)
	defer cancelShort()
	if pending, err := r.Poll(shortCtx, r.consumer); pending != nil || err != nil || f.polls != 0 {
		t.Fatalf("poll before acknowledgement = %+v, %v, polls %d", pending, err, f.polls)
	}
	_ = encoder.Encode(map[string]any{"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": map[string]any{"name": "acknowledge_delivery", "arguments": map[string]string{"message_id": r.probeID}}})
	var reply struct {
		Result struct {
			StructuredContent map[string]bool `json:"structuredContent"`
		} `json:"result"`
	}
	if err := decoder.Decode(&reply); err != nil {
		t.Fatal(err)
	}
	if !reply.Result.StructuredContent["acknowledged"] {
		t.Fatalf("probe acknowledgement = %+v, want acknowledged", reply)
	}
	if f.acks != 0 {
		t.Fatal("probe acknowledgement reached the gateway acknowledge operation")
	}
	deadline := time.Now().Add(3 * time.Second)
	for {
		hb := heartbeatsSnapshot(f)
		if len(hb) > 0 && hb[len(hb)-1].Deliverable {
			break
		}
		if time.Now().After(deadline) {
			t.Fatalf("no deliverable heartbeat after probe acknowledgement: %+v", hb)
		}
		time.Sleep(10 * time.Millisecond)
	}
	if state, _ := r.deliveryStatus(); state != "deliverable" {
		t.Fatalf("state after probe = %q", state)
	}
	cancel()
	_ = input.Close()
	<-finished
}

func TestWhoIsHarnessOnlyAndInstructionsShowPresence(t *testing.T) {
	f, r := harnessRemote(t, "heartbeat", "who")
	f.who = garden.WhoResult{Participants: []garden.Presence{{Name: "Spike"}, {Name: "Val", Online: true, Deliverable: true}, {Name: "zen"}}}
	labels, err := r.rosterLabels(context.Background())
	if err != nil || strings.Join(labels, ", ") != "Val (online), zen (offline)" {
		t.Fatalf("roster labels = %v, %v", labels, err)
	}
	tools, _ := r.List(context.Background())
	if !hasTool(tools, "who") {
		t.Fatal("harness session does not list who")
	}
	if _, err := r.Call(context.Background(), "who", json.RawMessage(`{}`)); err != nil {
		t.Fatal(err)
	}

	profileTools, _ := (&Remote{api: f, profile: r.profile}).List(context.Background())
	if hasTool(profileTools, "who") {
		t.Fatal("--profile connection lists who")
	}
	profile := &Remote{api: f, profile: r.profile}
	if _, err := profile.Call(context.Background(), "who", json.RawMessage(`{}`)); err == nil {
		t.Fatal("--profile connection called who")
	}
	out, err := profile.Call(context.Background(), "send_message", json.RawMessage(`{"content":"hi","recipients":["Val"]}`))
	if err != nil {
		t.Fatal(err)
	}
	if _, ok := out.(garden.Message); !ok {
		t.Fatalf("--profile send result is %T, want the unchanged garden.Message", out)
	}
	harnessOut, err := r.Call(context.Background(), "send_message", json.RawMessage(`{"content":"hi","recipients":["Val"]}`))
	if err != nil {
		t.Fatal(err)
	}
	if _, ok := harnessOut.(garden.SendResult); !ok {
		t.Fatalf("harness send result is %T, want garden.SendResult", harnessOut)
	}
}

func hasTool(tools []Tool, name string) bool {
	for _, tool := range tools {
		if tool.Name == name {
			return true
		}
	}
	return false
}

// refusedLease is a second consumer on the same identity, sharing one gateway with the first.
func refusedLease(t *testing.T, shared *fakeGarden, owner string) (*Remote, error) {
	t.Helper()
	r := &Remote{api: shared, profile: Profile{Participant: "Spike"}, harness: true, consumer: owner, nudge: make(chan struct{}, 1)}
	return r, r.takeLease(context.Background())
}

func TestSecondConsumerOnSameIdentityGetsStatusOnlySession(t *testing.T) {
	f, first := harnessRemote(t, "heartbeat", "who")
	if err := first.takeLease(context.Background()); err != nil {
		t.Fatal(err)
	}
	second, err := refusedLease(t, f, uuid.NewString())
	if !errors.Is(err, ErrBusy) {
		t.Fatalf("second consumer lease = %v, want ErrBusy", err)
	}
	if second.isHeld() {
		t.Fatal("refused consumer reports holding the lease")
	}
	// OpenHarness turns that refusal into a status-only session that names the fix.
	session := &HarnessSession{}
	session.disconnect("Spike", "Spike is already in use by another session or machine; set GARDEN_IDENTITY=<name> to connect as another identity")
	tools, _ := session.Tools.List(context.Background())
	if len(tools) != 1 || tools[0].Name != "status" {
		t.Fatalf("refused session exposes %+v, want status only", tools)
	}
	out, err := session.Tools.Call(context.Background(), "status", json.RawMessage(`{}`))
	if err != nil || !strings.Contains(fmt.Sprint(out), "GARDEN_IDENTITY") || !strings.Contains(fmt.Sprint(out), "in use") {
		t.Fatalf("refused status = %+v, %v", out, err)
	}
	if f.polls != 0 {
		t.Fatalf("refused session polled %d times", f.polls)
	}
}

func TestLeaseLossStopsPollingAndRecoversWhenFree(t *testing.T) {
	f, r := harnessRemote(t, "heartbeat")
	r.heartbeat = 5 * time.Millisecond
	if err := r.takeLease(context.Background()); err != nil {
		t.Fatal(err)
	}
	r.markDeliverable()
	// Another session takes the lease; the gateway refuses our next poll.
	f.mu.Lock()
	f.holder = uuid.NewString()
	f.pollErr = &garden.Error{Code: "inbox_busy", Message: "another adapter owns this inbox lease"}
	f.mu.Unlock()
	if _, err := r.Poll(context.Background(), r.consumer); !errors.Is(err, ErrBusy) {
		t.Fatalf("poll after takeover = %v, want ErrBusy", err)
	}
	if state, _ := r.deliveryStatus(); state != "lease_lost" {
		t.Fatalf("state after takeover = %q, want lease_lost", state)
	}
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	go r.keepLease(ctx)
	waitFor(t, func() bool { return !r.isHeld() }, "heartbeat did not report the lost lease")
	f.mu.Lock()
	f.pollErr = nil
	polls := f.polls
	f.mu.Unlock()
	shortCtx, cancelShort := context.WithTimeout(context.Background(), 100*time.Millisecond)
	defer cancelShort()
	if pending, err := r.Poll(shortCtx, r.consumer); pending != nil || err != nil {
		t.Fatalf("poll while lease lost = %+v, %v", pending, err)
	}
	f.mu.Lock()
	gatewayPolls := f.polls
	f.holder = ""
	f.mu.Unlock()
	if gatewayPolls != polls {
		t.Fatal("session polled the gateway while the lease was lost")
	}
	waitFor(t, r.isHeld, "lease was not regained once free")
	if state, _ := r.deliveryStatus(); state != "deliverable" {
		t.Fatalf("state after regaining lease = %q", state)
	}
}

func waitFor(t *testing.T, cond func() bool, message string) {
	t.Helper()
	deadline := time.Now().Add(3 * time.Second)
	for !cond() {
		if time.Now().After(deadline) {
			t.Fatal(message)
		}
		time.Sleep(5 * time.Millisecond)
	}
}

func TestLostLeaseAfterDeliverabilityWaitsInsteadOfSpinning(t *testing.T) {
	f, r := harnessRemote(t, "heartbeat", "who")
	r.markDeliverable()
	r.setHeld(false)
	ctx, cancel := context.WithTimeout(context.Background(), 300*time.Millisecond)
	defer cancel()
	start := time.Now()
	if pending, err := r.Poll(ctx, r.consumer); pending != nil || err != nil {
		t.Fatalf("lease-lost poll = %+v, %v; want nothing", pending, err)
	}
	if elapsed := time.Since(start); elapsed < 250*time.Millisecond {
		t.Fatalf("lease-lost poll returned after %v; want it to wait, not spin", elapsed)
	}
	if f.polls != 0 {
		t.Fatalf("lease-lost session polled the gateway %d times", f.polls)
	}
}

func TestDeliveryFailureWaitsInsteadOfSpinningAndSessionReportWakesPoll(t *testing.T) {
	f, r := harnessRemote(t, "heartbeat", "who")
	r.markDeliverable()
	r.DeliveryFailed("session busy")
	ctx, cancel := context.WithTimeout(context.Background(), 300*time.Millisecond)
	defer cancel()
	start := time.Now()
	if pending, err := r.Poll(ctx, r.consumer); pending != nil || err != nil {
		t.Fatalf("failed-delivery poll = %+v, %v; want nothing", pending, err)
	}
	if elapsed := time.Since(start); elapsed < 250*time.Millisecond {
		t.Fatalf("failed-delivery poll returned after %v; want it to wait, not spin", elapsed)
	}
	if f.polls != 0 {
		t.Fatalf("session without deliverability polled the gateway %d times", f.polls)
	}

	// A waiting poll must wake as soon as the plugin reports a session again, not after deliveryWait.
	woke := make(chan time.Time, 1)
	go func() {
		_, _ = r.Poll(context.Background(), r.consumer)
		woke <- time.Now()
	}()
	time.Sleep(100 * time.Millisecond)
	reported := time.Now()
	r.SessionReported("ses_1")
	select {
	case at := <-woke:
		if delay := at.Sub(reported); delay > 2*time.Second {
			t.Fatalf("poll woke %v after the session report; want a prompt wake-up", delay)
		}
	case <-time.After(5 * time.Second):
		t.Fatal("poll did not wake after the session report")
	}
	if f.polls != 1 {
		t.Fatalf("gateway polls after the session report = %d; want 1", f.polls)
	}
}

func TestUnacknowledgedProbeIsRepeatedUntilAcknowledged(t *testing.T) {
	_, r := harnessRemote(t, "heartbeat", "who")
	output, outWriter := io.Pipe()
	defer output.Close()
	defer outWriter.Close()
	frames := make(chan map[string]any, 16)
	go func() {
		decoder := json.NewDecoder(output)
		for {
			var frame map[string]any
			if decoder.Decode(&frame) != nil {
				return
			}
			frames <- frame
		}
	}()
	server := NewStdio(outWriter, r, true, "")
	r.probeAt = time.Now()
	go func() { _ = server.Probe(r.probeID, "spike", func() {}) }()
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	done := make(chan struct{})
	go func() { r.repeatProbe(ctx, server, time.Millisecond); close(done) }()
	for i := 0; i < 3; i++ {
		select {
		case frame := <-frames:
			if frame["params"].(map[string]any)["meta"].(map[string]any)["message_id"] != r.probeID {
				t.Fatalf("frame %d = %+v, want the same probe ID each time", i, frame)
			}
		case <-time.After(3 * time.Second):
			t.Fatalf("only %d probe frames before the deadline, want repeats", i)
		}
	}
	if !server.acknowledgeProbe(r.probeID) {
		t.Fatal("probe acknowledgement refused")
	}
	// Drain any frame already in flight so the loop can observe the acknowledgement and stop.
	go func() {
		for range frames {
		}
	}()
	select {
	case <-done:
	case <-time.After(3 * time.Second):
		t.Fatal("probe kept repeating after acknowledgement")
	}
	if !server.acknowledgeProbe(r.probeID) {
		t.Fatal("acknowledging a re-emitted copy of a confirmed probe failed")
	}
	if server.acknowledgeProbe("another-id") {
		t.Fatal("an unrelated ID was accepted as the probe")
	}
}
