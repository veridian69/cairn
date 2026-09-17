package chat

import (
	"fmt"
	"strings"
	"testing"

	tea "github.com/charmbracelet/bubbletea"
	"github.com/charmbracelet/lipgloss"
	"github.com/charmbracelet/x/ansi"
	"github.com/muesli/termenv"

	"github.com/veridian69/cairn/a2a/internal/model"
)

func sizedModel(t *testing.T, width, height int) *Model {
	t.Helper()
	m := newTestModel(t, &fakeViewService{})
	m.Update(tea.WindowSizeMsg{Width: width, Height: height})
	return m
}

func addVisibleMessage(t *testing.T, m *Model, messageID, author, content string) {
	t.Helper()
	applyCommand(t, m, pressStreamMessage(m, makeMsg(messageID, author, content)))
}

func useTrueColor(t *testing.T) {
	t.Helper()
	previousProfile := lipgloss.ColorProfile()
	lipgloss.SetColorProfile(termenv.TrueColor)
	t.Cleanup(func() {
		lipgloss.SetColorProfile(previousProfile)
	})
}

func TestViewBeforeFirstSizeIsEmpty(t *testing.T) {
	m := newModel(&fakeViewService{}, ViewConfig{HumanIdentity: "operator"}, nil)
	if got := m.View(); got != "" {
		t.Fatalf("pre-size view = %q, want empty", got)
	}
}

func TestViewShowsMessageBlockAndAuthor(t *testing.T) {
	m := sizedModel(t, 120, 40)
	addVisibleMessage(t, m, "m1", "claude", "block content here")
	out := m.View()
	for _, want := range []string{"claude", "block content here", "▍"} {
		if !strings.Contains(out, want) {
			t.Fatalf("rendered view missing %q", want)
		}
	}
}

func TestAccountantLedgerTogglesWithoutPollutingConversation(t *testing.T) {
	m := sizedModel(t, 120, 20)
	proposal := makeMsg("m1", "val", "Conversational explanation stays in chat.")
	proposal.Accountant = &model.AccountantRecord{
		Proposer:       "val",
		Idea:           "Trial a four-day fortnight",
		Probability:    "medium",
		NextExperiment: "Ask HR for a reversible calculation",
	}
	applyCommand(t, m, pressStreamMessage(m, proposal))

	conversation := ansi.Strip(m.View())
	if !strings.Contains(conversation, "Conversational explanation stays in chat.") {
		t.Fatal("conversation text missing from chat view")
	}
	if strings.Contains(conversation, "Trial a four-day fortnight") {
		t.Fatal("accountant record leaked into chat view")
	}

	selected := m.state.SelectedIndex()
	press(m, tea.KeyEsc)
	pressRune(m, 'a')
	ledger := ansi.Strip(m.View())
	for _, want := range []string{
		"Accountant", "val", "medium", "Trial a four-day fortnight",
		"Ask HR for a reversible calculation",
	} {
		if !strings.Contains(ledger, want) {
			t.Fatalf("accountant ledger missing %q:\n%s", want, ledger)
		}
	}
	if strings.Contains(ledger, "Conversational explanation stays in chat.") {
		t.Fatal("chat content leaked into accountant ledger")
	}
	if m.state.SelectedIndex() != selected {
		t.Fatal("opening accountant ledger changed conversation selection")
	}

	press(m, tea.KeyEsc)
	if out := ansi.Strip(m.View()); !strings.Contains(out, "Conversational explanation stays in chat.") {
		t.Fatal("Esc did not return to conversation")
	}
}

func TestAccountantLedgerOmitsRedactedRecords(t *testing.T) {
	svc := &fakeViewService{
		redactions: map[string]RedactionStatus{
			"m1": {Redacted: true, Reason: "operator-request"},
		},
	}
	m := newTestModel(t, svc)
	m.Update(tea.WindowSizeMsg{Width: 100, Height: 16})
	proposal := makeMsg("m1", "val", "Sensitive conversation.")
	proposal.Accountant = &model.AccountantRecord{
		Proposer: "val",
		Idea:     "Sensitive structured proposal",
	}
	applyCommand(t, m, pressStreamMessage(m, proposal))

	press(m, tea.KeyEsc)
	pressRune(m, 'a')
	ledger := ansi.Strip(m.View())
	if strings.Contains(ledger, "Sensitive structured proposal") {
		t.Fatal("redacted accountant record leaked into ledger")
	}
	if !strings.Contains(ledger, "(no accountant records)") {
		t.Fatalf("empty ledger placeholder missing:\n%s", ledger)
	}
}

func TestStreamBlockRangesMatchRenderedBlankLines(t *testing.T) {
	m := sizedModel(t, 120, 40)
	for _, message := range []struct {
		id     string
		author string
	}{
		{id: "m1", author: "claude"},
		{id: "m2", author: "gpt"},
		{id: "m3", author: "claude"},
	} {
		addVisibleMessage(t, m, message.id, message.author, "body")
	}

	content, ranges := m.streamContentWithLines(60)
	lines := strings.Split(content, "\n")
	wantRanges := []blockRange{
		{start: 0, end: 2},
		{start: 3, end: 5},
		{start: 6, end: 8},
	}
	if len(lines) != 8 {
		t.Fatalf("rendered line count = %d, want 8", len(lines))
	}
	if len(ranges) != len(wantRanges) {
		t.Fatalf("range count = %d, want %d", len(ranges), len(wantRanges))
	}
	for index, want := range wantRanges {
		if got := ranges[index]; got != want {
			t.Fatalf("range %d = %+v, want %+v", index, got, want)
		}
		if !strings.Contains(lines[want.start], "▍") {
			t.Fatalf("range %d starts on non-block line %q", index, lines[want.start])
		}
		if index < len(wantRanges)-1 && lines[want.end] != "" {
			t.Fatalf("range %d separator = %q, want blank", index, lines[want.end])
		}
	}
	if ranges[len(ranges)-1].end != len(lines) {
		t.Fatalf("last range ends at %d, content has %d lines",
			ranges[len(ranges)-1].end, len(lines))
	}
}

func TestUnknownRedactionStateFailsClosed(t *testing.T) {
	m := sizedModel(t, 120, 40)
	m.Update(streamMsg{Message: makeMsg("m1", "claude", "secret")})
	out := m.View()
	if strings.Contains(out, "secret") {
		t.Fatal("content rendered before redaction status was known")
	}
	if !strings.Contains(out, "[checking redaction]") {
		t.Fatal("pending redaction placeholder missing")
	}
}

func TestRedactedMessageRendersPlaceholder(t *testing.T) {
	svc := &fakeViewService{
		redactions: map[string]RedactionStatus{
			"m1": {Redacted: true, Reason: "operator-request"},
		},
	}
	m := newTestModel(t, svc)
	m.Update(tea.WindowSizeMsg{Width: 120, Height: 40})
	applyCommand(t, m, pressStreamMessage(m, makeMsg("m1", "claude", "secret")))
	out := m.View()
	if strings.Contains(out, "secret") {
		t.Fatal("redacted content leaked")
	}
	if !strings.Contains(out, "[redacted: operator-request]") {
		t.Fatal("redaction placeholder missing")
	}
}

func TestViewUsesFullWidthWithoutSidebar(t *testing.T) {
	m := sizedModel(t, 120, 40)
	addVisibleMessage(t, m, "m1", "claude", "full-width body")

	out := ansi.Strip(m.View())
	if strings.Contains(out, "(you)") {
		t.Fatal("single-column layout should not render a participants sidebar")
	}
	for _, line := range strings.Split(out, "\n") {
		if strings.Contains(line, "full-width body") {
			if !strings.HasPrefix(line, "▍ ") {
				t.Fatalf("message body does not start at column zero: %q", line)
			}
			return
		}
	}
	t.Fatal("message body missing from rendered view")
}

func TestFocusedAgentIsHighlightedInStatusBar(t *testing.T) {
	useTrueColor(t)
	m := sizedModel(t, 120, 12)
	m.presence["claude"] = AgentPresence{Name: "claude", State: "active"}
	press(m, tea.KeyTab)

	want := lipgloss.NewStyle().
		Foreground(m.colours.colour("claude")).
		Background(colorSurface).
		Bold(true).
		Render("● claude")
	if bar := m.renderStatusBar(120); !strings.Contains(bar, want) {
		t.Fatal("status bar does not highlight the focused agent")
	}
}

func TestTinyTerminalAndTallContentStayBounded(t *testing.T) {
	tiny := sizedModel(t, 19, 3)
	out := tiny.View()
	if !strings.Contains(out, "too small") {
		t.Fatal("tiny terminal fallback missing")
	}
	if lipgloss.Width(out) > 19 || lipgloss.Height(out) > 3 {
		t.Fatalf("tiny frame is %dx%d", lipgloss.Width(out), lipgloss.Height(out))
	}

	names := make([]string, 40)
	for i := range names {
		names[i] = fmt.Sprintf("agent-%02d", i)
	}
	m := newModel(&fakeViewService{}, ViewConfig{
		HumanIdentity: "operator", InitialIdentity: "operator", AgentNames: names,
	}, nil)
	m.Update(tea.WindowSizeMsg{Width: 120, Height: 12})
	out = m.View()
	if got := lipgloss.Height(out); got != 12 {
		t.Fatalf("frame height = %d, want 12", got)
	}
	if strings.Contains(out, "operator (you)") {
		t.Fatal("single-column layout rendered a participants sidebar")
	}
}

func TestStatusBarIsExactlyOneLine(t *testing.T) {
	m := sizedModel(t, 70, 12)
	if bar := m.renderStatusBar(70); !strings.Contains(bar, "gpt") {
		t.Fatal("status bar should include per-agent presence when space permits")
	}
	m.connectionText = strings.Repeat("connection-error-", 8)
	bar := m.renderStatusBar(70)
	if got := lipgloss.Height(bar); got != 1 {
		t.Fatalf("status bar height = %d, want 1", got)
	}
	if got := lipgloss.Width(bar); got != 70 {
		t.Fatalf("status bar width = %d, want 70", got)
	}
}

func TestStatusBarKeepsFullAgentNamesAtWideWidth(t *testing.T) {
	m := sizedModel(t, 120, 12)
	for _, name := range m.agentNames {
		m.presence[name] = AgentPresence{Name: name, State: "active"}
	}

	bar := ansi.Strip(m.renderStatusBar(120))
	for _, want := range []string{"● claude", "● gpt"} {
		if !strings.Contains(bar, want) {
			t.Fatalf("wide status bar missing full agent name %q: %q", want, bar)
		}
	}
}

func TestStatusBarKeepsFocusedAgentVisibleWhenSpaceIsTight(t *testing.T) {
	useTrueColor(t)
	for _, width := range []int{20, 28} {
		t.Run(fmt.Sprintf("width_%d", width), func(t *testing.T) {
			m := newModel(&fakeViewService{}, ViewConfig{
				HumanIdentity:   "operator",
				InitialIdentity: "operator",
				AgentNames: []string{
					"claude", "deepseek", "gemini", "gpt",
				},
			}, nil)
			m.Update(tea.WindowSizeMsg{Width: width, Height: 12})
			for _, name := range m.agentNames {
				m.presence[name] = AgentPresence{Name: name, State: "active"}
			}
			m.statusText = strings.Repeat("status-", 10)
			press(m, tea.KeyTab)

			bar := ansi.Strip(m.renderStatusBar(width))
			if !strings.Contains(bar, "● claude") {
				t.Fatalf("focused agent missing at width %d: %q", width, bar)
			}
			if got := lipgloss.Width(bar); got != width {
				t.Fatalf("status width = %d, want %d", got, width)
			}
		})
	}
}

func TestThreadUsesBoundedViewportAndRedactedBlockStyle(t *testing.T) {
	svc := &fakeViewService{
		redactions: map[string]RedactionStatus{
			"m1": {Redacted: true, Reason: "safety"},
		},
	}
	m := newTestModel(t, svc)
	m.Update(tea.WindowSizeMsg{Width: 90, Height: 12})
	applyCommand(t, m, pressStreamMessage(
		m, makeMsg("m1", "claude", strings.Repeat("sensitive words ", 20)),
	))
	press(m, tea.KeyEsc)
	pressRune(m, 't')
	out := m.View()
	if !strings.Contains(out, "Thread") ||
		!strings.Contains(out, "[redacted: safety]") {
		t.Fatal("thread missing redacted block")
	}
	if strings.Contains(out, "sensitive words") {
		t.Fatal("thread leaked redacted content")
	}
	if got := lipgloss.Height(out); got != 12 {
		t.Fatalf("thread frame height = %d, want 12", got)
	}
}

func TestThreadSurfaceDoesNotLeaveContentUnstyledAfterReset(t *testing.T) {
	useTrueColor(t)
	m := sizedModel(t, 90, 12)
	addVisibleMessage(t, m, "m1", "claude", "surface-backed content")
	press(m, tea.KeyEsc)
	pressRune(m, 't')

	panel := m.renderThread(90, 8)
	const reset = "\x1b[0m"
	for offset := 0; ; {
		index := strings.Index(panel[offset:], reset)
		if index < 0 {
			break
		}
		next := offset + index + len(reset)
		if next < len(panel) && panel[next] != '\x1b' && panel[next] != '\n' {
			t.Fatalf("unstyled byte %q follows reset in surface panel near %q",
				panel[next], panel[max(0, next-12):min(len(panel), next+24)])
		}
		offset = next
	}
}

func TestThreadSurfacePaintsViewportBlankRows(t *testing.T) {
	useTrueColor(t)
	m := sizedModel(t, 90, 12)
	addVisibleMessage(t, m, "m1", "claude", "short thread")
	press(m, tea.KeyEsc)
	pressRune(m, 't')

	panel := m.renderThread(90, 8)
	lines := strings.Split(panel, "\n")
	want := lipgloss.NewStyle().
		Background(colorSurface).
		Render(strings.Repeat(" ", 90))
	if got := lines[len(lines)-1]; got != want {
		t.Fatalf("last thread row is not surface-painted: %q", got)
	}
}

func TestSelectedBlockHeaderPaintsFullWidth(t *testing.T) {
	useTrueColor(t)
	m := sizedModel(t, 120, 12)
	addVisibleMessage(t, m, "m1", "claude", "body line long enough to wrap nowhere")
	press(m, tea.KeyEsc) // stream mode: selection highlight active

	block := m.renderMessageBlock(mustMessage(t, m, "m1"), 60, 0, true, colorBg)
	lines := strings.Split(block, "\n")
	if len(lines) < 2 {
		t.Fatalf("block has %d lines, want at least header and body", len(lines))
	}
	headerWidth := lipgloss.Width(lines[0])
	for index, line := range lines[1:] {
		if lipgloss.Width(line) != headerWidth {
			t.Fatalf("line %d width %d != header width %d — header not padded to block width",
				index+1, lipgloss.Width(line), headerWidth)
		}
	}
}

func mustMessage(t *testing.T, m *Model, id string) model.Message {
	t.Helper()
	message, ok := m.state.Message(id)
	if !ok {
		t.Fatalf("message %s not in state", id)
	}
	return message
}
