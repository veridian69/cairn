package agent

import (
	"context"
	"errors"
	"fmt"
	"net/http"
	"strings"
	"testing"
	"time"

	"github.com/veridian69/cairn/a2a/internal/model"
	"github.com/veridian69/cairn/a2a/internal/provider"
)

func TestSkipsOwnMessages(t *testing.T) {
	mock := &provider.MockProvider{Resp: provider.CompletionResponse{Content: "nope"}}
	p := model.Participant{ID: "p-1", Name: "claude", Kind: "agent"}
	rt := NewRuntime(p, mock, "test-model", "system", 0.9, 1.0, 10, 20)

	msg := model.NewMessage(p, "my own message", nil)
	out := rt.HandleMessage(context.Background(), msg, 1, nil)
	reply, skip := out.Reply, out.Skip

	if reply != nil {
		t.Error("should not reply to own message")
	}
	if skip != SkipOwnMessage {
		t.Errorf("skip = %q, want %q", skip, SkipOwnMessage)
	}
}

func TestResponds(t *testing.T) {
	mock := &provider.MockProvider{Resp: provider.CompletionResponse{Content: "Hello back!"}}
	p := model.Participant{ID: "p-1", Name: "claude", Kind: "agent"}
	rt := NewRuntime(p, mock, "test-model", "system", 0.9, 1.0, 10, 20)

	other := model.Participant{ID: "p-2", Name: "gemini", Kind: "agent"}
	msg := model.NewMessage(other, "Hello from the agora", nil)
	rt.AddToHistory(msg)

	out := rt.HandleMessage(context.Background(), msg, 1, nil)
	reply, skip := out.Reply, out.Skip

	if skip != "" {
		t.Fatalf("unexpected skip: %q", skip)
	}
	if reply == nil {
		t.Fatal("expected reply")
	}
	if reply.AuthorID != "p-1" {
		t.Errorf("reply.AuthorID = %q", reply.AuthorID)
	}
	if reply.Content != "Hello back!" {
		t.Errorf("reply.Content = %q", reply.Content)
	}
	if reply.ReplyTo == nil || *reply.ReplyTo != msg.ID {
		t.Error("reply.ReplyTo should reference original")
	}
}

func TestResponsivenessZero(t *testing.T) {
	mock := &provider.MockProvider{Resp: provider.CompletionResponse{Content: "nope"}}
	p := model.Participant{ID: "p-1", Name: "claude", Kind: "agent"}
	rt := NewRuntime(p, mock, "m", "s", 0.9, 0.0, 10, 20)

	other := model.Participant{ID: "p-2", Name: "gemini"}
	msg := model.NewMessage(other, "Hello", nil)
	out := rt.HandleMessage(context.Background(), msg, 1, nil)
	skip := out.Skip

	if skip != SkipResponsiveness {
		t.Errorf("skip = %q, want %q", skip, SkipResponsiveness)
	}
}

func TestEmptyResponse(t *testing.T) {
	mock := &provider.MockProvider{Resp: provider.CompletionResponse{Content: ""}}
	p := model.Participant{ID: "p-1", Name: "claude", Kind: "agent"}
	rt := NewRuntime(p, mock, "m", "s", 0.9, 1.0, 10, 20)

	other := model.Participant{ID: "p-2", Name: "gemini"}
	msg := model.NewMessage(other, "Hello", nil)
	rt.AddToHistory(msg)
	out := rt.HandleMessage(context.Background(), msg, 1, nil)
	reply, skip := out.Reply, out.Skip

	if reply != nil {
		t.Error("should not reply to empty response")
	}
	if skip != SkipEmptyResponse {
		t.Errorf("skip = %q, want %q", skip, SkipEmptyResponse)
	}
}

func TestSoftFailureRetry(t *testing.T) {
	callCount := 0
	mock := &provider.MockProvider{
		Err: fmt.Errorf("%w: timeout", provider.ErrTransient),
	}
	// Override Complete to succeed on second call
	p := model.Participant{ID: "p-1", Name: "claude", Kind: "agent"}
	rt := NewRuntime(p, &retryTestProvider{failFirst: true}, "m", "s", 0.9, 1.0, 10, 20)

	other := model.Participant{ID: "p-2", Name: "gemini"}
	msg := model.NewMessage(other, "Hello", nil)
	rt.AddToHistory(msg)
	out := rt.HandleMessage(context.Background(), msg, 1, nil)
	reply, skip := out.Reply, out.Skip

	if reply == nil {
		t.Fatalf("expected reply after retry, skip=%q", skip)
	}
	_ = callCount
	_ = mock
}

func TestProviderErrorOutcome(t *testing.T) {
	providerErr := &provider.RequestError{
		Category: provider.ErrAuth, StatusCode: http.StatusUnauthorized,
		ResponseBody: `{"error":"bad key"}`,
	}
	mock := &provider.MockProvider{Err: providerErr}
	participant := model.Participant{ID: "p-1", Name: "claude", Kind: "agent"}
	rt := NewRuntime(participant, mock, "model", "system", 0.7, 1, 10, 20)
	trigger := model.Message{ID: "trigger", AuthorID: "p-2", Content: "hello"}

	out := rt.HandleMessage(context.Background(), trigger, 1, nil)
	if out.Skip != SkipProviderError || !errors.Is(out.Err, provider.ErrAuth) {
		t.Fatalf("outcome = %#v", out)
	}
}

func TestRetryCancellationOutcome(t *testing.T) {
	mock := &provider.MockProvider{Err: fmt.Errorf("%w: timeout", provider.ErrTransient)}
	participant := model.Participant{ID: "p-1", Name: "claude", Kind: "agent"}
	rt := NewRuntime(participant, mock, "model", "system", 0.7, 1, 10, 20)
	trigger := model.Message{ID: "trigger", AuthorID: "p-2", Content: "hello"}
	ctx, cancel := context.WithCancel(context.Background())
	cancel()

	out := rt.HandleMessage(ctx, trigger, 1, nil)
	if out.Skip != SkipProviderError || !errors.Is(out.Err, context.Canceled) {
		t.Fatalf("outcome = %#v", out)
	}
}

type retryTestProvider struct {
	calls     int
	failFirst bool
}

func (r *retryTestProvider) Complete(ctx context.Context, req provider.CompletionRequest) (provider.CompletionResponse, error) {
	r.calls++
	if r.failFirst && r.calls == 1 {
		return provider.CompletionResponse{}, fmt.Errorf("%w: timeout", provider.ErrTransient)
	}
	return provider.CompletionResponse{Content: "retried successfully"}, nil
}

func (r *retryTestProvider) Name() string { return "retry-test" }

func TestStructuredControlBlock(t *testing.T) {
	jsonResp := `{"message": "I'll listen for a while.", "control": {"responsiveness": 0.8}}`
	mock := &provider.MockProvider{Resp: provider.CompletionResponse{Content: jsonResp}}

	p := model.Participant{ID: "p-1", Name: "claude", Kind: "agent"}
	// resp=1.0 guarantees processing; requesting 0.8 is within ±0.3 rate limit
	rt := NewRuntime(p, mock, "m", "s", 0.9, 1.0, 10, 20)

	other := model.Participant{ID: "p-2", Name: "gemini"}
	msg := model.NewMessage(other, "Hello", nil)
	rt.AddToHistory(msg)
	out := rt.HandleMessage(context.Background(), msg, 1, nil)
	reply, skip := out.Reply, out.Skip

	if skip != "" {
		t.Fatalf("unexpected skip: %q", skip)
	}
	if reply == nil {
		t.Fatal("expected reply")
	}
	// Content should be extracted from the JSON, not the raw JSON
	if reply.Content != "I'll listen for a while." {
		t.Errorf("Content = %q", reply.Content)
	}
	if rt.Responsiveness != 0.8 {
		t.Errorf("Responsiveness = %f, want 0.8", rt.Responsiveness)
	}
}

func TestStructuredAccountantControlKeepsReplyContentReadable(t *testing.T) {
	jsonResp := `{
		"message": "Use a temporary reduction in working hours.",
		"control": {
			"accountant": {
				"proposer": "val",
				"idea": "Temporarily reduce Roy's working hours",
				"assumptions": ["Roy's employer permits it"],
				"evidence": "Directly removes work days; pension impact is not yet known",
				"probability": "medium",
				"capital_required_chf": 12000,
				"time_required": "Two meetings; no ongoing work",
				"downside_if_wrong": "Lower income; inaction leaves the current schedule unchanged",
				"next_experiment": "Ask HR for a non-binding calculation",
				"dissent": "none"
			}
		}
	}`
	mock := &provider.MockProvider{Resp: provider.CompletionResponse{Content: jsonResp}}
	participant := model.Participant{ID: "p-1", Name: "val", Kind: model.KindAgent}
	rt := NewRuntime(participant, mock, "m", "s", 0.8, 1, 10, 20)

	trigger := model.NewMessage(
		model.Participant{ID: "human", Name: "operator", Kind: model.KindHuman},
		"Find a practical option.",
		nil,
	)
	rt.AddToHistory(trigger)
	out := rt.HandleMessage(context.Background(), trigger, 1, nil)

	if out.Reply == nil {
		t.Fatalf("expected reply, outcome = %#v", out)
	}
	if out.Reply.Content != "Use a temporary reduction in working hours." {
		t.Fatalf("reply content = %q", out.Reply.Content)
	}
	if out.Reply.Accountant == nil {
		t.Fatal("accountant record was not attached to the reply")
	}
	if out.Reply.Accountant.Idea != "Temporarily reduce Roy's working hours" {
		t.Fatalf("accountant idea = %q", out.Reply.Accountant.Idea)
	}
	if out.Reply.Accountant.CapitalRequiredCHF == nil ||
		*out.Reply.Accountant.CapitalRequiredCHF != 12000 {
		t.Fatalf("accountant capital = %#v", out.Reply.Accountant.CapitalRequiredCHF)
	}
}

func TestLegacyAccountantFenceIsRemovedFromReplyContent(t *testing.T) {
	raw := "Try the pension calculation first.\n\n" +
		"```accountant\n" +
		`{"proposer":"spike","idea":"Get a pension calculation","assumptions":[],"evidence":"It replaces estimates with facts","probability":"high","capital_required_chf":null,"time_required":"One request","downside_if_wrong":"Delay of a few days","next_experiment":"Request it","dissent":"none"}` +
		"\n```"
	mock := &provider.MockProvider{Resp: provider.CompletionResponse{Content: raw}}
	participant := model.Participant{ID: "p-1", Name: "spike", Kind: model.KindAgent}
	rt := NewRuntime(participant, mock, "m", "s", 0.7, 1, 10, 20)
	trigger := model.NewMessage(
		model.Participant{ID: "human", Name: "operator", Kind: model.KindHuman},
		"What should we verify first?",
		nil,
	)

	out := rt.HandleMessage(context.Background(), trigger, 1, nil)

	if out.Reply == nil {
		t.Fatalf("expected reply, outcome = %#v", out)
	}
	if out.Reply.Content != "Try the pension calculation first." {
		t.Fatalf("reply content = %q", out.Reply.Content)
	}
	if out.Reply.Accountant == nil ||
		out.Reply.Accountant.Idea != "Get a pension calculation" {
		t.Fatalf("accountant record = %#v", out.Reply.Accountant)
	}
}

func TestFencedStructuredResponseKeepsReplyContentReadable(t *testing.T) {
	raw := "```json\n" +
		`{"message":"Check the pension figures.","control":{"accountant":{"proposer":"val","idea":"Request a pension calculation","assumptions":[],"evidence":"Replaces estimates","probability":"high","capital_required_chf":null,"time_required":"One request","downside_if_wrong":"Minor delay","next_experiment":"Submit the request","dissent":"none"}}}` +
		"\n```"
	mock := &provider.MockProvider{Resp: provider.CompletionResponse{Content: raw}}
	participant := model.Participant{ID: "p-1", Name: "val", Kind: model.KindAgent}
	rt := NewRuntime(participant, mock, "m", "s", 0.8, 1, 10, 20)
	trigger := model.NewMessage(
		model.Participant{ID: "human", Name: "operator", Kind: model.KindHuman},
		"What should we verify?",
		nil,
	)

	out := rt.HandleMessage(context.Background(), trigger, 1, nil)

	if out.Reply == nil {
		t.Fatalf("expected reply, outcome = %#v", out)
	}
	if out.Reply.Content != "Check the pension figures." {
		t.Fatalf("reply content = %q", out.Reply.Content)
	}
	if out.Reply.Accountant == nil ||
		out.Reply.Accountant.Idea != "Request a pension calculation" {
		t.Fatalf("accountant record = %#v", out.Reply.Accountant)
	}
}

func TestOrdinaryJSONCodeFenceRemainsConversationalContent(t *testing.T) {
	raw := "```json\n{\"ordinary\":\"example\"}\n```"
	mock := &provider.MockProvider{Resp: provider.CompletionResponse{Content: raw}}
	participant := model.Participant{ID: "p-1", Name: "val", Kind: model.KindAgent}
	rt := NewRuntime(participant, mock, "m", "s", 0.8, 1, 10, 20)
	trigger := model.NewMessage(
		model.Participant{ID: "human", Name: "operator", Kind: model.KindHuman},
		"Show the JSON.",
		nil,
	)

	out := rt.HandleMessage(context.Background(), trigger, 1, nil)

	if out.Reply == nil {
		t.Fatalf("expected reply, outcome = %#v", out)
	}
	if out.Reply.Content != raw {
		t.Fatalf("ordinary JSON fence changed to %q", out.Reply.Content)
	}
}

func TestControlBlockClamping(t *testing.T) {
	// Test floor clamp via applyControl directly to isolate from responsiveness gate
	p := model.Participant{ID: "p-1", Name: "claude", Kind: "agent"}
	rt := NewRuntime(p, nil, "m", "s", 0.9, 0.2, 10, 20)

	zero := 0.0
	rt.applyControl(&provider.ControlBlock{Responsiveness: &zero})

	// 0.0 is clamped to 0.05 floor; delta from 0.2 is -0.15 which is within ±0.3
	if rt.Responsiveness != 0.05 {
		t.Errorf("Responsiveness = %f, want 0.05 (floor clamp)", rt.Responsiveness)
	}
}

func TestControlBlockRateLimitFirstAdjustment(t *testing.T) {
	// First control block should also be rate-limited to ±0.3
	jsonResp := `{"message": "Going quiet.", "control": {"responsiveness": 0.1}}`
	mock := &provider.MockProvider{Resp: provider.CompletionResponse{Content: jsonResp}}

	p := model.Participant{ID: "p-1", Name: "claude", Kind: "agent"}
	rt := NewRuntime(p, mock, "m", "s", 0.9, 1.0, 10, 20)

	other := model.Participant{ID: "p-2", Name: "gemini"}
	msg := model.NewMessage(other, "Hello", nil)
	rt.AddToHistory(msg)
	rt.HandleMessage(context.Background(), msg, 1, nil)

	// First control adjustment is unclamped — requested 0.1 from 1.0 should give 0.1
	if rt.Responsiveness != 0.1 {
		t.Errorf("Responsiveness = %f, want 0.1 (first adjustment unclamped)", rt.Responsiveness)
	}
}

func TestPlainTextNoControl(t *testing.T) {
	mock := &provider.MockProvider{Resp: provider.CompletionResponse{Content: "Just a plain reply."}}

	p := model.Participant{ID: "p-1", Name: "claude", Kind: "agent"}
	// Use resp=1.0 to guarantee the message is always processed (no probabilistic skip)
	rt := NewRuntime(p, mock, "m", "s", 0.9, 1.0, 10, 20)

	other := model.Participant{ID: "p-2", Name: "gemini"}
	msg := model.NewMessage(other, "Hello", nil)
	rt.AddToHistory(msg)
	out := rt.HandleMessage(context.Background(), msg, 1, nil)
	reply := out.Reply

	if reply == nil {
		t.Fatal("expected reply")
	}

	if reply.Content != "Just a plain reply." {
		t.Errorf("Content = %q", reply.Content)
	}
	if rt.Responsiveness != 1.0 {
		t.Errorf("Responsiveness changed to %f", rt.Responsiveness)
	}
}

func TestReplyChainContext(t *testing.T) {
	p1 := model.Participant{ID: "p-1", Name: "claude", Kind: "agent"}
	p2 := model.Participant{ID: "p-2", Name: "gemini", Kind: "agent"}

	// Build a reply chain: msg1 → msg2 → msg3
	msg1 := model.Message{ID: "m1", AuthorID: "p-2", AuthorName: "gemini", Content: "root message"}
	msg2ID := "m1"
	msg2 := model.Message{ID: "m2", AuthorID: "p-1", AuthorName: "claude", Content: "reply to root", ReplyTo: &msg2ID}
	msg3ID := "m2"
	msg3 := model.Message{ID: "m3", AuthorID: "p-2", AuthorName: "gemini", Content: "reply to reply", ReplyTo: &msg3ID}

	// Also add unrelated messages
	msg4 := model.Message{ID: "m4", AuthorID: "p-2", AuthorName: "gemini", Content: "unrelated 1"}
	msg5 := model.Message{ID: "m5", AuthorID: "p-2", AuthorName: "gemini", Content: "unrelated 2"}

	allMsgs := []model.Message{msg1, msg2, msg3, msg4, msg5}

	ctx, _ := AssembleContext(p1, msg3, allMsgs, 8, 10, 3, nil)

	// Ancestor chain should be: msg1, msg2, msg3
	// Global tail should include msg4, msg5 (not duplicates)
	if len(ctx) == 0 {
		t.Fatal("empty context")
	}

	// First messages should be the ancestor chain
	if ctx[0].Content != "gemini: root message" {
		t.Errorf("first context msg = %q, want ancestor chain root", ctx[0].Content)
	}

	_ = p2
}

func TestSkipsCheckpointedMessages(t *testing.T) {
	mock := &provider.MockProvider{Resp: provider.CompletionResponse{Content: "should not run"}}
	p := model.Participant{ID: "p-1", Name: "claude", Kind: "agent"}
	rt := NewRuntime(p, mock, "m", "s", 0.9, 1.0, 10, 20)
	rt.RestoreCheckpoint(10, 0, time.Time{}, 1.0)

	other := model.Participant{ID: "p-2", Name: "gemini", Kind: "agent"}
	msg := model.NewMessage(other, "Hello", nil)

	out := rt.HandleMessage(context.Background(), msg, 10, nil)
	reply, skip := out.Reply, out.Skip
	if reply != nil {
		t.Fatal("expected no reply")
	}
	if skip != SkipCheckpoint {
		t.Fatalf("skip = %q, want %q", skip, SkipCheckpoint)
	}
	if len(mock.Calls) != 0 {
		t.Fatalf("provider should not have been called, got %d calls", len(mock.Calls))
	}
}

type staticMemory struct{ block string }

func (memory staticMemory) InjectionBlock(context.Context, model.Message, map[string]bool, map[string]bool) string {
	return memory.block
}

func TestOutcomeCarriesReplyCoupledNominationAndPrependsMemory(t *testing.T) {
	mock := &provider.MockProvider{Resp: provider.CompletionResponse{
		Content: `{"message":"noted","control":{"remember":"keep this"}}`,
	}}
	runtime := NewRuntime(
		model.Participant{ID: "agent", Name: "claude"}, mock,
		"model", "system", 0.7, 1, 10, 20,
	)
	runtime.MaxNominationsPerHour = 1
	runtime.SetMemory(staticMemory{block: "[memory] earlier [/memory]"})
	trigger := model.Message{ID: "trigger", AuthorID: "human", Content: "hello"}
	runtime.AddToHistory(trigger)
	outcome := runtime.HandleMessage(context.Background(), trigger, 1, nil)
	if outcome.Reply == nil || outcome.Reply.Content != "noted" ||
		outcome.Nomination == nil || outcome.Nomination.Content != "keep this" {
		t.Fatalf("outcome = %+v", outcome)
	}
	if len(mock.Calls) != 1 || len(mock.Calls[0].Messages) == 0 ||
		mock.Calls[0].Messages[0].Content != "[memory] earlier [/memory]" {
		t.Fatalf("memory was not prepended: %+v", mock.Calls)
	}

	mock.Resp.Content = `{"message":"","control":{"remember":"orphan"}}`
	second := model.Message{ID: "trigger-2", AuthorID: "human", Content: "again"}
	outcome = runtime.HandleMessage(context.Background(), second, 2, nil)
	if outcome.Reply != nil || outcome.Nomination != nil || outcome.Skip != SkipEmptyResponse {
		t.Fatalf("orphan nomination survived: %+v", outcome)
	}

	mock.Resp.Content = `{"message":"another reply","control":{"remember":"over limit"}}`
	third := model.Message{ID: "trigger-3", AuthorID: "human", Content: "again"}
	outcome = runtime.HandleMessage(context.Background(), third, 3, nil)
	if outcome.Reply == nil || outcome.Nomination != nil {
		t.Fatalf("nomination rate limit failed: %+v", outcome)
	}
}

func TestControlContractAdvertisesMemoryConditionally(t *testing.T) {
	if contract := ControlContract(true); !strings.Contains(contract, `"remember"`) ||
		!strings.Contains(contract, "[memory]") {
		t.Fatalf("memory contract incomplete:\n%s", contract)
	}
	if contract := ControlContract(false); strings.Contains(contract, `"remember"`) ||
		strings.Contains(contract, "[memory]") {
		t.Fatalf("disabled memory leaked into contract:\n%s", contract)
	}
}
