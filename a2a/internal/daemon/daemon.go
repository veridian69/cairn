package daemon

import (
	"context"
	"encoding/json"
	"fmt"
	"log"
	"path/filepath"
	"strings"
	"sync"
	"time"

	"github.com/nats-io/nats.go"
	"github.com/veridian69/cairn/a2a/internal/agent"
	"github.com/veridian69/cairn/a2a/internal/config"
	"github.com/veridian69/cairn/a2a/internal/memory"
	"github.com/veridian69/cairn/a2a/internal/model"
	"github.com/veridian69/cairn/a2a/internal/provider"
	"github.com/veridian69/cairn/a2a/internal/state"
	"github.com/veridian69/cairn/a2a/internal/transport"
)

// MessageWithSeq pairs a message with its JetStream sequence number.
type MessageWithSeq struct {
	Message model.Message
	Seq     uint64
}

// agentWorker wraps a runtime with a serial inbox channel.
type agentWorker struct {
	runtime   *agent.Runtime
	apiKey    string
	inbox     chan MessageWithSeq
	subStream *transport.Stream
	statusMu  sync.RWMutex
	state     string
	lastEvent time.Time
}

// Daemon is the central orchestrator. It owns the NATS server, JetStream
// stream, SQLite state, and per-agent worker goroutines.
type Daemon struct {
	cfg                   *config.Config
	server                *transport.Server
	stream                *transport.Stream
	state                 *state.DB
	memory                *memory.Store
	injector              *memory.Injector
	embedder              provider.Embedder
	embedTimeout          time.Duration
	embedWake             chan struct{}
	embedScanInterval     time.Duration
	workers               map[string]*agentWorker
	workersMu             sync.RWMutex
	verboseProviderErrors bool
	cancel                context.CancelFunc
	wg                    sync.WaitGroup
}

type AgentStatus struct {
	Name           string    `json:"name"`
	Provider       string    `json:"provider"`
	Model          string    `json:"model"`
	Active         bool      `json:"active"`
	State          string    `json:"state"`
	Responsiveness float64   `json:"responsiveness"`
	QueueDepth     int       `json:"queue_depth"`
	LastSeenSeq    uint64    `json:"last_seen_seq"`
	HourlyCount    int       `json:"hourly_count"`
	LastActivityAt time.Time `json:"last_activity_at"`
}

type ActivityEvent struct {
	Agent string    `json:"agent"`
	State string    `json:"state"`
	At    time.Time `json:"at"`
}

var newProvider = provider.NewProvider
var newEmbedder = provider.NewEmbedder

// Option configures a Daemon.
type Option func(*Daemon)

// WithVerboseProviderErrors controls whether safe provider response bodies are logged.
func WithVerboseProviderErrors(enabled bool) Option {
	return func(d *Daemon) {
		d.verboseProviderErrors = enabled
	}
}

// New creates a Daemon, opening the SQLite state database in dataDir.
func New(cfg *config.Config, dataDir string, options ...Option) (*Daemon, error) {
	stateDB, err := state.Open(filepath.Join(dataDir, "state.db"))
	if err != nil {
		return nil, fmt.Errorf("opening state: %w", err)
	}

	d := &Daemon{
		cfg: cfg, state: stateDB, workers: make(map[string]*agentWorker),
		embedWake: make(chan struct{}, 1), embedScanInterval: 30 * time.Second,
	}
	for _, option := range options {
		option(d)
	}
	if cfg.Memory.EffectiveEnabled() {
		store, err := memory.Open(dataDir, memory.Options{
			RebuildFTS: true, MaxItemBytes: cfg.Memory.EffectiveMaxItemBytes(),
		})
		if err != nil {
			log.Printf("memory disabled: %v", err)
		} else {
			d.memory = store
		}
	}
	return d, nil
}

// Start launches the embedded NATS server, creates the JetStream stream,
// registers agents as participants, spins up per-agent serial workers,
// and subscribes to the stream for fan-out dispatch.
func (d *Daemon) Start(ctx context.Context) error {
	srv, err := transport.NewServer(d.cfg.Stream.DataDir)
	if err != nil {
		return fmt.Errorf("NATS: %w", err)
	}
	d.server = srv

	maxAge, err := d.cfg.Stream.MaxAgeDuration()
	if err != nil {
		srv.Stop()
		return fmt.Errorf("stream max_age: %w", err)
	}

	stream, err := transport.NewManagedStream(srv.ClientURL(), transport.StreamOptions{
		MaxAge:   maxAge,
		MaxBytes: d.cfg.Stream.MaxBytes,
	})
	if err != nil {
		srv.Stop()
		return fmt.Errorf("stream: %w", err)
	}
	d.stream = stream

	agentCtx, cancel := context.WithCancel(ctx)
	d.cancel = cancel

	if d.memory != nil {
		if embedding := d.cfg.Memory.Embedding; embedding != nil && embedding.Provider != "" {
			embedder, err := newEmbedder(
				embedding.Provider, embedding.EffectiveAPIKey(), embedding.Model, "",
			)
			if err != nil {
				log.Printf("embedding disabled: %v", err)
			} else {
				d.embedder = embedder
				d.embedTimeout, _ = embedding.TimeoutDuration()
			}
		}
		queryTimeout := 2 * time.Second
		embedQueries := false
		if embedding := d.cfg.Memory.Embedding; embedding != nil {
			queryTimeout, _ = embedding.QueryTimeoutDuration()
			embedQueries = embedding.EffectiveEmbedQueries()
		}
		d.injector = &memory.Injector{
			Store: d.memory, Embedder: d.embedder, EmbedQueries: embedQueries,
			QueryTimeout:  queryTimeout,
			RecallLimit:   d.cfg.Memory.EffectiveRecallLimit(),
			MaxPinned:     d.cfg.Memory.EffectiveMaxPinnedInjected(),
			QueryBytes:    d.cfg.Memory.EffectiveQueryBytes(),
			MaxBlockBytes: d.cfg.Memory.EffectiveMaxBlockBytes(),
		}
	}

	// Register agents and create per-agent serial workers.
	for name, ac := range d.cfg.Agents {
		p, err := d.state.RegisterParticipant(model.Participant{
			Name: name, Kind: model.KindAgent, Provider: ac.Provider, Model: ac.Model,
		})
		if err != nil {
			log.Printf("skip agent %q: %v", name, err)
			continue
		}

		prov, err := newProvider(ac.Provider, ac.EffectiveAPIKey(), "")
		if err != nil {
			log.Printf("skip agent %q: %v", name, err)
			continue
		}

		initialResponsiveness := ac.EffectiveResponsiveness(d.cfg.DefaultResponsiveness())

		// Load checkpoint for restart-safe resume, seeding new agents from config.
		cp, err := d.state.GetCheckpoint(p.ID, initialResponsiveness)
		if err != nil {
			log.Printf("skip agent %q checkpoint: %v", name, err)
			continue
		}

		system := ac.System
		if d.cfg.Defaults.EffectiveControlContract() {
			system += "\n\n" + agent.ControlContract(d.memory != nil)
		}
		rt := agent.NewRuntime(p, prov, ac.Model, system,
			ac.EffectiveTemperature(), initialResponsiveness,
			d.cfg.Defaults.ContextWindow, d.cfg.Limits.PerAgentPerHour,
		)
		rt.MaxNominationsPerHour = d.cfg.Memory.EffectiveMaxNominationsPerHour()
		if d.injector != nil {
			rt.SetMemory(d.injector)
		}
		rt.RestoreCheckpoint(cp.LastSeenSeq, cp.HourlyCount, cp.HourlyResetAt, cp.Responsiveness)

		subStream, err := transport.NewStream(srv.ClientURL())
		if err != nil {
			log.Printf("skip agent %q subscription: %v", name, err)
			continue
		}

		w := &agentWorker{
			runtime:   rt,
			apiKey:    ac.EffectiveAPIKey(),
			inbox:     make(chan MessageWithSeq, 64),
			subStream: subStream,
			state:     "active",
		}
		w.markState("active")
		rt.SetProviderHooks(func() {
			w.markState("thinking")
			d.publishActivity(w.runtime.Participant.Name, "thinking")
		}, func() {
			w.markState("active")
			d.publishActivity(w.runtime.Participant.Name, "idle")
		})
		d.workersMu.Lock()
		d.workers[name] = w
		d.workersMu.Unlock()

		// Launch dedicated serial worker goroutine per agent.
		d.wg.Add(1)
		go d.runWorker(agentCtx, w)

		startSeq := cp.LastSeenSeq + 1
		if err := subStream.SubscribeFromSeq(agentCtx, startSeq, func(msg model.Message, seq uint64) error {
			select {
			case w.inbox <- MessageWithSeq{Message: msg, Seq: seq}:
				return nil
			case <-agentCtx.Done():
				return agentCtx.Err()
			}
		}); err != nil {
			log.Printf("skip agent %q subscribe: %v", name, err)
			subStream.Close()
			d.workersMu.Lock()
			delete(d.workers, name)
			d.workersMu.Unlock()
			continue
		}
	}

	if d.memory != nil && d.embedder != nil {
		d.wg.Add(1)
		go d.runEmbedWorker(agentCtx)
	}

	// Control message subscription (pause/resume).
	nc := stream.NATSConn()
	if _, err := nc.Subscribe("a2a.control", func(msg *nats.Msg) {
		parts := strings.SplitN(string(msg.Data), ":", 2)
		if len(parts) != 2 {
			return
		}
		switch parts[0] {
		case "pause":
			d.PauseAgent(parts[1])
		case "resume":
			d.ResumeAgent(parts[1])
		}
	}); err != nil {
		cancel()
		stream.Close()
		srv.Stop()
		return fmt.Errorf("control subscription: %w", err)
	}

	if _, err := nc.Subscribe("a2a.status", func(msg *nats.Msg) {
		data, err := json.Marshal(d.Statuses())
		if err != nil {
			return
		}
		_ = msg.Respond(data)
	}); err != nil {
		cancel()
		stream.Close()
		srv.Stop()
		return fmt.Errorf("status subscription: %w", err)
	}

	return nil
}

// runWorker is the serial processing loop for one agent.
func (d *Daemon) runWorker(ctx context.Context, w *agentWorker) {
	defer d.wg.Done()
	for {
		select {
		case <-ctx.Done():
			return
		case item, ok := <-w.inbox:
			if !ok {
				return
			}
			redactedIDs, err := d.state.RedactedIDs()
			if err != nil {
				log.Printf("[%s] redaction lookup error: %v", w.runtime.Participant.Name, err)
				continue
			}

			w.runtime.AddToHistory(item.Message)

			outcome := w.runtime.HandleMessage(ctx, item.Message, item.Seq, redactedIDs)

			if outcome.Skip != "" {
				participantName := w.runtime.Participant.Name
				messageID := model.ShortID(item.Message.ID)
				diagnostic := ""
				if outcome.Skip == agent.SkipProviderError && outcome.Err != nil {
					participantName = sanitiseProviderErrorPrefixToken(participantName, w.apiKey)
					redactedMessageID := redactProviderErrorPrefixValue(item.Message.ID, w.apiKey)
					messageID = sanitiseProviderErrorPrefixToken(model.ShortID(redactedMessageID), "")
					diagnostic = ": " + formatProviderError(
						w.runtime.Provider.Name(),
						w.runtime.Model,
						w.apiKey,
						d.verboseProviderErrors,
						outcome.Err,
					)
				}
				log.Printf("[%s] skip %s: %s%s",
					participantName, messageID, outcome.Skip, diagnostic)
			}

			if outcome.Reply != nil {
				if err := d.stream.PublishWithDedup(ctx, *outcome.Reply, w.runtime.Participant.ID); err != nil {
					log.Printf("[%s] publish error: %v", w.runtime.Participant.Name, err)
					continue
				}
				if outcome.Nomination != nil && d.memory != nil {
					if _, err := d.memory.Insert(memory.Item{
						Content:         outcome.Nomination.Content,
						AuthorID:        w.runtime.Participant.ID,
						AuthorName:      w.runtime.Participant.Name,
						SourceMessageID: outcome.Reply.ID,
						SourceCreatedAt: outcome.Reply.CreatedAt,
						ReplyTo:         outcome.Reply.ReplyTo,
						NominatedBy:     w.runtime.Participant.ID,
					}); err != nil {
						log.Printf("[%s] nomination rejected (%d bytes): %v",
							w.runtime.Participant.Name, len(outcome.Nomination.Content), err)
					} else {
						select {
						case d.embedWake <- struct{}{}:
						default:
						}
					}
				}
			}

			snap := w.runtime.Snapshot()
			cp := state.Checkpoint{
				ParticipantID:   w.runtime.Participant.ID,
				LastSeenSeq:     item.Seq,
				LastProcessedID: item.Message.ID,
				HourlyCount:     snap.HourlyCount,
				HourlyResetAt:   snap.HourlyResetAt,
				Responsiveness:  snap.Responsiveness,
			}
			if outcome.Reply != nil {
				cp.LastRespondedID = outcome.Reply.ID
			}
			if err := d.state.SaveCheckpoint(cp); err != nil {
				log.Printf("[%s] checkpoint error: %v", w.runtime.Participant.Name, err)
				continue
			}
			w.runtime.MarkCheckpointSaved(item.Seq)
		}
	}
}

func (w *agentWorker) markState(state string) {
	w.statusMu.Lock()
	w.state = state
	w.lastEvent = time.Now().UTC()
	w.statusMu.Unlock()
}

func (w *agentWorker) snapshotState() (string, time.Time) {
	w.statusMu.RLock()
	defer w.statusMu.RUnlock()
	return w.state, w.lastEvent
}

func (d *Daemon) publishActivity(agentName, state string) {
	if d.stream == nil {
		return
	}
	data, err := json.Marshal(ActivityEvent{
		Agent: agentName,
		State: state,
		At:    time.Now().UTC(),
	})
	if err != nil {
		log.Printf("[%s] activity marshal error: %v", agentName, err)
		return
	}
	if err := d.stream.NATSConn().Publish("a2a.activity", data); err != nil {
		log.Printf("[%s] activity publish error: %v", agentName, err)
	}
}

// Publish sends a message to the stream.
func (d *Daemon) Publish(ctx context.Context, msg model.Message) error {
	return d.stream.Publish(ctx, msg)
}

// History replays messages from the stream.
func (d *Daemon) History(ctx context.Context, startSeq uint64, limit int) ([]model.Message, error) {
	return d.stream.Replay(ctx, startSeq, limit)
}

// NATSUrl returns the embedded NATS server's client URL.
func (d *Daemon) NATSUrl() string {
	if d.server == nil {
		return ""
	}
	return d.server.ClientURL()
}

// State returns the underlying state database.
func (d *Daemon) State() *state.DB { return d.state }

func (d *Daemon) Statuses() []AgentStatus {
	d.workersMu.RLock()
	defer d.workersMu.RUnlock()
	statuses := make([]AgentStatus, 0, len(d.workers))
	for name, w := range d.workers {
		snap := w.runtime.Snapshot()
		state, lastEvent := w.snapshotState()
		if !snap.Active && state != "offline" {
			state = "paused"
		}
		statuses = append(statuses, AgentStatus{
			Name:           name,
			Provider:       w.runtime.Participant.Provider,
			Model:          w.runtime.Participant.Model,
			Active:         snap.Active,
			State:          state,
			Responsiveness: snap.Responsiveness,
			QueueDepth:     len(w.inbox),
			LastSeenSeq:    snap.LastSeenSeq,
			HourlyCount:    snap.HourlyCount,
			LastActivityAt: lastEvent.UTC(),
		})
	}
	return statuses
}

// PauseAgent deactivates an agent's processing loop.
func (d *Daemon) PauseAgent(name string) {
	d.workersMu.RLock()
	w, ok := d.workers[name]
	d.workersMu.RUnlock()
	if ok {
		w.runtime.SetActive(false)
		w.markState("paused")
		log.Printf("[%s] paused", name)
	}
}

// ResumeAgent reactivates an agent's processing loop.
func (d *Daemon) ResumeAgent(name string) {
	d.workersMu.RLock()
	w, ok := d.workers[name]
	d.workersMu.RUnlock()
	if ok {
		w.runtime.SetActive(true)
		w.markState("active")
		log.Printf("[%s] resumed", name)
	}
}

// Stop tears down all resources: cancels workers, closes stream, stops NATS, closes SQLite.
func (d *Daemon) Stop() {
	if d.cancel != nil {
		d.cancel()
	}
	d.workersMu.RLock()
	workers := make([]*agentWorker, 0, len(d.workers))
	for _, w := range d.workers {
		workers = append(workers, w)
	}
	d.workersMu.RUnlock()
	for _, w := range workers {
		if w.subStream != nil {
			w.subStream.Close()
		}
	}
	d.wg.Wait()
	if d.stream != nil {
		d.stream.Close()
	}
	if d.server != nil {
		d.server.Stop()
	}
	if d.memory != nil {
		_ = d.memory.Close()
	}
	if d.state != nil {
		_ = d.state.Close()
	}
}
