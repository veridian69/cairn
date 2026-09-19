package chat

import (
	"sort"

	"github.com/veridian69/cairn/a2a/internal/model"
)

type FocusPane string

const (
	FocusInput  FocusPane = "input"
	FocusStream FocusPane = "stream"
	FocusThread FocusPane = "thread"
)

type ThreadSection string

const (
	ThreadContext ThreadSection = "context"
	ThreadReplies ThreadSection = "replies"
)

type ThreadItem struct {
	Message   model.Message
	Depth     int
	Section   ThreadSection
	IsMissing bool
	MissingID string
}

type ThreadView struct {
	SelectedID string
	Context    []ThreadItem
	Replies    []ThreadItem
}

type State struct {
	messagesByID     map[string]model.Message
	childrenByParent map[string][]string
	orderedIDs       []string
	selected         int
	autoFollow       bool
	unseenCount      int
	focus            FocusPane
	threadOpen       bool
	currentIdentity  string
}

func NewState(events []StreamEvent, identity string) *State {
	st := &State{
		messagesByID:     make(map[string]model.Message, len(events)),
		childrenByParent: make(map[string][]string),
		selected:         -1,
		autoFollow:       true,
		focus:            FocusInput,
		currentIdentity:  identity,
	}
	for _, event := range events {
		st.Append(event)
	}
	if len(st.orderedIDs) > 0 {
		st.selected = len(st.orderedIDs) - 1
	}
	st.unseenCount = 0
	st.autoFollow = true
	return st
}

func (s *State) Append(event StreamEvent) {
	msg := event.Message
	if _, exists := s.messagesByID[msg.ID]; exists {
		s.messagesByID[msg.ID] = msg
		return
	}
	s.messagesByID[msg.ID] = msg
	s.orderedIDs = append(s.orderedIDs, msg.ID)
	if msg.ReplyTo != nil {
		parentID := *msg.ReplyTo
		s.childrenByParent[parentID] = append(s.childrenByParent[parentID], msg.ID)
	}
	if len(s.orderedIDs) == 1 {
		s.selected = 0
		s.autoFollow = true
		return
	}
	if s.autoFollow {
		s.selected = len(s.orderedIDs) - 1
		s.unseenCount = 0
		return
	}
	s.unseenCount++
}

func (s *State) OrderedIDs() []string {
	return append([]string(nil), s.orderedIDs...)
}

func (s *State) Message(id string) (model.Message, bool) {
	msg, ok := s.messagesByID[id]
	return msg, ok
}

func (s *State) SelectedIndex() int {
	return s.selected
}

func (s *State) SelectedMessage() (model.Message, bool) {
	if s.selected < 0 || s.selected >= len(s.orderedIDs) {
		return model.Message{}, false
	}
	return s.messagesByID[s.orderedIDs[s.selected]], true
}

func (s *State) AutoFollow() bool {
	return s.autoFollow
}

func (s *State) UnseenCount() int {
	return s.unseenCount
}

func (s *State) Focus() FocusPane {
	return s.focus
}

func (s *State) ThreadOpen() bool {
	return s.threadOpen
}

func (s *State) CurrentIdentity() string {
	return s.currentIdentity
}

func (s *State) SetCurrentIdentity(identity string) {
	s.currentIdentity = identity
}

func (s *State) ToggleFocus() {
	switch s.focus {
	case FocusInput:
		s.focus = FocusStream
	case FocusStream:
		s.focus = FocusInput
	case FocusThread:
		s.focus = FocusStream
	}
}

func (s *State) OpenThread() bool {
	if _, ok := s.SelectedMessage(); !ok {
		return false
	}
	s.threadOpen = true
	s.focus = FocusThread
	return true
}

func (s *State) CloseThread() {
	s.threadOpen = false
	if s.focus == FocusThread {
		s.focus = FocusStream
	}
}

func (s *State) MoveSelection(delta int) bool {
	if len(s.orderedIDs) == 0 || delta == 0 {
		return false
	}
	if s.selected < 0 {
		s.selected = 0
	}
	next := s.selected + delta
	if next < 0 {
		next = 0
	}
	if next >= len(s.orderedIDs) {
		next = len(s.orderedIDs) - 1
	}
	if next == s.selected {
		return false
	}
	s.selected = next
	s.autoFollow = s.selected == len(s.orderedIDs)-1
	if s.autoFollow {
		s.unseenCount = 0
	}
	return true
}

func (s *State) SetSelectedIndex(index int) bool {
	if len(s.orderedIDs) == 0 {
		s.selected = -1
		s.autoFollow = true
		s.unseenCount = 0
		return false
	}
	if index < 0 {
		index = 0
	}
	if index >= len(s.orderedIDs) {
		index = len(s.orderedIDs) - 1
	}
	changed := s.selected != index
	s.selected = index
	s.autoFollow = s.selected == len(s.orderedIDs)-1
	if s.autoFollow {
		s.unseenCount = 0
	}
	return changed
}

func (s *State) JumpToBottom() bool {
	if len(s.orderedIDs) == 0 {
		return false
	}
	changed := s.selected != len(s.orderedIDs)-1 || !s.autoFollow || s.unseenCount != 0
	s.selected = len(s.orderedIDs) - 1
	s.autoFollow = true
	s.unseenCount = 0
	return changed
}

func (s *State) SetAutoFollow(auto bool) {
	s.autoFollow = auto
	if auto && len(s.orderedIDs) > 0 {
		s.selected = len(s.orderedIDs) - 1
		s.unseenCount = 0
	}
}

func (s *State) BuildThread(maxDepth int) (ThreadView, bool) {
	selected, ok := s.SelectedMessage()
	if !ok {
		return ThreadView{}, false
	}
	view := ThreadView{SelectedID: selected.ID}

	context, missingID := s.buildContext(selected, maxDepth)
	view.Context = context
	if missingID != "" {
		view.Context = append(view.Context, ThreadItem{
			Section:   ThreadContext,
			IsMissing: true,
			MissingID: missingID,
		})
	}
	view.Replies = s.buildReplies(selected.ID)
	return view, true
}

func (s *State) buildContext(selected model.Message, maxDepth int) ([]ThreadItem, string) {
	var chain []model.Message
	current := selected
	chain = append(chain, current)
	for depth := 0; depth < maxDepth; depth++ {
		if current.ReplyTo == nil {
			break
		}
		parentID := *current.ReplyTo
		parent, ok := s.messagesByID[parentID]
		if !ok {
			return reverseContext(chain), parentID
		}
		current = parent
		chain = append(chain, current)
	}
	return reverseContext(chain), ""
}

func reverseContext(chain []model.Message) []ThreadItem {
	items := make([]ThreadItem, 0, len(chain))
	depth := 0
	for i := len(chain) - 1; i >= 0; i-- {
		items = append(items, ThreadItem{
			Message: chain[i],
			Depth:   depth,
			Section: ThreadContext,
		})
		depth++
	}
	return items
}

func (s *State) buildReplies(rootID string) []ThreadItem {
	var items []ThreadItem
	var walk func(parentID string, depth int)
	walk = func(parentID string, depth int) {
		children := append([]string(nil), s.childrenByParent[parentID]...)
		sort.Slice(children, func(i, j int) bool {
			left := s.messagesByID[children[i]]
			right := s.messagesByID[children[j]]
			if left.CreatedAt.Equal(right.CreatedAt) {
				return left.ID < right.ID
			}
			return left.CreatedAt.Before(right.CreatedAt)
		})
		for _, childID := range children {
			child, ok := s.messagesByID[childID]
			if !ok {
				continue
			}
			items = append(items, ThreadItem{
				Message: child,
				Depth:   depth,
				Section: ThreadReplies,
			})
			walk(childID, depth+1)
		}
	}
	walk(rootID, 1)
	return items
}
