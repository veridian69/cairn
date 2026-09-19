package chat

import (
	"context"
	"errors"
	"fmt"
	"strings"
	"testing"
	"time"

	tea "github.com/charmbracelet/bubbletea"
	"github.com/veridian69/cairn/a2a/internal/model"
)

func newTestModel(t *testing.T, svc Service) *Model {
	t.Helper()
	m := newModel(svc, ViewConfig{
		HumanIdentity:   "operator",
		AgentNames:      []string{"gpt", "claude"},
		InitialIdentity: "operator",
		PollInterval:    time.Hour,
		TailLimit:       10,
	}, nil)
	m.Update(tea.WindowSizeMsg{Width: 120, Height: 40})
	return m
}

func press(m *Model, key tea.KeyType) tea.Cmd {
	_, cmd := m.Update(tea.KeyMsg{Type: key})
	return cmd
}

func pressRune(m *Model, r rune) tea.Cmd {
	_, cmd := m.Update(tea.KeyMsg{Type: tea.KeyRunes, Runes: []rune{r}})
	return cmd
}

func makeMsg(id, author, content string) model.Message {
	return model.Message{
		ID: id, AuthorID: author, AuthorName: author, Content: content,
		CreatedAt: time.Now(), Metadata: map[string]any{},
	}
}

func applyCommand(t *testing.T, m *Model, cmd tea.Cmd) {
	t.Helper()
	if cmd == nil {
		t.Fatal("expected command, got nil")
	}
	m.Update(cmd())
}

func TestFocusRingOrder(t *testing.T) {
	m := newTestModel(t, &fakeViewService{})
	if !m.chatFocused() {
		t.Fatal("initial focus should be chat")
	}
	press(m, tea.KeyTab)
	if name, _ := m.focusedAgent(); name != "claude" {
		t.Fatalf("first tab focuses %q, want claude", name)
	}
	press(m, tea.KeyTab)
	if name, _ := m.focusedAgent(); name != "gpt" {
		t.Fatalf("second tab focuses %q, want gpt", name)
	}
	press(m, tea.KeyTab)
	if !m.chatFocused() {
		t.Fatal("third tab should return to chat")
	}
	press(m, tea.KeyShiftTab)
	if name, _ := m.focusedAgent(); name != "gpt" {
		t.Fatalf("shift+tab focuses %q, want gpt", name)
	}
}

func TestQQuitsFromFocusedAgent(t *testing.T) {
	m := newTestModel(t, &fakeViewService{})
	press(m, tea.KeyTab)
	cmd := pressRune(m, 'q')
	if cmd == nil {
		t.Fatal("q should quit while an agent has focus")
	}
	if _, ok := cmd().(tea.QuitMsg); !ok {
		t.Fatal("q on an agent should produce tea.QuitMsg")
	}
}

func TestTabFromStreamModeReturnsToHumanComposer(t *testing.T) {
	m := newTestModel(t, &fakeViewService{})
	press(m, tea.KeyEsc)
	m.state.SetCurrentIdentity("claude")
	press(m, tea.KeyTab)
	press(m, tea.KeyTab)
	press(m, tea.KeyTab)
	if !m.chatFocused() || m.state.Focus() != FocusInput || !m.composer.Focused() {
		t.Fatal("tabbing back to chat must activate the composer")
	}
	if got := m.state.CurrentIdentity(); got != "operator" {
		t.Fatalf("identity = %q, want operator", got)
	}
}

func TestTypingReachesComposerOnlyWhenChatFocused(t *testing.T) {
	m := newTestModel(t, &fakeViewService{})
	pressRune(m, 'h')
	if got := m.composer.Value(); got != "h" {
		t.Fatalf("composer = %q, want h", got)
	}
	press(m, tea.KeyTab)
	pressRune(m, 'x')
	if got := m.composer.Value(); got != "h" {
		t.Fatalf("composer changed while agent focused: %q", got)
	}
}

func TestCtrlCQuits(t *testing.T) {
	m := newTestModel(t, &fakeViewService{})
	cmd := press(m, tea.KeyCtrlC)
	if cmd == nil {
		t.Fatal("ctrl+c should return tea.Quit")
	}
	if _, ok := cmd().(tea.QuitMsg); !ok {
		t.Fatal("ctrl+c command should produce tea.QuitMsg")
	}
}

func TestSubmitSendsAndClearsOnlySubmittedText(t *testing.T) {
	svc := &fakeViewService{}
	m := newTestModel(t, svc)
	m.composer.SetValue("hello there")
	cmd := press(m, tea.KeyEnter)
	result := cmd()
	if svc.sentIdentity != "operator" || svc.sentContent != "hello there" {
		t.Fatalf("send recorded %q/%q", svc.sentIdentity, svc.sentContent)
	}
	m.Update(result)
	if m.composer.Value() != "" {
		t.Fatal("successful send should clear submitted text")
	}

	m.composer.SetValue("first")
	cmd = press(m, tea.KeyEnter)
	m.composer.SetValue("second")
	m.Update(cmd())
	if got := m.composer.Value(); got != "second" {
		t.Fatalf("composer = %q, want newer text preserved", got)
	}
}

func TestSubmitDoesNotClearTextRecreatedAfterSubmission(t *testing.T) {
	m := newTestModel(t, &fakeViewService{})
	m.composer.SetValue("same")
	cmd := press(m, tea.KeyEnter)

	pressRune(m, 'x')
	press(m, tea.KeyBackspace)
	if got := m.composer.Value(); got != "same" {
		t.Fatalf("composer = %q, want recreated submitted text", got)
	}

	m.Update(cmd())
	if got := m.composer.Value(); got != "same" {
		t.Fatalf("composer = %q, want post-submit edit preserved", got)
	}
}

func TestSubmitPreservesRawWhitespaceUntilSuccess(t *testing.T) {
	svc := &fakeViewService{}
	m := newTestModel(t, svc)
	m.composer.SetValue("  hello there  ")
	cmd := press(m, tea.KeyEnter)

	if svc.sentContent != "" {
		t.Fatal("send command ran before Bubble Tea executed it")
	}
	result := cmd()
	if got := svc.sentContent; got != "hello there" {
		t.Fatalf("sent content = %q, want trimmed content", got)
	}
	if got := m.composer.Value(); got != "  hello there  " {
		t.Fatalf("composer = %q before result, want raw submission", got)
	}
	m.Update(result)
	if got := m.composer.Value(); got != "" {
		t.Fatalf("composer = %q after success, want cleared", got)
	}
}

func TestStaleActionResultCannotReplaceNewerFeedback(t *testing.T) {
	m := newTestModel(t, &fakeViewService{})
	m.composer.SetValue("first")
	first := press(m, tea.KeyEnter)
	m.composer.SetValue("second")
	second := press(m, tea.KeyEnter)

	m.Update(first())
	if m.statusText != "" {
		t.Fatalf("stale result set feedback %q", m.statusText)
	}
	m.Update(second())
	if got := m.statusText; got != "sent" {
		t.Fatalf("latest result feedback = %q, want sent", got)
	}
}

func TestSuccessfulSendClearsMatchingComposerAfterUnrelatedAction(t *testing.T) {
	m := newTestModel(t, &fakeViewService{})
	m.composer.SetValue("hello")
	send := press(m, tea.KeyEnter)

	press(m, tea.KeyTab)
	pause := press(m, tea.KeySpace)
	if pause == nil {
		t.Fatal("pause should produce a command")
	}
	m.Update(send())
	if got := m.composer.Value(); got != "" {
		t.Fatalf("composer = %q, want successful submitted text cleared", got)
	}
	if m.statusText != "" {
		t.Fatalf("older send result replaced newer action feedback with %q", m.statusText)
	}
}

func TestSuccessfulControlClearsMatchingComposerAfterUnrelatedAction(t *testing.T) {
	m := newTestModel(t, &fakeViewService{})
	m.composer.SetValue("/pause claude")
	control := press(m, tea.KeyEnter)

	press(m, tea.KeyTab)
	if next := press(m, tea.KeySpace); next == nil {
		t.Fatal("focused-agent pause should produce a newer command")
	}
	m.Update(control())
	if got := m.composer.Value(); got != "" {
		t.Fatalf("composer = %q, want successful slash command cleared", got)
	}
	if m.statusText != "" {
		t.Fatalf("older control result replaced newer action feedback with %q", m.statusText)
	}
}

func TestSubmitFailurePreservesText(t *testing.T) {
	svc := &fakeViewService{sendErr: errors.New("boom")}
	m := newTestModel(t, svc)
	m.composer.SetValue("keep me")
	applyCommand(t, m, press(m, tea.KeyEnter))
	if got := m.composer.Value(); got != "keep me" {
		t.Fatalf("composer = %q, want preserved", got)
	}
	if !m.statusIsError || !strings.Contains(m.statusText, "send failed") {
		t.Fatalf("status = %q", m.statusText)
	}
}

func TestSlashCommandsPreserveFailuresAndRejectUnknown(t *testing.T) {
	svc := &fakeViewService{}
	m := newTestModel(t, svc)
	m.composer.SetValue("/pause claude")
	applyCommand(t, m, press(m, tea.KeyEnter))
	if svc.pauseAgent != "claude" || m.composer.Value() != "" {
		t.Fatalf("pause recorded %q, composer %q", svc.pauseAgent, m.composer.Value())
	}

	svc.pauseErr = errors.New("down")
	m.composer.SetValue("/pause gpt")
	applyCommand(t, m, press(m, tea.KeyEnter))
	if got := m.composer.Value(); got != "/pause gpt" {
		t.Fatalf("failed command changed composer to %q", got)
	}

	m.composer.SetValue("/wat")
	if cmd := press(m, tea.KeyEnter); cmd != nil {
		t.Fatal("unknown command should not produce I/O")
	}
	if !strings.Contains(m.statusText, "unknown command") || m.composer.Value() != "/wat" {
		t.Fatalf("unknown command state = %q/%q", m.statusText, m.composer.Value())
	}
}

func TestEnterOnAgentSeedsIdentityAndActivatesComposer(t *testing.T) {
	m := newTestModel(t, &fakeViewService{})
	press(m, tea.KeyEsc)
	press(m, tea.KeyTab)
	press(m, tea.KeyEnter)
	if got := m.state.CurrentIdentity(); got != "claude" {
		t.Fatalf("identity = %q, want claude", got)
	}
	if !m.chatFocused() || m.state.Focus() != FocusInput || !m.composer.Focused() {
		t.Fatal("enter on agent must activate the composer")
	}
}

func TestAgentPauseToggle(t *testing.T) {
	svc := &fakeViewService{}
	m := newTestModel(t, svc)
	m.presence["claude"] = AgentPresence{Name: "claude", State: "active"}
	press(m, tea.KeyTab)
	applyCommand(t, m, press(m, tea.KeySpace))
	if svc.pauseAgent != "claude" {
		t.Fatalf("pause agent = %q", svc.pauseAgent)
	}
	m.presence["claude"] = AgentPresence{Name: "claude", State: "paused"}
	applyCommand(t, m, pressRune(m, 'p'))
	if svc.resumeAgent != "claude" {
		t.Fatalf("resume agent = %q", svc.resumeAgent)
	}
}

func TestStreamPresenceConnectionAndPollMessages(t *testing.T) {
	svc := &fakeViewService{statuses: []AgentPresence{{Name: "claude", State: "active"}}}
	m := newTestModel(t, svc)
	cmd := pressStreamMessage(m, makeMsg("m1", "claude", "hello"))
	if len(m.state.OrderedIDs()) != 1 || cmd == nil {
		t.Fatal("stream message was not appended or redaction command missing")
	}
	applyCommand(t, m, cmd)

	_, poll := m.Update(statusMsg{
		generation: m.statusGeneration,
		statuses:   []AgentPresence{{Name: "claude", State: "active"}},
	})
	if m.presence["claude"].State != "active" || poll == nil {
		t.Fatal("status message was not applied or did not schedule poll")
	}
	m.Update(activityMsg{Agent: "claude", State: "thinking"})
	if m.presence["claude"].State != "thinking" {
		t.Fatal("activity message did not mark thinking")
	}
	m.Update(connectionMsg{State: "disconnected"})
	if !strings.Contains(m.connectionText, "reconnecting") {
		t.Fatalf("connection text = %q", m.connectionText)
	}
	_, refresh := m.Update(connectionMsg{State: "reconnected"})
	if refresh == nil || m.connectionText != "" {
		t.Fatal("reconnect should clear banner and request status")
	}
}

func TestStaleStatusResultCannotStartSecondPollChain(t *testing.T) {
	m := newTestModel(t, &fakeViewService{})
	m.statusGeneration = 4
	if _, cmd := m.Update(statusMsg{generation: 3}); cmd != nil {
		t.Fatal("stale status result should not schedule another poll")
	}
	if _, cmd := m.Update(pollMsg{generation: 3}); cmd != nil {
		t.Fatal("stale poll tick should not issue another request")
	}
}

func TestEscStreamKeysPromoteAndThread(t *testing.T) {
	svc := &fakeViewService{}
	m := newTestModel(t, svc)
	applyCommand(t, m, pressStreamMessage(m, makeMsg("m1", "claude", "one")))
	applyCommand(t, m, pressStreamMessage(m, makeMsg("m2", "gpt", "two")))
	press(m, tea.KeyEsc)
	pressRune(m, 'k')
	if m.state.SelectedIndex() != 0 {
		t.Fatalf("selected = %d, want 0", m.state.SelectedIndex())
	}
	applyCommand(t, m, pressRune(m, 'm'))
	if svc.promoted == nil || svc.promoted.ID != "m1" {
		t.Fatal("m did not promote selected message")
	}
	pressRune(m, 't')
	if !m.state.ThreadOpen() {
		t.Fatal("t should open thread")
	}
	if cmd := pressRune(m, 'q'); cmd == nil {
		t.Fatal("q should quit while the thread has focus")
	} else if _, ok := cmd().(tea.QuitMsg); !ok {
		t.Fatal("q in thread should produce tea.QuitMsg")
	}
	press(m, tea.KeyEsc)
	if m.state.ThreadOpen() {
		t.Fatal("Esc should close thread first")
	}
	press(m, tea.KeyEnd)
	if !m.state.AutoFollow() || m.state.SelectedIndex() != 1 {
		t.Fatal("End should jump to bottom and follow")
	}
}

func pressStreamMessage(m *Model, message model.Message) tea.Cmd {
	_, cmd := m.Update(streamMsg{Message: message})
	return cmd
}

func TestViewportFollowAndScrollback(t *testing.T) {
	m := newTestModel(t, &fakeViewService{})
	m.Update(tea.WindowSizeMsg{Width: 120, Height: 12})
	for i := 0; i < 30; i++ {
		cmd := pressStreamMessage(m, makeMsg(fmt.Sprintf("m%d", i), "claude", "line"))
		applyCommand(t, m, cmd)
	}
	if !m.vp.AtBottom() {
		t.Fatal("following viewport should remain at bottom")
	}
	press(m, tea.KeyEsc)
	press(m, tea.KeyPgUp)
	if m.state.AutoFollow() {
		t.Fatal("PageUp should stop following")
	}
	offset := m.vp.YOffset
	cmd := pressStreamMessage(m, makeMsg("new", "gpt", "later"))
	applyCommand(t, m, cmd)
	if m.state.UnseenCount() != 1 || m.vp.YOffset != offset {
		t.Fatalf("scrollback changed: unseen=%d offset=%d want=%d",
			m.state.UnseenCount(), m.vp.YOffset, offset)
	}
	press(m, tea.KeyEnd)
	if !m.state.AutoFollow() || !m.vp.AtBottom() {
		t.Fatal("End should restore follow at bottom")
	}
}

func TestSubscriptionsAndCommandsStopCleanly(t *testing.T) {
	svc := &fakeViewService{}
	sender := &recordingSender{}
	subCtx, cancel, err := subscribeAll(context.Background(), svc, sender)
	if err != nil {
		t.Fatalf("subscribeAll: %v", err)
	}
	cancel()
	<-subCtx.Done()
	svc.streamHandler(StreamEvent{})
	if len(sender.msgs) != 0 {
		t.Fatal("cancelled subscription forwarded a late event")
	}

	m := newTestModel(t, svc)
	started := make(chan struct{})
	release := make(chan struct{})
	cmd := m.trackCommand(func() tea.Msg {
		close(started)
		<-release
		return nil
	})
	go cmd()
	<-started
	stopped := make(chan struct{})
	go func() {
		m.stopCommands()
		close(stopped)
	}()
	select {
	case <-stopped:
		t.Fatal("stopCommands returned while command was active")
	case <-time.After(20 * time.Millisecond):
	}
	close(release)
	select {
	case <-stopped:
	case <-time.After(time.Second):
		t.Fatal("stopCommands did not return")
	}
}
