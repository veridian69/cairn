package attention

import (
	"context"
	"encoding/json"
	"io"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"strings"
	"testing"
	"time"

	"github.com/google/uuid"
)

// savedCodexThread makes CODEX_HOME a temporary home in which thread has a saved rollout.
func savedCodexThread(t *testing.T, thread string) {
	t.Helper()
	home := t.TempDir()
	t.Setenv("CODEX_HOME", home)
	dir := filepath.Join(home, "sessions", "2026", "10", "19")
	if err := os.MkdirAll(dir, 0700); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(dir, "rollout-2026-10-19T16-31-29-"+thread+".jsonl"), []byte("{}\n"), 0600); err != nil {
		t.Fatal(err)
	}
}

func TestCodexThreadSavedNeedsRolloutForThatThread(t *testing.T) {
	thread := uuid.NewString()
	savedCodexThread(t, thread)
	ctx := context.Background()
	if got := codexThreadSaved(ctx, thread); got != codexSaved {
		t.Fatalf("a thread with a rollout = %v, want saved", got)
	}
	if got := codexThreadSaved(ctx, uuid.NewString()); got != codexUnsaved {
		t.Fatalf("a thread without a rollout (a /side chat) = %v, want unsaved", got)
	}
	if codexThreadSaved(ctx, "*") != codexUnsaved || codexThreadSaved(ctx, "") != codexUnsaved {
		t.Fatal("a non-UUID thread was matched as a pattern")
	}
}

func TestCodexWithoutAnySavedSessionReportsHandoffOff(t *testing.T) {
	t.Setenv("CODEX_HOME", t.TempDir())
	home := Home{Dir: t.TempDir()}
	session := &HarnessSession{Tools: Disconnected{Identity: "spike", Reason: codexMovedReason}, home: home, harness: "codex", name: "spike", blocked: true}
	core := &CodexCore{session: session, standby: true, connected: make(chan struct{}, 1), yield: make(chan struct{}, 1)}
	core.Learn(uuid.NewString())
	value, err := core.Call(context.Background(), "status", nil)
	if err != nil {
		t.Fatal(err)
	}
	if reason := value.(map[string]any)["reason"].(string); reason != codexNoSessionsReason || !strings.Contains(reason, "thread handoff is off") {
		t.Fatalf("status = %+v, want the handoff-off reason when Codex saved no session", value)
	}
	if _, ok := home.ClaimHolder("spike"); ok {
		t.Fatal("the identity was claimed without a saved session")
	}
}

func TestClaimRecordsHolderPID(t *testing.T) {
	home := Home{Dir: t.TempDir()}
	claim, err := home.ClaimIdentity("spike")
	if err != nil {
		t.Fatal(err)
	}
	defer claim.Close()
	if pid, ok := home.ClaimHolder("spike"); !ok || pid != os.Getpid() {
		t.Fatalf("holder = %d, %v; want this process %d", pid, ok, os.Getpid())
	}
	if _, ok := home.ClaimHolder("val"); ok {
		t.Fatal("an identity never claimed reported a holder")
	}
}

func TestRunnerStopFinishesDeliveryInFlight(t *testing.T) {
	stop := make(chan struct{})
	inbox := &testInbox{pending: &Pending{Event: Event{ID: "m", Payload: json.RawMessage(`{}`)}, Receipt: "receipt-one", Generation: "g"}, cancel: func() {}}
	runner := Runner{Inbox: inbox, ConsumerID: "consumer", Stop: stop, Host: deliveryFunc(func(ctx context.Context, _ Event) error {
		close(stop)
		select {
		case <-ctx.Done():
			return ErrUncertain
		case <-time.After(50 * time.Millisecond):
			return nil
		}
	})}
	if err := runner.Run(context.Background()); err != nil {
		t.Fatalf("Run = %v, want nil after the stop", err)
	}
	if inbox.acks != 1 {
		t.Fatalf("acks = %d, want the in-flight delivery finished and acknowledged", inbox.acks)
	}
}

func TestRunnerStopAbandonsIdleWait(t *testing.T) {
	stop := make(chan struct{})
	inbox := &testInbox{cancel: func() {}}
	runner := Runner{Inbox: inbox, ConsumerID: "consumer", Stop: stop, retryAfter: time.Hour, Host: deliveryFunc(func(context.Context, Event) error { return nil })}
	time.AfterFunc(50*time.Millisecond, func() { close(stop) })
	done := make(chan error, 1)
	go func() { done <- runner.Run(context.Background()) }()
	select {
	case err := <-done:
		if err != nil {
			t.Fatalf("Run = %v, want nil", err)
		}
	case <-time.After(3 * time.Second):
		t.Fatal("Run kept waiting after the stop")
	}
}

// TestCodexSiblingHelper is a child process for TestCodexSiblingNeedsSameParentAndExecutable.
func TestCodexSiblingHelper(t *testing.T) {
	mode := os.Getenv("GARDEN_SIBLING_HELPER")
	switch {
	case mode == "sleep":
		time.Sleep(10 * time.Second)
		os.Exit(0)
	case strings.HasPrefix(mode, "check:"):
		pid, _ := strconv.Atoi(strings.TrimPrefix(mode, "check:"))
		if codexSibling(pid) {
			os.Exit(0)
		}
		os.Exit(3)
	}
}

func TestCodexSiblingNeedsSameParentAndExecutable(t *testing.T) {
	if codexSibling(os.Getpid()) || codexSibling(os.Getppid()) || codexSibling(1) || codexSibling(0) {
		t.Fatal("self, parent, init or PID 0 counted as a sibling")
	}
	helper := func(mode string) *exec.Cmd {
		cmd := exec.Command(os.Args[0], "-test.run=^TestCodexSiblingHelper$")
		cmd.Env = append(os.Environ(), "GARDEN_SIBLING_HELPER="+mode)
		return cmd
	}
	sleeper := helper("sleep")
	if err := sleeper.Start(); err != nil {
		t.Fatal(err)
	}
	defer func() { _ = sleeper.Process.Kill(); _ = sleeper.Wait() }()
	if err := helper("check:" + strconv.Itoa(sleeper.Process.Pid)).Run(); err != nil {
		t.Fatalf("a child of the same parent and executable was not a sibling: %v", err)
	}
	if err := helper("check:" + strconv.Itoa(os.Getpid())).Run(); err == nil {
		t.Fatal("the checker's own parent counted as a sibling")
	}
}

func TestCodexCoreYieldReleasesIdentityAndRetakesOnCall(t *testing.T) {
	_, r := harnessRemote(t, "heartbeat", "who")
	r.awaitSession, r.codex = true, true
	home := Home{Dir: t.TempDir()}
	claim, err := home.ClaimIdentity("spike")
	if err != nil {
		t.Fatal(err)
	}
	session := &HarnessSession{Remote: r, Tools: r, ConsumerID: r.consumer, Host: deliveryFunc(func(context.Context, Event) error { return nil }), claim: claim, home: home, harness: "codex", name: "spike"}
	defer session.Close()
	core := NewCodexCore(session)
	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan error, 1)
	go func() { done <- core.Run(ctx, NewStdio(io.Discard, core, false, "")) }()
	time.Sleep(50 * time.Millisecond)
	core.Yield()
	deadline := time.Now().Add(3 * time.Second)
	for {
		core.mu.Lock()
		released, standby := session.Remote == nil, core.standby
		core.mu.Unlock()
		if released && standby {
			break
		}
		if time.Now().After(deadline) {
			t.Fatal("core did not release the identity after a yield")
		}
		time.Sleep(10 * time.Millisecond)
	}
	again, err := home.ClaimIdentity("spike")
	if err != nil {
		t.Fatalf("claim still held after the yield: %v", err)
	}
	_ = again.Close()
	thread := uuid.NewString()
	savedCodexThread(t, thread)
	core.Learn(thread)
	value, err := core.Call(ctx, "status", nil)
	if err != nil {
		t.Fatal(err)
	}
	// The home has no token, so the takeover claims, then fails to connect and says why.
	if reason := value.(map[string]any)["reason"].(string); !strings.Contains(reason, "token") {
		t.Fatalf("status after the yield = %+v, want a reconnect attempt that reports the missing token", value)
	}
	cancel()
	select {
	case err := <-done:
		if err != nil {
			t.Fatalf("Run = %v, want nil on cancellation", err)
		}
	case <-time.After(3 * time.Second):
		t.Fatal("Run did not end on cancellation")
	}
}

func TestCodexCoreInUnsavedThreadDoesNotTakeOver(t *testing.T) {
	savedCodexThread(t, uuid.NewString())
	home := Home{Dir: t.TempDir()}
	session := &HarnessSession{Tools: Disconnected{Identity: "spike", Reason: codexMovedReason}, home: home, harness: "codex", name: "spike", blocked: true}
	core := &CodexCore{session: session, standby: true, connected: make(chan struct{}, 1), yield: make(chan struct{}, 1)}
	core.Learn(uuid.NewString())
	value, err := core.Call(context.Background(), "status", nil)
	if err != nil {
		t.Fatal(err)
	}
	if reason := value.(map[string]any)["reason"].(string); reason != codexUnsavedReason {
		t.Fatalf("status = %+v, want the unsaved-thread reason", value)
	}
	if pid, ok := home.ClaimHolder("spike"); ok {
		t.Fatalf("an unsaved thread claimed the identity (holder %d)", pid)
	}
}
