package agent

import (
	"context"
	"encoding/json"
	"errors"
	"math/rand"
	"strings"
	"sync"
	"sync/atomic"
	"time"

	"github.com/veridian69/cairn/a2a/internal/model"
	"github.com/veridian69/cairn/a2a/internal/provider"
)

// Silence categories
const (
	SkipOwnMessage     = "own-message"
	SkipCheckpoint     = "checkpoint"
	SkipRateLimit      = "rate-limit"
	SkipResponsiveness = "responsiveness"
	SkipPaused         = "paused"
	SkipEmptyResponse  = "empty-response"
	SkipRefusal        = "refusal"
	SkipProviderError  = "provider-error"
)

type Runtime struct {
	Participant           model.Participant
	Provider              provider.Provider
	Model                 string
	System                string
	Temperature           float64
	Responsiveness        float64
	ContextWindow         int
	MaxPerHour            int
	MaxNominationsPerHour int

	history     *History
	mu          sync.RWMutex
	rng         *rand.Rand
	hourlyCount int
	hourlyReset time.Time
	lastSeenSeq uint64
	lastControl time.Time
	active      atomic.Bool
	beforeCall  func()
	afterCall   func()
	memory      MemoryInjector
	nomCount    int
	nomReset    time.Time
}

type Outcome struct {
	Reply      *model.Message
	Skip       string
	Err        error
	Nomination *Nomination
}

type Nomination struct {
	Content string
}

type MemoryInjector interface {
	InjectionBlock(ctx context.Context, trigger model.Message, visibleIDs, redactedIDs map[string]bool) string
}

type Snapshot struct {
	LastSeenSeq    uint64
	HourlyCount    int
	HourlyResetAt  time.Time
	Responsiveness float64
	Active         bool
}

func NewRuntime(p model.Participant, prov provider.Provider, model, system string, temp, resp float64, ctxWindow, maxPerHour int) *Runtime {
	rt := &Runtime{
		Participant:    p,
		Provider:       prov,
		Model:          model,
		System:         system,
		Temperature:    temp,
		Responsiveness: resp,
		ContextWindow:  ctxWindow,
		MaxPerHour:     maxPerHour,
		history:        NewHistory(ctxWindow * 2),
		rng:            rand.New(rand.NewSource(time.Now().UnixNano())),
	}
	rt.active.Store(true)
	return rt
}

func (rt *Runtime) RestoreCheckpoint(seq uint64, hourlyCount int, hourlyReset time.Time, responsiveness float64) {
	rt.mu.Lock()
	defer rt.mu.Unlock()
	rt.lastSeenSeq = seq
	rt.hourlyCount = hourlyCount
	rt.hourlyReset = hourlyReset
	if responsiveness >= 0 {
		rt.Responsiveness = responsiveness
	}
	rt.active.Store(true)
}

func (rt *Runtime) SetActive(active bool) {
	rt.active.Store(active)
}

func (rt *Runtime) SetProviderHooks(before, after func()) {
	rt.mu.Lock()
	rt.beforeCall = before
	rt.afterCall = after
	rt.mu.Unlock()
}

func (rt *Runtime) SetMemory(memory MemoryInjector) {
	rt.mu.Lock()
	rt.memory = memory
	rt.mu.Unlock()
}

func (rt *Runtime) allowNomination() bool {
	rt.mu.Lock()
	defer rt.mu.Unlock()
	now := time.Now()
	if now.Sub(rt.nomReset) > time.Hour {
		rt.nomCount = 0
		rt.nomReset = now
	}
	if rt.MaxNominationsPerHour > 0 && rt.nomCount >= rt.MaxNominationsPerHour {
		return false
	}
	rt.nomCount++
	return true
}

func (rt *Runtime) MarkCheckpointSaved(seq uint64) {
	rt.mu.Lock()
	rt.lastSeenSeq = seq
	rt.mu.Unlock()
}

func (rt *Runtime) Snapshot() Snapshot {
	rt.mu.RLock()
	defer rt.mu.RUnlock()
	return Snapshot{
		LastSeenSeq:    rt.lastSeenSeq,
		HourlyCount:    rt.hourlyCount,
		HourlyResetAt:  rt.hourlyReset,
		Responsiveness: rt.Responsiveness,
		Active:         rt.active.Load(),
	}
}

func (rt *Runtime) AddToHistory(msg model.Message) {
	rt.history.Add(msg)
}

// parseResponse extracts message content and optional control block.
// If the response is valid JSON with "message" and optional "control" fields,
// it extracts them. Otherwise treats the entire response as plain text.
func (rt *Runtime) parseResponse(raw string) (string, *provider.ControlBlock) {
	raw = strings.TrimSpace(raw)
	content := raw
	var ctrl *provider.ControlBlock

	structuredCandidate := unwrapJSONFence(raw)
	if strings.HasPrefix(structuredCandidate, "{") {
		var structured struct {
			Message *string                `json:"message"`
			Control *provider.ControlBlock `json:"control,omitempty"`
		}
		if err := json.Unmarshal([]byte(structuredCandidate), &structured); err == nil &&
			structured.Message != nil {
			content = strings.TrimSpace(*structured.Message)
			ctrl = structured.Control
		}
	}

	cleanContent, legacyAccountant := extractAccountantFence(content)
	if legacyAccountant != nil {
		if ctrl == nil {
			ctrl = &provider.ControlBlock{}
		}
		if ctrl.Accountant == nil {
			ctrl.Accountant = legacyAccountant
		}
	}

	return cleanContent, ctrl
}

func unwrapJSONFence(raw string) string {
	const marker = "```json"
	if !strings.HasPrefix(raw, marker) {
		return raw
	}
	tail := strings.TrimSpace(raw[len(marker):])
	if !strings.HasSuffix(tail, "```") {
		return raw
	}
	return strings.TrimSpace(strings.TrimSuffix(tail, "```"))
}

func extractAccountantFence(content string) (string, *model.AccountantRecord) {
	const marker = "```accountant"
	start := strings.LastIndex(content, marker)
	if start < 0 {
		return content, nil
	}

	tail := strings.TrimSpace(content[start+len(marker):])
	if !strings.HasSuffix(tail, "```") {
		return content, nil
	}
	payload := strings.TrimSpace(strings.TrimSuffix(tail, "```"))
	var record model.AccountantRecord
	if err := json.Unmarshal([]byte(payload), &record); err != nil ||
		strings.TrimSpace(record.Idea) == "" {
		return content, nil
	}
	return strings.TrimSpace(content[:start]), &record
}

// applyControl applies a control block with clamping and rate limiting.
func (rt *Runtime) applyControl(ctrl *provider.ControlBlock) {
	if ctrl == nil {
		return
	}
	if ctrl.Responsiveness != nil {
		rt.mu.Lock()
		defer rt.mu.Unlock()

		val := *ctrl.Responsiveness
		// Clamp to [0.05, 1.0]
		if val < 0.05 {
			val = 0.05
		}
		if val > 1.0 {
			val = 1.0
		}

		if !rt.lastControl.IsZero() && time.Since(rt.lastControl) < time.Hour {
			upper := rt.Responsiveness + 0.3
			lower := rt.Responsiveness - 0.3
			if val > upper {
				val = upper
			}
			if val < lower {
				val = lower
			}
			if val < 0.05 {
				val = 0.05
			}
			if val > 1.0 {
				val = 1.0
			}
		}

		rt.Responsiveness = val
		rt.lastControl = time.Now()
	}
}

// HandleMessage processes a message. A nomination is returned only alongside
// a publishable reply.
func (rt *Runtime) HandleMessage(ctx context.Context, msg model.Message, seq uint64, redactedIDs map[string]bool) Outcome {
	// Own message
	if msg.AuthorID == rt.Participant.ID {
		return Outcome{Skip: SkipOwnMessage}
	}

	rt.mu.RLock()
	lastSeen := rt.lastSeenSeq
	rt.mu.RUnlock()
	if seq <= lastSeen {
		return Outcome{Skip: SkipCheckpoint}
	}

	// Paused
	if !rt.active.Load() {
		return Outcome{Skip: SkipPaused}
	}

	// Rate limit
	rt.mu.Lock()
	now := time.Now()
	if now.Sub(rt.hourlyReset) > time.Hour {
		rt.hourlyCount = 0
		rt.hourlyReset = now
	}
	if rt.MaxPerHour > 0 && rt.hourlyCount >= rt.MaxPerHour {
		rt.mu.Unlock()
		return Outcome{Skip: SkipRateLimit}
	}
	resp := rt.Responsiveness
	rt.mu.Unlock()

	// Responsiveness gate
	if resp <= 0 {
		return Outcome{Skip: SkipResponsiveness}
	}
	rt.mu.Lock()
	roll := rt.rng.Float64()
	rt.mu.Unlock()
	if resp < 1.0 && roll > resp {
		return Outcome{Skip: SkipResponsiveness}
	}

	// Build context — reply-chain-aware
	allMsgs := rt.history.All()
	contextMsgs, visibleIDs := AssembleContext(rt.Participant, msg, allMsgs, 8, 10, 3, redactedIDs)
	rt.mu.RLock()
	memory := rt.memory
	rt.mu.RUnlock()
	if memory != nil {
		if block := memory.InjectionBlock(ctx, msg, visibleIDs, redactedIDs); block != "" {
			contextMsgs = append([]provider.ChatMessage{{Role: "user", Content: block}}, contextMsgs...)
		}
	}

	rt.mu.RLock()
	beforeCall := rt.beforeCall
	afterCall := rt.afterCall
	rt.mu.RUnlock()
	if beforeCall != nil {
		beforeCall()
	}
	if afterCall != nil {
		defer afterCall()
	}

	// Call LLM with one retry for transient failures
	result, err := rt.Provider.Complete(ctx, provider.CompletionRequest{
		System:      rt.System,
		Messages:    contextMsgs,
		Model:       rt.Model,
		Temperature: rt.Temperature,
	})

	if err != nil && provider.IsTransient(err) {
		// One bounded retry after short delay
		select {
		case <-time.After(5 * time.Second):
		case <-ctx.Done():
			return Outcome{Skip: SkipProviderError, Err: ctx.Err()}
		}
		result, err = rt.Provider.Complete(ctx, provider.CompletionRequest{
			System:      rt.System,
			Messages:    contextMsgs,
			Model:       rt.Model,
			Temperature: rt.Temperature,
		})
	}

	if err != nil {
		if errors.Is(err, provider.ErrRefused) {
			return Outcome{Skip: SkipRefusal}
		}
		return Outcome{Skip: SkipProviderError, Err: err}
	}

	content, ctrl := rt.parseResponse(result.Content)
	rt.applyControl(ctrl)

	if strings.TrimSpace(content) == "" {
		return Outcome{Skip: SkipEmptyResponse}
	}

	// Increment rate counter
	rt.mu.Lock()
	rt.hourlyCount++
	rt.mu.Unlock()

	replyTo := msg.ID
	reply := model.NewMessage(rt.Participant, content, &replyTo)
	if ctrl != nil {
		reply.Accountant = ctrl.Accountant
	}
	reply.Metadata["model"] = rt.Model
	reply.Metadata["provider"] = rt.Provider.Name()
	if result.TokensIn > 0 {
		reply.Metadata["tokens_in"] = result.TokensIn
	}
	if result.TokensOut > 0 {
		reply.Metadata["tokens_out"] = result.TokensOut
	}

	outcome := Outcome{Reply: &reply}
	if ctrl != nil && ctrl.Remember != nil && rt.allowNomination() {
		outcome.Nomination = &Nomination{Content: *ctrl.Remember}
	}
	return outcome
}
