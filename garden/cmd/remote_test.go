package cmd

import (
	"context"
	"errors"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/spf13/cobra"
	"github.com/veridian69/cairn/garden/internal/garden"
	"github.com/veridian69/cairn/garden/internal/gardenauth"
	"github.com/veridian69/cairn/garden/internal/remotecli"
)

// cmdGateway is a scripted gateway for the remote commands.
type cmdGateway struct {
	messages []garden.Message
	sent     []garden.SendArgs
	readErr  error
	closed   int
}

func (g *cmdGateway) Status(context.Context) (garden.StatusResult, error) {
	return garden.StatusResult{Participant: "operator", Binding: testRemoteBinding()}, nil
}

func (g *cmdGateway) Read(_ context.Context, a garden.ReadArgs) (garden.ReadResult, error) {
	if g.readErr != nil {
		return garden.ReadResult{}, g.readErr
	}
	messages := g.messages
	if a.Tail > 0 && len(messages) > a.Tail {
		messages = messages[len(messages)-a.Tail:]
	}
	next := uint64(0)
	if len(g.messages) > 0 {
		next = g.messages[len(g.messages)-1].Sequence
	}
	return garden.ReadResult{Messages: messages, Generation: "gen-1", NextSeq: next}, nil
}

func (g *cmdGateway) Send(_ context.Context, a garden.SendArgs) (garden.SendResult, error) {
	g.sent = append(g.sent, a)
	m := garden.Message{ID: "00000000-0000-4000-8000-000000000099", Sequence: 9, AuthorName: "operator", Content: a.Content, CreatedAt: "2026-11-08T09:00:00Z"}
	return garden.SendResult{Message: m, Deliveries: []garden.Delivery{{Recipient: "claude", Delivery: "queued"}}}, nil
}

func (g *cmdGateway) Who(context.Context) (garden.WhoResult, error) {
	return garden.WhoResult{}, nil
}

func (g *cmdGateway) Supports(string) bool { return true }

func (g *cmdGateway) Close() error {
	g.closed++
	return nil
}

func testRemoteBinding() garden.Binding {
	return garden.Binding{
		InstanceID:     "6f1f3d2e-8a4b-4c5d-9e6f-7a8b9c0d1e2f",
		Scope:          gardenauth.Scope{Realm: "cairn", Segments: []gardenauth.Segment{{Kind: "project", Identifier: "garden"}}},
		Classification: "internal",
	}
}

// useRemoteGateway replaces openRemote with a session over gw and counts how often it is opened.
func useRemoteGateway(t *testing.T, gw *cmdGateway) *int {
	t.Helper()
	opened := 0
	old := openRemote
	openRemote = func(context.Context) (*remotecli.Session, error) {
		opened++
		return remotecli.NewSession("operator", testRemoteBinding(), gw), nil
	}
	t.Cleanup(func() { openRemote = old })
	return &opened
}

// failRemote replaces openRemote with one that reports a missing home.
func failRemote(t *testing.T) *int {
	t.Helper()
	opened := 0
	old := openRemote
	openRemote = func(context.Context) (*remotecli.Session, error) {
		opened++
		return nil, errors.New("no Garden home at test; run `garden join` with an invite first")
	}
	t.Cleanup(func() { openRemote = old })
	return &opened
}

// remoteHome points HOME at a fresh directory and returns the paths the local daemon would use.
func remoteHome(t *testing.T) (daemonURL, stateDB, runtimeLock string) {
	t.Helper()
	t.Setenv("HOME", t.TempDir())
	t.Setenv("GARDEN_REMOTE", "")
	t.Setenv("GARDEN_HOME", "")
	return DaemonURLPath(), filepath.Join(DefaultDataDir(), "state.db"), filepath.Join(DefaultConfigDir(), "runtime.lock")
}

// assertNoLocalRuntime checks that none of the local daemon's files were read or created.
func assertNoLocalRuntime(t *testing.T, daemonURL, stateDB, runtimeLock string) {
	t.Helper()
	for _, path := range []string{daemonURL, stateDB, runtimeLock} {
		if _, err := os.Stat(path); !errors.Is(err, os.ErrNotExist) {
			t.Fatalf("remote mode touched %s (stat error %v)", path, err)
		}
	}
}

// setFlag sets a command's flag for one test and restores its default afterwards.
func setFlag(t *testing.T, command *cobra.Command, name, value string) {
	t.Helper()
	flag := command.Flags().Lookup(name)
	if flag == nil {
		t.Fatalf("no flag %s on %s", name, command.Name())
	}
	if err := flag.Value.Set(value); err != nil {
		t.Fatal(err)
	}
	flag.Changed = true
	t.Cleanup(func() {
		_ = flag.Value.Set(flag.DefValue)
		flag.Changed = false
	})
}

func TestRemoteRequestedPrecedence(t *testing.T) {
	cases := []struct {
		name string
		env  string
		args []string
		want bool
		err  bool
	}{
		{name: "neither", want: false},
		{name: "env on", env: "1", want: true},
		{name: "env off", env: "0", want: false},
		{name: "flag on", args: []string{"--remote"}, want: true},
		{name: "flag false overrides env on", env: "1", args: []string{"--remote=false"}, want: false},
		{name: "flag true overrides env off", env: "0", args: []string{"--remote"}, want: true},
		{name: "invalid env", env: "sometimes", err: true},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			t.Setenv("GARDEN_REMOTE", tc.env)
			command := &cobra.Command{Use: "probe"}
			addRemoteFlag(command)
			if err := command.Flags().Parse(tc.args); err != nil {
				t.Fatal(err)
			}
			got, err := remoteRequested(command)
			if tc.err {
				if err == nil {
					t.Fatal("remoteRequested accepted an invalid GARDEN_REMOTE")
				}
				return
			}
			if err != nil || got != tc.want {
				t.Fatalf("remoteRequested = (%v, %v), want %v", got, err, tc.want)
			}
		})
	}
}

func TestRemoteSayNeverTouchesDaemonFiles(t *testing.T) {
	daemonURL, stateDB, runtimeLock := remoteHome(t)
	gw := &cmdGateway{}
	opened := useRemoteGateway(t, gw)
	setFlag(t, sayCmd, "remote", "true")

	if err := sayCmd.PersistentPreRunE(sayCmd, nil); err != nil {
		t.Fatalf("pre-run in remote mode: %v", err)
	}
	output := captureStdout(t, func() {
		if err := sayCmd.RunE(sayCmd, []string{"hello", "gateway"}); err != nil {
			t.Fatalf("say --remote: %v", err)
		}
	})

	if *opened != 1 || len(gw.sent) != 1 || gw.sent[0].Content != "hello gateway" {
		t.Fatalf("opened %d, sent %+v", *opened, gw.sent)
	}
	if !strings.Contains(output, "[operator] hello gateway") || !strings.Contains(output, "stored as 00000000, sequence 9") {
		t.Fatalf("output = %q", output)
	}
	if !strings.Contains(output, "claude: queued") {
		t.Fatalf("delivery outcome missing from %q", output)
	}
	if gw.closed != 1 {
		t.Fatalf("session closed %d times, want 1", gw.closed)
	}
	assertNoLocalRuntime(t, daemonURL, stateDB, runtimeLock)
}

func TestRemoteSayPassesRecipients(t *testing.T) {
	remoteHome(t)
	gw := &cmdGateway{}
	useRemoteGateway(t, gw)
	setFlag(t, sayCmd, "remote", "true")
	old := sayTo
	sayTo = []string{"claude", "codex"}
	defer func() { sayTo = old }()

	captureStdout(t, func() {
		if err := sayCmd.RunE(sayCmd, []string{"hi"}); err != nil {
			t.Fatalf("say --remote --to: %v", err)
		}
	})
	if len(gw.sent) != 1 || strings.Join(gw.sent[0].Recipients, ",") != "claude,codex" {
		t.Fatalf("sent = %+v, want recipients claude and codex", gw.sent)
	}
}

func TestRemoteSayRefusesAsBeforeOpening(t *testing.T) {
	remoteHome(t)
	opened := failRemote(t)
	setFlag(t, sayCmd, "remote", "true")
	old := sayAs
	sayAs = "claude"
	defer func() { sayAs = old }()

	err := sayCmd.RunE(sayCmd, []string{"hi"})
	if err == nil || !strings.Contains(err.Error(), "--as is not available in remote mode") {
		t.Fatalf("say --remote --as error = %v", err)
	}
	if *opened != 0 {
		t.Fatal("--as must be refused before a session is opened")
	}
}

func TestLocalSayRefusesToWithoutRemote(t *testing.T) {
	remoteHome(t)
	old := sayTo
	sayTo = []string{"claude"}
	defer func() { sayTo = old }()

	err := sayCmd.RunE(sayCmd, []string{"hi"})
	if err == nil || !strings.Contains(err.Error(), "--to applies only with --remote") {
		t.Fatalf("local say --to error = %v", err)
	}
}

func TestLocalSayStillUsesTheDaemon(t *testing.T) {
	remoteHome(t)
	err := sayCmd.RunE(sayCmd, []string{"hi"})
	if err == nil || !strings.Contains(err.Error(), "daemon not running") {
		t.Fatalf("local say without a daemon error = %v", err)
	}
}

func TestRemoteCommandsReportMissingHomeWithoutLocalFiles(t *testing.T) {
	daemonURL, stateDB, runtimeLock := remoteHome(t)
	setFlag(t, sayCmd, "remote", "true")
	setFlag(t, historyCmd, "remote", "true")
	setFlag(t, watchCmd, "remote", "true")
	setFlag(t, chatCmd, "remote", "true")
	opened := failRemote(t)

	runs := map[string]func() error{
		"say":     func() error { return sayCmd.RunE(sayCmd, []string{"hi"}) },
		"history": func() error { return historyCmd.RunE(historyCmd, nil) },
		"watch":   func() error { return watchCmd.RunE(watchCmd, nil) },
		"chat":    func() error { return chatCmd.RunE(chatCmd, nil) },
	}
	for name, run := range runs {
		err := run()
		if err == nil || !strings.Contains(err.Error(), "no Garden home") {
			t.Fatalf("%s --remote error = %v, want the missing home", name, err)
		}
	}
	if *opened != len(runs) {
		t.Fatalf("sessions opened = %d, want %d", *opened, len(runs))
	}
	assertNoLocalRuntime(t, daemonURL, stateDB, runtimeLock)
}

func TestRemotePreRunSkipsRuntimeLeaseButLocalKeepsIt(t *testing.T) {
	_, _, runtimeLock := remoteHome(t)

	setFlag(t, sayCmd, "remote", "true")
	if err := remotePreRun(sayCmd, nil); err != nil {
		t.Fatalf("remote pre-run: %v", err)
	}
	if _, err := os.Stat(runtimeLock); !errors.Is(err, os.ErrNotExist) {
		t.Fatalf("remote pre-run created the runtime lock (%v)", err)
	}

	setFlag(t, sayCmd, "remote", "false")
	if err := remotePreRun(sayCmd, nil); err != nil {
		t.Fatalf("local pre-run: %v", err)
	}
	defer releaseCommandLease()
	if _, err := os.Stat(runtimeLock); err != nil {
		t.Fatalf("local pre-run must keep the shared runtime lease: %v", err)
	}
}

func TestRemoteHistoryShowsNewestWindow(t *testing.T) {
	remoteHome(t)
	gw := &cmdGateway{messages: []garden.Message{
		{ID: "00000000-0000-4000-8000-000000000001", Sequence: 1, AuthorName: "operator", Content: "first", CreatedAt: "2026-11-08T09:00:00Z"},
		{ID: "00000000-0000-4000-8000-000000000002", Sequence: 2, AuthorName: "claude", Content: "second", ReplyTo: "00000000-0000-4000-8000-000000000001", CreatedAt: "2026-11-08T09:00:02Z"},
	}}
	useRemoteGateway(t, gw)
	setFlag(t, historyCmd, "remote", "true")
	oldLast, oldBy := historyLast, historyBy
	historyLast, historyBy = 2, ""
	defer func() { historyLast, historyBy = oldLast, oldBy }()

	output := captureStdout(t, func() {
		if err := historyCmd.RunE(historyCmd, nil); err != nil {
			t.Fatalf("history --remote: %v", err)
		}
	})
	if !strings.Contains(output, "00000000 operator: first") || !strings.Contains(output, "00000000 claude (re: 00000000): second") {
		t.Fatalf("history output = %q", output)
	}
}

func TestRemoteHistoryFiltersAuthorAndSaysWhenEmpty(t *testing.T) {
	remoteHome(t)
	gw := &cmdGateway{messages: []garden.Message{
		{ID: "00000000-0000-4000-8000-000000000001", Sequence: 1, AuthorName: "operator", Content: "mine", CreatedAt: "2026-11-08T09:00:00Z"},
	}}
	useRemoteGateway(t, gw)
	setFlag(t, historyCmd, "remote", "true")
	oldLast, oldBy := historyLast, historyBy
	historyLast, historyBy = 5, "claude"
	defer func() { historyLast, historyBy = oldLast, oldBy }()

	output := captureStdout(t, func() {
		if err := historyCmd.RunE(historyCmd, nil); err != nil {
			t.Fatalf("history --remote --by: %v", err)
		}
	})
	if strings.TrimSpace(output) != "(no messages)" {
		t.Fatalf("output = %q, want (no messages)", output)
	}
}

func TestRemoteHistoryRefusesUnsupportedFlags(t *testing.T) {
	remoteHome(t)
	opened := failRemote(t)
	setFlag(t, historyCmd, "remote", "true")
	oldLast, oldRaw, oldThread := historyLast, historyRaw, historyThread
	defer func() { historyLast, historyRaw, historyThread = oldLast, oldRaw, oldThread }()

	historyLast, historyRaw, historyThread = 50, true, ""
	if err := historyCmd.RunE(historyCmd, nil); err == nil || !strings.Contains(err.Error(), "--raw is not available") {
		t.Fatalf("--raw error = %v", err)
	}
	historyRaw, historyThread = false, "abc123"
	if err := historyCmd.RunE(historyCmd, nil); err == nil || !strings.Contains(err.Error(), "--thread needs the full history") {
		t.Fatalf("--thread error = %v", err)
	}
	historyThread = ""
	for _, last := range []int{0, remotecli.MaxTail + 1} {
		historyLast = last
		if err := historyCmd.RunE(historyCmd, nil); err == nil || !strings.Contains(err.Error(), "--last must be 1 to 100") {
			t.Fatalf("--last %d error = %v", last, err)
		}
	}
	if *opened != 0 {
		t.Fatal("refused flags must not open a session")
	}
}

func TestRemoteHistoryExplainsGatewayWithoutTail(t *testing.T) {
	remoteHome(t)
	gw := &cmdGateway{readErr: &garden.Error{Code: "invalid_argument", Message: "unknown argument"}}
	useRemoteGateway(t, gw)
	setFlag(t, historyCmd, "remote", "true")
	oldLast := historyLast
	historyLast = 10
	defer func() { historyLast = oldLast }()

	err := historyCmd.RunE(historyCmd, nil)
	if err == nil || !strings.Contains(err.Error(), "upgrade") {
		t.Fatalf("history error = %v, want an upgrade instruction", err)
	}
}

func TestRemoteChatRefusesAsOfAnotherIdentity(t *testing.T) {
	remoteHome(t)
	gw := &cmdGateway{}
	useRemoteGateway(t, gw)
	setFlag(t, chatCmd, "remote", "true")
	oldAs := chatAs
	chatAs = "claude"
	defer func() { chatAs = oldAs }()

	err := chatCmd.RunE(chatCmd, nil)
	if err == nil || !strings.Contains(err.Error(), "can only name your own identity") {
		t.Fatalf("chat --remote --as error = %v", err)
	}
	if gw.closed != 1 {
		t.Fatalf("session closed %d times, want 1", gw.closed)
	}
}

// Unlocking a sealed identity waits for the passphrase, so the session must open without the
// call's deadline (Val's review, 8 November 2026).
func TestRemoteOpenIsNotUnderTheCallDeadline(t *testing.T) {
	for name, run := range map[string]func() error{
		"say":     func() error { return sayCmd.RunE(sayCmd, []string{"hi"}) },
		"history": func() error { return historyCmd.RunE(historyCmd, nil) },
	} {
		t.Run(name, func(t *testing.T) {
			remoteHome(t)
			setFlag(t, sayCmd, "remote", "true")
			setFlag(t, historyCmd, "remote", "true")
			gw := &cmdGateway{}
			old := openRemote
			openRemote = func(ctx context.Context) (*remotecli.Session, error) {
				if _, ok := ctx.Deadline(); ok {
					t.Error("the session opened under the call deadline, so a slow passphrase would expire it")
				}
				return remotecli.NewSession("operator", testRemoteBinding(), gw), nil
			}
			t.Cleanup(func() { openRemote = old })
			captureStdout(t, func() {
				if err := run(); err != nil {
					t.Fatalf("%s --remote: %v", name, err)
				}
			})
		})
	}
}
