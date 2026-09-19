package agent

import (
	"sync"

	"github.com/veridian69/cairn/a2a/internal/model"
	"github.com/veridian69/cairn/a2a/internal/provider"
)

type History struct {
	mu       sync.RWMutex
	messages []model.Message
	max      int
}

func NewHistory(max int) *History {
	return &History{max: max}
}

func (h *History) Add(msg model.Message) {
	h.mu.Lock()
	defer h.mu.Unlock()
	h.messages = append(h.messages, msg)
	if len(h.messages) > h.max*2 {
		h.messages = h.messages[len(h.messages)-h.max:]
	}
}

// All returns a snapshot copy of all messages in the history.
func (h *History) All() []model.Message {
	h.mu.RLock()
	defer h.mu.RUnlock()
	result := make([]model.Message, len(h.messages))
	copy(result, h.messages)
	return result
}

// AssembleContext builds the ordered context per the spec:
// [ancestor chain oldest→newest] + [same-author recent] + [global tail oldest→newest]
// With deduplication: ancestor > same-author > global tail.
func AssembleContext(
	self model.Participant,
	trigger model.Message,
	allMessages []model.Message,
	maxAncestorDepth int,
	maxGlobalTail int,
	maxSameAuthor int,
	redactedIDs map[string]bool,
) ([]provider.ChatMessage, map[string]bool) {
	if redactedIDs == nil {
		redactedIDs = make(map[string]bool)
	}

	// Build message index for parent walking
	byID := make(map[string]model.Message)
	for _, m := range allMessages {
		byID[m.ID] = m
	}

	// 1. Ancestor chain: walk ReplyTo backwards
	var ancestors []model.Message
	visited := make(map[string]bool)
	current := trigger
	for i := 0; i < maxAncestorDepth; i++ {
		if redactedIDs[current.ID] || visited[current.ID] {
			break
		}
		visited[current.ID] = true
		ancestors = append([]model.Message{current}, ancestors...)
		if current.ReplyTo == nil {
			break
		}
		parent, ok := byID[*current.ReplyTo]
		if !ok {
			break
		}
		current = parent
	}

	// Track which IDs are in ancestor chain
	ancestorIDs := make(map[string]bool)
	for _, m := range ancestors {
		ancestorIDs[m.ID] = true
	}

	// 2. Same-author recent (not in ancestors, not redacted)
	var sameAuthor []model.Message
	for i := len(allMessages) - 1; i >= 0 && len(sameAuthor) < maxSameAuthor; i-- {
		m := allMessages[i]
		if m.AuthorID == self.ID && !ancestorIDs[m.ID] && !redactedIDs[m.ID] {
			sameAuthor = append([]model.Message{m}, sameAuthor...)
		}
	}

	sameAuthorIDs := make(map[string]bool)
	for _, m := range sameAuthor {
		sameAuthorIDs[m.ID] = true
	}

	// 3. Global tail (not in ancestors or same-author, not redacted)
	var globalTail []model.Message
	start := 0
	if len(allMessages) > maxGlobalTail {
		start = len(allMessages) - maxGlobalTail
	}
	for _, m := range allMessages[start:] {
		if !ancestorIDs[m.ID] && !sameAuthorIDs[m.ID] && !redactedIDs[m.ID] {
			globalTail = append(globalTail, m)
		}
	}

	// 4. Assemble final ordered list
	var result []provider.ChatMessage
	visible := make(map[string]bool)
	for _, m := range ancestors {
		result = append(result, toChat(m))
		visible[m.ID] = true
	}
	for _, m := range sameAuthor {
		result = append(result, toChat(m))
		visible[m.ID] = true
	}
	for _, m := range globalTail {
		result = append(result, toChat(m))
		visible[m.ID] = true
	}

	return result, visible
}

func toChat(m model.Message) provider.ChatMessage {
	return provider.ChatMessage{
		Role:    "user",
		Content: m.AuthorName + ": " + m.Content,
	}
}
