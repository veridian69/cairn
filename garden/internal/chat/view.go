package chat

import (
	"context"
	"os/user"
	"sort"
	"strings"
	"sync"
	"time"

	"github.com/charmbracelet/bubbles/textinput"
	"github.com/charmbracelet/bubbles/viewport"
	tea "github.com/charmbracelet/bubbletea"
)

type ViewConfig struct {
	HumanIdentity   string
	AgentNames      []string
	InitialIdentity string
	PollInterval    time.Duration
	TailLimit       int
}

type View struct {
	service Service
	model   *Model
}

func NewView(ctx context.Context, service Service, cfg ViewConfig) (*View, error) {
	if cfg.PollInterval <= 0 {
		cfg.PollInterval = 3 * time.Second
	}
	if cfg.TailLimit <= 0 {
		cfg.TailLimit = 50
	}
	human := strings.TrimSpace(cfg.HumanIdentity)
	if human == "" {
		if current, err := user.Current(); err == nil && current != nil {
			human = current.Username
		}
	}
	if human == "" {
		human = "human"
	}
	cfg.HumanIdentity = human
	if strings.TrimSpace(cfg.InitialIdentity) == "" {
		cfg.InitialIdentity = human
	}

	initial, err := service.Tail(ctx, cfg.TailLimit)
	if err != nil {
		return nil, err
	}
	model := newModel(service, cfg, initial)
	model.ctx = ctx
	return &View{service: service, model: model}, nil
}

func (v *View) Run(ctx context.Context) error {
	runCtx, cancelRun := context.WithCancel(ctx)
	v.model.ctx = runCtx
	program := tea.NewProgram(v.model, tea.WithContext(runCtx), tea.WithAltScreen())
	_, cancelSubscriptions, err := subscribeAll(runCtx, v.service, program)
	if err != nil {
		cancelRun()
		return err
	}

	_, runErr := program.Run()
	cancelRun()
	cancelSubscriptions()
	v.model.stopCommands()
	return runErr
}

type Model struct {
	ctx     context.Context
	svc     Service
	cfg     ViewConfig
	state   *State
	colours *colourMap

	agentNames []string
	focusIndex int

	presence map[string]AgentPresence

	redactions      map[string]RedactionStatus
	redactionErrors map[string]error

	composer       textinput.Model
	vp             viewport.Model
	threadVP       viewport.Model
	accountantVP   viewport.Model
	accountantOpen bool

	width  int
	height int
	sized  bool

	statusText       string
	statusIsError    bool
	pollError        string
	connectionText   string
	editorRevision   uint64
	actionGeneration uint64

	statusGeneration uint64
	statusCancel     context.CancelFunc

	commandMu sync.Mutex
	commandWG sync.WaitGroup
	stopping  bool
}

func newModel(service Service, cfg ViewConfig, initial []StreamEvent) *Model {
	names := uniqueSorted(cfg.AgentNames)
	composer := textinput.New()
	composer.Placeholder = "Type a message..."
	composer.Prompt = "> "
	composer.Focus()

	return &Model{
		ctx:             context.Background(),
		svc:             service,
		cfg:             cfg,
		state:           NewState(initial, cfg.InitialIdentity),
		colours:         newColourMap(cfg.HumanIdentity, names),
		agentNames:      names,
		focusIndex:      len(names),
		presence:        make(map[string]AgentPresence),
		redactions:      make(map[string]RedactionStatus),
		redactionErrors: make(map[string]error),
		composer:        composer,
	}
}

func (m *Model) Init() tea.Cmd {
	commands := []tea.Cmd{m.requestStatus()}
	for _, id := range m.state.OrderedIDs() {
		commands = append(commands, m.trackCommand(redactionCmd(m.svc, id)))
	}
	return tea.Batch(commands...)
}

func (m *Model) Update(message tea.Msg) (tea.Model, tea.Cmd) {
	switch msg := message.(type) {
	case tea.WindowSizeMsg:
		m.width, m.height, m.sized = msg.Width, msg.Height, true
		m.rebuildStream(false)
		m.rebuildThread()
		m.rebuildAccountant(false)
		return m, nil
	case tea.KeyMsg:
		return m.handleKey(msg)
	case streamMsg:
		m.state.Append(StreamEvent(msg))
		m.rebuildStream(false)
		m.rebuildAccountant(true)
		if m.state.ThreadOpen() {
			m.rebuildThread()
		}
		return m, m.trackCommand(redactionCmd(m.svc, msg.Message.ID))
	case activityMsg:
		m.applyActivity(ActivityEvent(msg))
		return m, nil
	case connectionMsg:
		return m.applyConnection(ConnectionEvent(msg))
	case statusMsg:
		return m.applyStatus(msg)
	case pollMsg:
		if msg.generation != m.statusGeneration {
			return m, nil
		}
		return m, m.requestStatus()
	case redactionResultMsg:
		if msg.err != nil {
			m.redactionErrors[msg.messageID] = msg.err
			delete(m.redactions, msg.messageID)
		} else {
			m.redactions[msg.messageID] = msg.status
			delete(m.redactionErrors, msg.messageID)
		}
		m.rebuildStream(false)
		m.rebuildAccountant(false)
		if m.state.ThreadOpen() {
			m.rebuildThread()
		}
		return m, nil
	case sendResultMsg:
		if msg.err != nil {
			if msg.actionID == m.actionGeneration {
				m.setError("send failed: " + msg.err.Error())
			}
			return m, nil
		}
		if m.editorRevision == msg.revision && m.composer.Value() == msg.raw {
			m.composer.SetValue("")
			m.editorRevision++
		}
		if msg.actionID == m.actionGeneration {
			m.setInfo("sent")
		}
		return m, nil
	case controlResultMsg:
		if msg.err != nil {
			if msg.actionID == m.actionGeneration {
				m.setError(msg.action + " failed: " + msg.err.Error())
			}
			return m, nil
		}
		if msg.submission != "" &&
			m.editorRevision == msg.revision &&
			m.composer.Value() == msg.submission {
			m.composer.SetValue("")
			m.editorRevision++
		}
		if msg.actionID == m.actionGeneration {
			m.setInfo(msg.action + "d " + msg.agent)
		}
		return m, nil
	case promoteResultMsg:
		if msg.actionID != m.actionGeneration {
			return m, nil
		}
		switch {
		case msg.err != nil:
			m.setError("remember failed: " + msg.err.Error())
		case msg.existed && msg.pin:
			m.setInfo("already in memory — pinned")
		case msg.existed:
			m.setInfo("already in memory")
		case msg.pin:
			m.setInfo("remembered + pinned")
		default:
			m.setInfo("remembered")
		}
		return m, nil
	}
	return m, nil
}

func (m *Model) handleKey(msg tea.KeyMsg) (tea.Model, tea.Cmd) {
	switch msg.Type {
	case tea.KeyCtrlC:
		return m, tea.Quit
	case tea.KeyTab:
		m.moveFocus(1)
		return m, nil
	case tea.KeyShiftTab:
		m.moveFocus(-1)
		return m, nil
	case tea.KeyEsc:
		if !m.chatFocused() {
			return m, nil
		}
		if m.accountantOpen {
			m.accountantOpen = false
			m.rebuildStream(false)
			return m, nil
		}
		if m.state.ThreadOpen() {
			m.state.CloseThread()
			m.rebuildStream(false)
			return m, nil
		}
		m.state.ToggleFocus()
		if m.state.Focus() == FocusInput {
			m.composer.Focus()
		} else {
			m.composer.Blur()
		}
		return m, nil
	case tea.KeyEnter:
		if name, ok := m.focusedAgent(); ok {
			m.state.SetCurrentIdentity(name)
			m.focusIndex = len(m.agentNames)
			m.activateComposer()
			return m, nil
		}
		if m.state.Focus() == FocusInput {
			return m, m.handleSubmit()
		}
		return m, nil
	case tea.KeySpace:
		if !m.chatFocused() {
			if cmd, ok := m.togglePauseCmd(); ok {
				return m, cmd
			}
		}
	case tea.KeyEnd:
		if m.accountantOpen {
			m.accountantVP.GotoBottom()
			return m, nil
		}
		if m.chatFocused() && m.state.Focus() != FocusInput {
			m.state.JumpToBottom()
			m.rebuildStream(false)
			return m, nil
		}
	case tea.KeyPgUp:
		if m.accountantOpen {
			m.accountantVP.HalfViewUp()
			return m, nil
		}
		if m.state.Focus() == FocusThread {
			m.threadVP.HalfViewUp()
			return m, nil
		}
		if m.chatFocused() && m.state.Focus() == FocusStream {
			m.vp.HalfViewUp()
			m.state.SetAutoFollow(false)
			return m, nil
		}
	case tea.KeyPgDown:
		if m.accountantOpen {
			m.accountantVP.HalfViewDown()
			return m, nil
		}
		if m.state.Focus() == FocusThread {
			m.threadVP.HalfViewDown()
			return m, nil
		}
		if m.chatFocused() && m.state.Focus() == FocusStream {
			m.vp.HalfViewDown()
			if m.vp.AtBottom() {
				m.state.JumpToBottom()
				m.rebuildStream(false)
			}
			return m, nil
		}
	case tea.KeyUp:
		if m.accountantOpen {
			m.accountantVP.LineUp(1)
			return m, nil
		}
		if m.state.Focus() == FocusThread {
			m.threadVP.LineUp(1)
			return m, nil
		}
		if m.chatFocused() && m.state.Focus() == FocusStream {
			m.state.MoveSelection(-1)
			m.rebuildStream(true)
			return m, nil
		}
	case tea.KeyDown:
		if m.accountantOpen {
			m.accountantVP.LineDown(1)
			return m, nil
		}
		if m.state.Focus() == FocusThread {
			m.threadVP.LineDown(1)
			return m, nil
		}
		if m.chatFocused() && m.state.Focus() == FocusStream {
			m.state.MoveSelection(1)
			m.rebuildStream(true)
			return m, nil
		}
	}

	if msg.Type == tea.KeyRunes && len(msg.Runes) == 1 {
		key := msg.Runes[0]
		if !m.chatFocused() && key == 'q' {
			return m, tea.Quit
		}
		if !m.chatFocused() && key == 'p' {
			if cmd, ok := m.togglePauseCmd(); ok {
				return m, cmd
			}
		}
		if m.accountantOpen {
			switch key {
			case 'a':
				m.accountantOpen = false
				m.rebuildStream(false)
			case 'j':
				m.accountantVP.LineDown(1)
			case 'k':
				m.accountantVP.LineUp(1)
			case 'q':
				return m, tea.Quit
			}
			return m, nil
		}
		if m.chatFocused() && m.state.Focus() == FocusThread {
			switch key {
			case 'j':
				m.threadVP.LineDown(1)
			case 'k':
				m.threadVP.LineUp(1)
			case 'q':
				return m, tea.Quit
			}
			return m, nil
		}
		if m.chatFocused() && m.state.Focus() == FocusStream {
			switch key {
			case 'a':
				m.accountantOpen = true
				if m.state.ThreadOpen() {
					m.state.CloseThread()
				}
				m.rebuildAccountant(false)
				m.accountantVP.GotoBottom()
			case 'j':
				m.state.MoveSelection(1)
				m.rebuildStream(true)
			case 'k':
				m.state.MoveSelection(-1)
				m.rebuildStream(true)
			case 't':
				if m.state.OpenThread() {
					m.rebuildStream(false)
					m.rebuildThread()
				}
			case 'm', 'M':
				if selected, ok := m.state.SelectedMessage(); ok {
					actionID := m.nextAction()
					return m, m.trackCommand(promoteCmd(
						m.svc, actionID, m.cfg.HumanIdentity,
						selected, key == 'M',
					))
				}
			case 'q':
				return m, tea.Quit
			}
			return m, nil
		}
	}

	if m.chatFocused() && m.state.Focus() == FocusInput {
		before := m.composer.Value()
		var cmd tea.Cmd
		m.composer, cmd = m.composer.Update(msg)
		if m.composer.Value() != before {
			m.editorRevision++
		}
		return m, cmd
	}
	return m, nil
}

func (m *Model) handleSubmit() tea.Cmd {
	raw := m.composer.Value()
	text := strings.TrimSpace(raw)
	if text == "" {
		return nil
	}
	if strings.HasPrefix(text, "/") {
		return m.handleCommand(text, raw)
	}
	actionID := m.nextAction()
	return m.trackCommand(sendCmd(
		m.ctx, m.svc, actionID, raw, m.editorRevision,
		m.state.CurrentIdentity(), text, nil,
	))
}

func (m *Model) handleCommand(text, raw string) tea.Cmd {
	fields := strings.Fields(text)
	switch fields[0] {
	case "/quit", "/q":
		return tea.Quit
	case "/pause", "/resume":
		if len(fields) != 2 {
			m.setError("usage: " + fields[0] + " name")
			return nil
		}
		action := strings.TrimPrefix(fields[0], "/")
		actionID := m.nextAction()
		return m.trackCommand(controlCmd(
			m.ctx, m.svc, actionID, action, fields[1], raw, m.editorRevision,
		))
	default:
		m.setError("unknown command: " + fields[0])
		return nil
	}
}

func (m *Model) togglePauseCmd() (tea.Cmd, bool) {
	name, ok := m.focusedAgent()
	if !ok {
		return nil, false
	}
	action := "pause"
	if status, known := m.presence[name]; known && status.State == "paused" {
		action = "resume"
	}
	actionID := m.nextAction()
	return m.trackCommand(controlCmd(
		m.ctx, m.svc, actionID, action, name, "", m.editorRevision,
	)), true
}

func (m *Model) nextAction() uint64 {
	m.actionGeneration++
	m.statusText = ""
	m.statusIsError = false
	return m.actionGeneration
}

func (m *Model) setError(text string) {
	m.statusText, m.statusIsError = text, true
}

func (m *Model) setInfo(text string) {
	m.statusText, m.statusIsError = text, false
}

func (m *Model) chatFocused() bool {
	return m.focusIndex == len(m.agentNames)
}

func (m *Model) focusedAgent() (string, bool) {
	if m.chatFocused() {
		return "", false
	}
	return m.agentNames[m.focusIndex], true
}

func (m *Model) moveFocus(delta int) {
	m.accountantOpen = false
	size := len(m.agentNames) + 1
	m.focusIndex = ((m.focusIndex+delta)%size + size) % size
	if m.chatFocused() {
		m.state.SetCurrentIdentity(m.cfg.HumanIdentity)
		m.activateComposer()
		return
	}
	m.composer.Blur()
}

func (m *Model) activateComposer() {
	m.accountantOpen = false
	if m.state.ThreadOpen() {
		m.state.CloseThread()
	}
	if m.state.Focus() == FocusStream {
		m.state.ToggleFocus()
	}
	m.composer.Focus()
}

func (m *Model) applyActivity(event ActivityEvent) {
	status := m.presence[event.Agent]
	status.Name = event.Agent
	if event.State == "thinking" {
		status.State = "thinking"
	} else if status.State != "paused" && status.State != "offline" {
		status.State = "active"
	}
	status.LastActivityAt = event.At
	m.presence[event.Agent] = status
}

func (m *Model) applyConnection(event ConnectionEvent) (tea.Model, tea.Cmd) {
	switch event.State {
	case "disconnected":
		m.connectionText = "disconnected - reconnecting..."
	case "reconnected":
		m.connectionText = ""
		return m, m.requestStatus()
	case "closed":
		m.connectionText = "connection closed"
	}
	return m, nil
}

func (m *Model) applyStatus(msg statusMsg) (tea.Model, tea.Cmd) {
	if msg.generation != m.statusGeneration {
		return m, nil
	}
	m.statusCancel = nil
	if msg.err != nil {
		m.pollError = "status unavailable: " + msg.err.Error()
		return m, pollTick(m.cfg.PollInterval, msg.generation)
	}
	m.pollError = ""
	m.presence = make(map[string]AgentPresence, len(msg.statuses))
	for _, status := range msg.statuses {
		m.presence[status.Name] = status
	}
	return m, pollTick(m.cfg.PollInterval, msg.generation)
}

func (m *Model) requestStatus() tea.Cmd {
	if m.statusCancel != nil {
		m.statusCancel()
	}
	requestCtx, cancel := context.WithCancel(m.ctx)
	m.statusCancel = cancel
	m.statusGeneration++
	return m.trackCommand(statusCmd(
		requestCtx, m.svc, m.statusGeneration,
	))
}

func (m *Model) trackCommand(cmd tea.Cmd) tea.Cmd {
	if cmd == nil {
		return nil
	}
	return func() tea.Msg {
		m.commandMu.Lock()
		if m.stopping {
			m.commandMu.Unlock()
			return nil
		}
		m.commandWG.Add(1)
		m.commandMu.Unlock()
		defer m.commandWG.Done()
		return cmd()
	}
}

func (m *Model) stopCommands() {
	m.commandMu.Lock()
	m.stopping = true
	if m.statusCancel != nil {
		m.statusCancel()
	}
	m.commandMu.Unlock()
	m.commandWG.Wait()
}

func subscribeAll(
	ctx context.Context,
	svc Service,
	sender msgSender,
) (context.Context, context.CancelFunc, error) {
	subCtx, cancel := context.WithCancel(ctx)
	if err := runSubscriptions(subCtx, svc, sender); err != nil {
		cancel()
		return subCtx, cancel, err
	}
	return subCtx, cancel, nil
}

func uniqueSorted(names []string) []string {
	seen := make(map[string]struct{}, len(names))
	result := make([]string, 0, len(names))
	for _, name := range names {
		name = strings.TrimSpace(name)
		if name == "" {
			continue
		}
		if _, exists := seen[name]; exists {
			continue
		}
		seen[name] = struct{}{}
		result = append(result, name)
	}
	sort.Strings(result)
	return result
}
