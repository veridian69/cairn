package chat

import (
	"fmt"
	"strings"

	"github.com/charmbracelet/lipgloss"
	"github.com/charmbracelet/x/ansi"

	"github.com/veridian69/cairn/a2a/internal/model"
)

const (
	minChatWidthForSplit = 100
	composerHeight       = 3
	statusBarHeight      = 1
)

type blockRange struct {
	start int
	end   int
}

func (m *Model) View() string {
	if !m.sized {
		return ""
	}
	if m.width < 20 || m.height < 5 {
		return fitBlock("terminal too small", max(1, m.width), max(1, m.height), lipgloss.NewStyle())
	}

	chatWidth := m.chatWidth()
	bodyHeight := m.bodyHeight()
	var chatPanel string
	if m.accountantOpen {
		chatPanel = m.renderAccountant(chatWidth, bodyHeight)
	} else if m.state.ThreadOpen() {
		if chatWidth >= minChatWidthForSplit {
			streamWidth := chatWidth / 2
			chatPanel = lipgloss.JoinHorizontal(
				lipgloss.Top,
				m.renderStream(streamWidth, bodyHeight),
				m.renderThread(chatWidth-streamWidth, bodyHeight),
			)
		} else {
			chatPanel = m.renderThread(chatWidth, bodyHeight)
		}
	} else {
		chatPanel = m.renderStream(chatWidth, bodyHeight)
	}

	chatColumn := lipgloss.JoinVertical(
		lipgloss.Left,
		chatPanel,
		m.renderComposer(chatWidth),
	)
	frame := lipgloss.JoinVertical(
		lipgloss.Left,
		chatColumn,
		m.renderStatusBar(m.width),
	)
	return fitBlock(frame, m.width, m.height, lipgloss.NewStyle().Background(colorBg))
}

func (m *Model) chatWidth() int {
	return max(1, m.width)
}

func (m *Model) bodyHeight() int {
	return max(1, m.height-composerHeight-statusBarHeight)
}

func (m *Model) streamWidth() int {
	width := m.chatWidth()
	if m.state.ThreadOpen() && width >= minChatWidthForSplit {
		return max(1, width/2)
	}
	return width
}

// rebuildStream is the only place that updates stream content or offset.
func (m *Model) rebuildStream(keepSelectionVisible bool) {
	if !m.sized {
		return
	}
	oldOffset := m.vp.YOffset
	m.vp.Width = m.streamWidth()
	m.vp.Height = m.bodyHeight()
	m.composer.Width = max(1, m.chatWidth()-6)
	content, ranges := m.streamContentWithLines(m.vp.Width)
	m.vp.SetContent(content)
	switch {
	case m.state.AutoFollow():
		m.vp.GotoBottom()
	case keepSelectionVisible:
		m.ensureVisible(ranges)
	default:
		m.vp.SetYOffset(oldOffset)
	}
}

func (m *Model) rebuildThread() {
	if !m.sized || !m.state.ThreadOpen() {
		return
	}
	width := m.chatWidth()
	if width >= minChatWidthForSplit {
		width -= width / 2
	}
	oldOffset := m.threadVP.YOffset
	m.threadVP.Width = max(1, width)
	m.threadVP.Height = m.bodyHeight()
	m.threadVP.SetContent(fillLines(
		m.threadContent(m.threadVP.Width),
		m.threadVP.Width,
		lipgloss.NewStyle().Background(colorSurface),
	))
	m.threadVP.SetYOffset(oldOffset)
}

func (m *Model) rebuildAccountant(follow bool) {
	if !m.sized {
		return
	}
	wasAtBottom := m.accountantVP.AtBottom()
	oldOffset := m.accountantVP.YOffset
	m.accountantVP.Width = m.chatWidth()
	m.accountantVP.Height = m.bodyHeight()
	m.accountantVP.SetContent(fillLines(
		m.accountantContent(m.accountantVP.Width),
		m.accountantVP.Width,
		lipgloss.NewStyle().Background(colorSurface),
	))
	if follow && wasAtBottom {
		m.accountantVP.GotoBottom()
		return
	}
	m.accountantVP.SetYOffset(oldOffset)
}

func (m *Model) renderStream(width, height int) string {
	return fitBlock(
		m.vp.View(), width, height,
		lipgloss.NewStyle().Background(colorBg),
	)
}

func (m *Model) renderAccountant(width, height int) string {
	content := strings.TrimRight(m.accountantVP.View(), " \n")
	return fitBlock(content, width, height, lipgloss.NewStyle().Background(colorSurface))
}

func (m *Model) renderThread(width, height int) string {
	content := strings.TrimRight(m.threadVP.View(), " \n")
	return fitBlock(content, width, height, lipgloss.NewStyle().Background(colorSurface))
}

func (m *Model) renderComposer(width int) string {
	style := styleComposerBlurred
	if m.chatFocused() && m.state.Focus() == FocusInput {
		style = styleComposerFocused
	}
	return fitBlock(
		style.Width(max(1, width-2)).Render(m.composer.View()),
		width,
		composerHeight,
		lipgloss.NewStyle().Background(colorBg),
	)
}

func (m *Model) streamContentWithLines(width int) (string, []blockRange) {
	var blocks []string
	ranges := make([]blockRange, 0, len(m.state.OrderedIDs()))
	line := 0
	for index, id := range m.state.OrderedIDs() {
		message, ok := m.state.Message(id)
		if !ok {
			ranges = append(ranges, blockRange{start: line, end: line})
			continue
		}
		block := m.renderMessageBlock(
			message, width, 0, index == m.state.SelectedIndex(), colorBg,
		)
		blockLines := lipgloss.Height(block)
		ranges = append(ranges, blockRange{start: line, end: line + blockLines})
		blocks = append(blocks, block)
		line += blockLines
		if index < len(m.state.OrderedIDs())-1 {
			line++
		}
	}
	return strings.Join(blocks, "\n\n"), ranges
}

func (m *Model) renderMessageBlock(
	message model.Message,
	width int,
	depth int,
	selected bool,
	panelBackground lipgloss.Color,
) string {
	indent := strings.Repeat("  ", max(0, depth))
	available := max(1, width-lipgloss.Width(indent)-2)
	colour := m.colours.colour(message.AuthorName)
	background := panelBackground
	if selected && m.state.Focus() != FocusInput {
		background = colorSurface
	}
	authorStyle := lipgloss.NewStyle().
		Foreground(colour).
		Background(background).
		Bold(true)
	mutedStyle := styleMuted.Background(background)
	spacingStyle := lipgloss.NewStyle().Background(background)
	header := authorStyle.Render(message.AuthorName)
	if message.ReplyTo != nil {
		parentName := "..."
		if parent, ok := m.state.Message(*message.ReplyTo); ok {
			parentName = parent.AuthorName
		}
		header += mutedStyle.Render("  ↩ " + parentName)
	}
	if !message.CreatedAt.IsZero() {
		header += mutedStyle.Render("  " + message.CreatedAt.Local().Format("15:04"))
	}
	if pad := available - lipgloss.Width(header); pad > 0 {
		header += spacingStyle.Render(strings.Repeat(" ", pad))
	}

	content := m.contentFor(message)
	bodyStyle := styleText.Background(background)
	if strings.HasPrefix(content, "[redacted:") {
		bodyStyle = styleRedacted.Background(background)
	} else if strings.HasPrefix(content, "[checking redaction]") ||
		strings.HasPrefix(content, "[redaction lookup error:") {
		bodyStyle = mutedStyle
	}
	wrapped := bodyStyle.Width(available).Render(content)
	lines := append([]string{header}, strings.Split(wrapped, "\n")...)
	barStyle := lipgloss.NewStyle().Foreground(colour).Background(background)
	for index, line := range lines {
		lines[index] = spacingStyle.Render(indent) +
			barStyle.Render("▍") +
			spacingStyle.Render(" ") +
			line
	}
	return strings.Join(lines, "\n")
}

func (m *Model) contentFor(message model.Message) string {
	if err, ok := m.redactionErrors[message.ID]; ok {
		return fmt.Sprintf("[redaction lookup error: %v]", err)
	}
	redaction, known := m.redactions[message.ID]
	if !known {
		return "[checking redaction]"
	}
	if redaction.Redacted {
		return fmt.Sprintf("[redacted: %s]", redaction.Reason)
	}
	return message.Content
}

func (m *Model) ensureVisible(ranges []blockRange) {
	selected := m.state.SelectedIndex()
	if selected < 0 || selected >= len(ranges) {
		return
	}
	block := ranges[selected]
	switch {
	case block.start < m.vp.YOffset:
		m.vp.SetYOffset(block.start)
	case block.end > m.vp.YOffset+m.vp.Height:
		m.vp.SetYOffset(block.end - m.vp.Height)
	}
}

func (m *Model) renderStatusBar(width int) string {
	if width <= 0 {
		return ""
	}
	surfaceSpacing := styleStatusBar
	left := styleChip.Render("a2a") + surfaceSpacing.Render(" ") +
		styleIdentity.Render(m.state.CurrentIdentity()+" ▸")
	left = ansi.Truncate(left, max(1, width/2), "")

	var agentParts []string
	focusedPart := ""
	for index, name := range m.agentNames {
		status := m.presence[name]
		agentStyle := lipgloss.NewStyle().
			Foreground(m.colours.colour(name)).
			Background(colorSurface)
		if index == m.focusIndex {
			agentStyle = agentStyle.Bold(true)
		}
		part := agentStyle.Render(presenceDot(status.State) + " " + name)
		agentParts = append(agentParts, part)
		if index == m.focusIndex {
			focusedPart = part
		}
	}
	var extraParts []string
	if unseen := m.state.UnseenCount(); unseen > 0 {
		extraParts = append(extraParts,
			styleText.Background(colorSurface).Render(fmt.Sprintf("%d new", unseen)),
		)
	}
	if m.connectionText != "" {
		extraParts = append(extraParts,
			styleError.Background(colorSurface).Render(m.connectionText),
		)
	}
	if m.pollError != "" {
		extraParts = append(extraParts,
			styleError.Background(colorSurface).Render(m.pollError),
		)
	}
	if m.statusText != "" {
		style := styleMuted
		if m.statusIsError {
			style = styleError
		}
		extraParts = append(extraParts, style.Background(colorSurface).Render(m.statusText))
	}

	hintText := "Tab focus · Enter send-as · a accountant · t thread · q quit"
	if m.accountantOpen {
		hintText = "a/Esc chat · j/k scroll · q quit"
	}
	hints := styleMuted.Background(colorSurface).Render(hintText)
	separator := surfaceSpacing.Render("  ")
	rightParts := append(append([]string(nil), agentParts...), extraParts...)
	right := strings.Join(rightParts, separator)
	if focusedPart != "" {
		maxLeftWidth := max(0, width-lipgloss.Width(focusedPart)-1)
		left = ansi.Truncate(left, maxLeftWidth, "")
	}
	available := max(0, width-lipgloss.Width(left)-1)
	if lipgloss.Width(right) > available {
		if focusedPart != "" && lipgloss.Width(focusedPart) <= available {
			compactParts := []string{focusedPart}
			candidates := make([]string, 0, len(rightParts)-1)
			for index, part := range agentParts {
				if index != m.focusIndex {
					candidates = append(candidates, part)
				}
			}
			candidates = append(candidates, extraParts...)
			for _, part := range candidates {
				candidate := strings.Join(append(compactParts, part), separator)
				if lipgloss.Width(candidate) > available {
					continue
				}
				compactParts = append(compactParts, part)
			}
			right = strings.Join(compactParts, separator)
		} else {
			remove := lipgloss.Width(right) - available + 1
			right = ansi.TruncateLeft(right, remove, "…")
		}
	}
	middle := ""
	gap := width - lipgloss.Width(left) - lipgloss.Width(right)
	if gap >= lipgloss.Width(hints)+2 {
		middle = hints
	}

	spaces := max(
		1,
		width-lipgloss.Width(left)-lipgloss.Width(middle)-lipgloss.Width(right),
	)
	content := left
	if middle != "" {
		leftGap := max(1, spaces/2)
		rightGap := max(1, spaces-leftGap)
		content += surfaceSpacing.Render(strings.Repeat(" ", leftGap))
		content += middle
		content += surfaceSpacing.Render(strings.Repeat(" ", rightGap))
	} else {
		content += surfaceSpacing.Render(strings.Repeat(" ", spaces))
	}
	content += right
	return fitLine(content, width, styleStatusBar)
}

func (m *Model) accountantContent(width int) string {
	background := lipgloss.NewStyle().Background(colorSurface)
	blocks := []string{
		styleText.Background(colorSurface).Bold(true).Render("Accountant"),
	}
	count := 0
	for _, id := range m.state.OrderedIDs() {
		message, ok := m.state.Message(id)
		if !ok || message.Accountant == nil {
			continue
		}
		redaction, known := m.redactions[id]
		if !known || redaction.Redacted || m.redactionErrors[id] != nil {
			continue
		}
		blocks = append(blocks, m.renderAccountantRecord(message, width))
		count++
	}
	if count == 0 {
		blocks = append(blocks, styleMuted.Background(colorSurface).Render("(no accountant records)"))
	}
	return fillLines(strings.Join(blocks, "\n\n"), width, background)
}

func (m *Model) renderAccountantRecord(message model.Message, width int) string {
	record := message.Accountant
	colour := m.colours.colour(message.AuthorName)
	header := lipgloss.NewStyle().
		Foreground(colour).
		Background(colorSurface).
		Bold(true).
		Render(message.AuthorName)
	header += styleMuted.Background(colorSurface).Render(
		fmt.Sprintf("  [%s]  %s  %s",
			record.Probability,
			model.ShortID(message.ID),
			message.CreatedAt.Local().Format("15:04"),
		),
	)
	lines := []string{
		header,
		styleText.Background(colorSurface).Bold(true).Render(record.Idea),
	}
	if len(record.Assumptions) > 0 {
		lines = append(lines, styleText.Background(colorSurface).Render(
			"Assumptions: "+strings.Join(record.Assumptions, "; "),
		))
	}
	if record.Evidence != "" {
		lines = append(lines, styleText.Background(colorSurface).Render("Evidence: "+record.Evidence))
	}
	if record.CapitalRequiredCHF != nil {
		lines = append(lines, styleText.Background(colorSurface).Render(
			fmt.Sprintf("Capital: CHF %g", *record.CapitalRequiredCHF),
		))
	}
	if record.TimeRequired != "" {
		lines = append(lines, styleText.Background(colorSurface).Render("Time: "+record.TimeRequired))
	}
	if record.DownsideIfWrong != "" {
		lines = append(lines, styleText.Background(colorSurface).Render("Downside: "+record.DownsideIfWrong))
	}
	if record.NextExperiment != "" {
		lines = append(lines, styleText.Background(colorSurface).Render("Next: "+record.NextExperiment))
	}
	if record.Dissent != "" {
		lines = append(lines, styleText.Background(colorSurface).Render("Dissent: "+record.Dissent))
	}
	return lipgloss.NewStyle().
		Background(colorSurface).
		Width(max(1, width-2)).
		Render(strings.Join(lines, "\n"))
}

func (m *Model) threadContent(width int) string {
	thread, ok := m.state.BuildThread(8)
	if !ok {
		return ""
	}
	blocks := []string{
		styleText.Background(colorSurface).Bold(true).Render("Thread"),
	}
	for _, item := range thread.Context {
		blocks = append(blocks, m.renderThreadItem(item, width))
	}
	if len(thread.Replies) > 0 {
		blocks = append(blocks, styleMuted.Background(colorSurface).Render("Replies"))
		for _, item := range thread.Replies {
			blocks = append(blocks, m.renderThreadItem(item, width))
		}
	}
	return strings.Join(blocks, "\n")
}

func (m *Model) renderThreadItem(item ThreadItem, width int) string {
	if item.IsMissing {
		spacing := lipgloss.NewStyle().Background(colorSurface)
		return spacing.Render(strings.Repeat("  ", max(0, item.Depth))) +
			styleMuted.Background(colorSurface).
				Render("(missing: "+item.MissingID+")")
	}
	return m.renderMessageBlock(
		item.Message, width, item.Depth, false, colorSurface,
	)
}

func presenceDot(state string) string {
	switch state {
	case "active":
		return "●"
	case "thinking":
		return "◐"
	default:
		return "○"
	}
}

func fitBlock(content string, width, height int, style lipgloss.Style) string {
	width, height = max(1, width), max(1, height)
	lines := strings.Split(fillLines(content, width, style), "\n")
	if len(lines) > height {
		lines = lines[:height]
	}
	for len(lines) < height {
		lines = append(lines, fitLine("", width, style))
	}
	return strings.Join(lines, "\n")
}

func fillLines(content string, width int, style lipgloss.Style) string {
	lines := strings.Split(content, "\n")
	for index, line := range lines {
		lines[index] = fitLine(line, width, style)
	}
	return strings.Join(lines, "\n")
}

func fitLine(content string, width int, style lipgloss.Style) string {
	width = max(1, width)
	content = ansi.Truncate(strings.ReplaceAll(content, "\n", " "), width, "")
	padding := max(0, width-lipgloss.Width(content))
	return content + style.Render(strings.Repeat(" ", padding))
}
