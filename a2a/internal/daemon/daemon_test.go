package daemon

import (
	"context"
	"encoding/json"
	"fmt"
	"sync"
	"testing"
	"time"

	"github.com/nats-io/nats.go"
	"github.com/veridian69/cairn/a2a/internal/config"
	"github.com/veridian69/cairn/a2a/internal/memory"
	"github.com/veridian69/cairn/a2a/internal/model"
	"github.com/veridian69/cairn/a2a/internal/provider"
)

func floatPtr(v float64) *float64 { return &v }

func TestDaemonStartStop(t *testing.T) {
	dir := t.TempDir()
	cfg := &config.Config{
		Agents:   map[string]config.AgentConfig{},
		Defaults: config.Defaults{ContextWindow: 10, Responsiveness: floatPtr(0.5)},
		Limits:   config.Limits{PerAgentPerHour: 20},
		Stream:   config.StreamConfig{DataDir: dir},
	}

	d, err := New(cfg, dir)
	if err != nil {
		t.Fatalf("New: %v", err)
	}

	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()

	if err := d.Start(ctx); err != nil {
		t.Fatalf("Start: %v", err)
	}
	defer d.Stop()

	if d.NATSUrl() == "" {
		t.Error("NATSUrl empty")
	}
}

func TestDaemonSeedsNewCheckpointFromConfiguredResponsiveness(t *testing.T) {
	oldFactory := newProvider
	t.Cleanup(func() { newProvider = oldFactory })
	newProvider = func(name, apiKey, baseURL string) (provider.Provider, error) {
		return &provider.MockProvider{}, nil
	}

	dir := t.TempDir()
	cfg := &config.Config{
		Agents: map[string]config.AgentConfig{
			"val": {
				Provider:       "mock",
				Model:          "test-model",
				System:         "test",
				Responsiveness: floatPtr(0.9),
			},
		},
		Defaults: config.Defaults{ContextWindow: 10, Responsiveness: floatPtr(0.5)},
		Limits:   config.Limits{PerAgentPerHour: 20},
		Stream:   config.StreamConfig{DataDir: dir},
	}
	d, err := New(cfg, dir)
	if err != nil {
		t.Fatal(err)
	}
	if err := d.Start(context.Background()); err != nil {
		t.Fatal(err)
	}
	defer d.Stop()

	statuses := d.Statuses()
	if len(statuses) != 1 || statuses[0].Responsiveness != 0.9 {
		t.Fatalf("statuses = %+v, want configured responsiveness 0.9", statuses)
	}
}

func TestDaemonPublish(t *testing.T) {
	dir := t.TempDir()
	cfg := &config.Config{
		Agents:   map[string]config.AgentConfig{},
		Defaults: config.Defaults{ContextWindow: 10, Responsiveness: floatPtr(0.5)},
		Limits:   config.Limits{PerAgentPerHour: 20},
		Stream:   config.StreamConfig{DataDir: dir},
	}

	d, err := New(cfg, dir)
	if err != nil {
		t.Fatalf("New: %v", err)
	}
	ctx := context.Background()
	if err := d.Start(ctx); err != nil {
		t.Fatalf("Start: %v", err)
	}
	defer d.Stop()

	p := model.Participant{ID: "test", Name: "operator", Kind: "human"}
	msg := model.NewMessage(p, "Hello", nil)
	if err := d.Publish(ctx, msg); err != nil {
		t.Fatalf("Publish: %v", err)
	}

	msgs, err := d.History(ctx, 0, 10)
	if err != nil {
		t.Fatalf("History: %v", err)
	}
	if len(msgs) != 1 {
		t.Fatalf("got %d msgs, want 1", len(msgs))
	}
}

type blockingProvider struct {
	startOnce sync.Once
	started   chan struct{}
	release   chan struct{}
}

func (p *blockingProvider) Complete(ctx context.Context, req provider.CompletionRequest) (provider.CompletionResponse, error) {
	p.startOnce.Do(func() { close(p.started) })
	select {
	case <-p.release:
	case <-ctx.Done():
		return provider.CompletionResponse{}, ctx.Err()
	}
	return provider.CompletionResponse{Content: "done"}, nil
}

func (p *blockingProvider) Name() string { return "mock" }

type blockingEmbedder struct {
	startOnce sync.Once
	started   chan struct{}
	afterStop func() error
	checked   chan error
}

func (embedder *blockingEmbedder) Embed(ctx context.Context, text string) ([]float32, error) {
	embedder.startOnce.Do(func() { close(embedder.started) })
	<-ctx.Done()
	if embedder.afterStop != nil {
		embedder.checked <- embedder.afterStop()
	}
	return nil, ctx.Err()
}

func (*blockingEmbedder) ModelName() string { return "test-model" }

func TestDaemonStatusAndActivity(t *testing.T) {
	oldFactory := newProvider
	t.Cleanup(func() { newProvider = oldFactory })

	blocker := &blockingProvider{
		started: make(chan struct{}),
		release: make(chan struct{}),
	}
	newProvider = func(name, apiKey, baseURL string) (provider.Provider, error) {
		if name != "mock" {
			return nil, fmt.Errorf("unexpected provider %q", name)
		}
		return blocker, nil
	}

	dir := t.TempDir()
	cfg := &config.Config{
		Agents: map[string]config.AgentConfig{
			"claude": {
				Provider:       "mock",
				Model:          "test-model",
				APIKey:         "unused",
				System:         "test",
				Responsiveness: floatPtr(1.0),
			},
		},
		Defaults: config.Defaults{ContextWindow: 10, Responsiveness: floatPtr(1.0)},
		Limits:   config.Limits{PerAgentPerHour: 20},
		Stream:   config.StreamConfig{DataDir: dir},
	}

	d, err := New(cfg, dir)
	if err != nil {
		t.Fatalf("New: %v", err)
	}
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	if err := d.Start(ctx); err != nil {
		t.Fatalf("Start: %v", err)
	}
	defer d.Stop()
	d.workers["claude"].runtime.Responsiveness = 1.0

	nc, err := nats.Connect(d.NATSUrl())
	if err != nil {
		t.Fatalf("Connect: %v", err)
	}
	defer nc.Close()

	sub, err := nc.SubscribeSync("a2a.activity")
	if err != nil {
		t.Fatalf("SubscribeSync: %v", err)
	}
	if err := nc.Flush(); err != nil {
		t.Fatalf("Flush: %v", err)
	}

	human := model.Participant{ID: "human-1", Name: "operator", Kind: model.KindHuman}
	if err := d.Publish(context.Background(), model.NewMessage(human, "hello", nil)); err != nil {
		t.Fatalf("Publish: %v", err)
	}

	select {
	case <-blocker.started:
	case <-time.After(5 * time.Second):
		t.Fatal("provider did not start")
	}

	thinkingMsg, err := sub.NextMsg(5 * time.Second)
	if err != nil {
		t.Fatalf("NextMsg thinking: %v", err)
	}
	var thinking ActivityEvent
	if err := json.Unmarshal(thinkingMsg.Data, &thinking); err != nil {
		t.Fatalf("unmarshal thinking: %v", err)
	}
	if thinking.Agent != "claude" || thinking.State != "thinking" {
		t.Fatalf("unexpected thinking event: %+v", thinking)
	}

	statuses := d.Statuses()
	if len(statuses) != 1 {
		t.Fatalf("got %d statuses, want 1", len(statuses))
	}
	if statuses[0].State != "thinking" {
		t.Fatalf("got state %q, want thinking", statuses[0].State)
	}
	if statuses[0].LastActivityAt.IsZero() {
		t.Fatal("LastActivityAt should be set while thinking")
	}

	close(blocker.release)

	idleMsg, err := sub.NextMsg(5 * time.Second)
	if err != nil {
		t.Fatalf("NextMsg idle: %v", err)
	}
	var idle ActivityEvent
	if err := json.Unmarshal(idleMsg.Data, &idle); err != nil {
		t.Fatalf("unmarshal idle: %v", err)
	}
	if idle.Agent != "claude" || idle.State != "idle" {
		t.Fatalf("unexpected idle event: %+v", idle)
	}

	deadline := time.Now().Add(5 * time.Second)
	for time.Now().Before(deadline) {
		statuses = d.Statuses()
		if len(statuses) == 1 && statuses[0].State == "active" && !statuses[0].LastActivityAt.IsZero() {
			break
		}
		time.Sleep(25 * time.Millisecond)
	}
	if len(statuses) != 1 {
		t.Fatalf("got %d statuses after release, want 1", len(statuses))
	}
	if statuses[0].State != "active" {
		t.Fatalf("got state %q after release, want active", statuses[0].State)
	}
	if statuses[0].LastActivityAt.IsZero() {
		t.Fatal("LastActivityAt should remain set after activity")
	}

	d.PauseAgent("claude")
	statuses = d.Statuses()
	if statuses[0].State != "paused" {
		t.Fatalf("got state %q after pause, want paused", statuses[0].State)
	}
}

func TestDaemonStatusRequestAndControlSubjects(t *testing.T) {
	oldFactory := newProvider
	t.Cleanup(func() { newProvider = oldFactory })
	newProvider = func(name, apiKey, baseURL string) (provider.Provider, error) {
		return &provider.MockProvider{
			Resp: provider.CompletionResponse{Content: "ok"},
		}, nil
	}

	dir := t.TempDir()
	cfg := &config.Config{
		Agents: map[string]config.AgentConfig{
			"claude": {
				Provider:       "mock",
				Model:          "test-model",
				APIKey:         "unused",
				System:         "test",
				Responsiveness: floatPtr(0.6),
			},
		},
		Defaults: config.Defaults{ContextWindow: 10, Responsiveness: floatPtr(0.6)},
		Limits:   config.Limits{PerAgentPerHour: 20},
		Stream:   config.StreamConfig{DataDir: dir},
	}

	d, err := New(cfg, dir)
	if err != nil {
		t.Fatalf("New: %v", err)
	}
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	if err := d.Start(ctx); err != nil {
		t.Fatalf("Start: %v", err)
	}
	defer d.Stop()

	nc, err := nats.Connect(d.NATSUrl())
	if err != nil {
		t.Fatalf("Connect: %v", err)
	}
	defer nc.Close()

	msg, err := nc.Request("a2a.status", nil, 2*time.Second)
	if err != nil {
		t.Fatalf("status request: %v", err)
	}
	var statuses []AgentStatus
	if err := json.Unmarshal(msg.Data, &statuses); err != nil {
		t.Fatalf("unmarshal statuses: %v", err)
	}
	if len(statuses) != 1 || statuses[0].Name != "claude" {
		t.Fatalf("unexpected statuses: %+v", statuses)
	}
	if statuses[0].State != "active" {
		t.Fatalf("initial state = %q, want active", statuses[0].State)
	}

	if err := nc.Publish("a2a.control", []byte("pause:claude")); err != nil {
		t.Fatalf("publish pause control: %v", err)
	}
	if err := nc.Flush(); err != nil {
		t.Fatalf("flush pause control: %v", err)
	}

	deadline := time.Now().Add(2 * time.Second)
	for time.Now().Before(deadline) {
		if got := d.Statuses(); len(got) == 1 && got[0].State == "paused" {
			break
		}
		time.Sleep(20 * time.Millisecond)
	}
	if got := d.Statuses(); len(got) != 1 || got[0].State != "paused" {
		t.Fatalf("state after pause = %+v, want paused", got)
	}

	if err := nc.Publish("a2a.control", []byte("resume:claude")); err != nil {
		t.Fatalf("publish resume control: %v", err)
	}
	if err := nc.Flush(); err != nil {
		t.Fatalf("flush resume control: %v", err)
	}

	deadline = time.Now().Add(2 * time.Second)
	for time.Now().Before(deadline) {
		if got := d.Statuses(); len(got) == 1 && got[0].State == "active" {
			break
		}
		time.Sleep(20 * time.Millisecond)
	}
	if got := d.Statuses(); len(got) != 1 || got[0].State != "active" {
		t.Fatalf("state after resume = %+v, want active", got)
	}
}

func TestDaemonPersistsNominationAfterPublishingReply(t *testing.T) {
	oldFactory := newProvider
	t.Cleanup(func() { newProvider = oldFactory })
	newProvider = func(name, apiKey, baseURL string) (provider.Provider, error) {
		return &provider.MockProvider{Resp: provider.CompletionResponse{
			Content: `{"message":"noted","control":{"remember":"the durable fact"}}`,
		}}, nil
	}
	dir := t.TempDir()
	cfg := &config.Config{
		Agents: map[string]config.AgentConfig{
			"claude": {
				Provider: "mock", Model: "model", System: "system",
				Responsiveness: floatPtr(1),
			},
		},
		Defaults: config.Defaults{ContextWindow: 10, Responsiveness: floatPtr(1)},
		Limits:   config.Limits{PerAgentPerHour: 20},
		Stream:   config.StreamConfig{DataDir: dir},
	}
	daemon, err := New(cfg, dir)
	if err != nil {
		t.Fatal(err)
	}
	if err := daemon.Start(context.Background()); err != nil {
		t.Fatal(err)
	}
	defer daemon.Stop()
	daemon.workers["claude"].runtime.Responsiveness = 1
	if err := daemon.Publish(context.Background(), model.NewMessage(
		model.Participant{ID: "human", Name: "operator", Kind: model.KindHuman},
		"remember this", nil,
	)); err != nil {
		t.Fatal(err)
	}
	deadline := time.Now().Add(5 * time.Second)
	for time.Now().Before(deadline) {
		items, listErr := daemon.memory.List(memory.ListFilter{}, nil)
		if listErr == nil && len(items) == 1 {
			if items[0].Content != "the durable fact" ||
				items[0].SourceMessageID == "" ||
				items[0].NominatedBy != items[0].AuthorID {
				t.Fatalf("bad nomination provenance: %+v", items[0])
			}
			return
		}
		time.Sleep(20 * time.Millisecond)
	}
	t.Fatal("nomination was not persisted")
}

func TestDaemonMemoryFailureDoesNotBlockReplyOrCheckpoint(t *testing.T) {
	oldFactory := newProvider
	t.Cleanup(func() { newProvider = oldFactory })
	newProvider = func(name, apiKey, baseURL string) (provider.Provider, error) {
		return &provider.MockProvider{Resp: provider.CompletionResponse{Content: "reply survives"}}, nil
	}
	dir := t.TempDir()
	cfg := &config.Config{
		Agents: map[string]config.AgentConfig{
			"claude": {
				Provider: "mock", Model: "model", System: "system",
				Responsiveness: floatPtr(1),
			},
		},
		Defaults: config.Defaults{ContextWindow: 10, Responsiveness: floatPtr(1)},
		Limits:   config.Limits{PerAgentPerHour: 20},
		Stream:   config.StreamConfig{DataDir: dir},
	}
	daemon, err := New(cfg, dir)
	if err != nil {
		t.Fatal(err)
	}
	if err := daemon.memory.Close(); err != nil {
		t.Fatal(err)
	}
	if err := daemon.Start(context.Background()); err != nil {
		t.Fatal(err)
	}
	defer daemon.Stop()
	daemon.workers["claude"].runtime.Responsiveness = 1
	if err := daemon.Publish(context.Background(), model.NewMessage(
		model.Participant{ID: "human", Name: "operator", Kind: model.KindHuman},
		"trigger", nil,
	)); err != nil {
		t.Fatal(err)
	}

	participantID := daemon.workers["claude"].runtime.Participant.ID
	deadline := time.Now().Add(5 * time.Second)
	for time.Now().Before(deadline) {
		history, historyErr := daemon.History(context.Background(), 0, 10)
		checkpoint, checkpointErr := daemon.state.GetCheckpoint(participantID, 0.5)
		if historyErr == nil && checkpointErr == nil &&
			len(history) >= 2 && checkpoint.LastSeenSeq > 0 {
			if history[len(history)-1].Content != "reply survives" {
				t.Fatalf("last message = %+v", history[len(history)-1])
			}
			return
		}
		time.Sleep(20 * time.Millisecond)
	}
	t.Fatal("memory failure prevented reply publication or checkpoint persistence")
}

func TestDaemonStopCancelsAndWaitsForEmbeddingWorker(t *testing.T) {
	oldFactory := newEmbedder
	t.Cleanup(func() { newEmbedder = oldFactory })
	blocker := &blockingEmbedder{started: make(chan struct{}), checked: make(chan error, 1)}
	newEmbedder = func(string, string, string, string) (provider.Embedder, error) {
		return blocker, nil
	}
	dir := t.TempDir()
	cfg := &config.Config{
		Agents:   map[string]config.AgentConfig{},
		Defaults: config.Defaults{ContextWindow: 10, Responsiveness: floatPtr(1)},
		Limits:   config.Limits{PerAgentPerHour: 20},
		Stream:   config.StreamConfig{DataDir: dir},
		Memory: config.MemoryConfig{Embedding: &config.EmbeddingConfig{
			Provider: "openai", Model: "test-model", APIKey: "test",
		}},
	}
	daemon, err := New(cfg, dir)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := daemon.memory.Insert(memory.Item{
		Content: "pending", AuthorID: "agent", AuthorName: "claude",
		SourceMessageID: "source", SourceCreatedAt: time.Now().UTC(),
		NominatedBy: "human",
	}); err != nil {
		t.Fatal(err)
	}
	blocker.afterStop = func() error {
		_, err := daemon.memory.List(memory.ListFilter{}, map[string]bool{})
		return err
	}
	if err := daemon.Start(context.Background()); err != nil {
		t.Fatal(err)
	}
	select {
	case <-blocker.started:
	case <-time.After(2 * time.Second):
		daemon.Stop()
		t.Fatal("embedding worker did not start")
	}
	stopped := make(chan struct{})
	go func() {
		daemon.Stop()
		close(stopped)
	}()
	select {
	case <-stopped:
	case <-time.After(2 * time.Second):
		t.Fatal("Stop did not cancel and wait for embedding worker")
	}
	if err := <-blocker.checked; err != nil {
		t.Fatalf("memory database closed before embedding worker exited: %v", err)
	}
}
