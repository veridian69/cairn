package cmd

import (
	"bytes"
	"context"
	"encoding/json"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/veridian69/cairn/a2a/internal/model"
)

func TestAccountantCommandShowsRecordsWithoutConversationText(t *testing.T) {
	_, stream, _ := setupCommandStateAndStream(t)
	capital := 12000.0
	proposal := model.NewMessage(
		model.Participant{ID: "agent-val", Name: "val", Kind: model.KindAgent},
		"Long conversational explanation that belongs in chat.",
		nil,
	)
	proposal.Accountant = &model.AccountantRecord{
		Proposer:           "val",
		Idea:               "Temporarily reduce Roy's working hours",
		Probability:        "medium",
		CapitalRequiredCHF: &capital,
		NextExperiment:     "Ask HR for a calculation",
	}
	if err := stream.Publish(context.Background(), proposal); err != nil {
		t.Fatal(err)
	}
	if err := stream.Publish(context.Background(), model.NewMessage(
		model.Participant{ID: "human", Name: "operator", Kind: model.KindHuman},
		"This ordinary chat message has no accountant record.",
		nil,
	)); err != nil {
		t.Fatal(err)
	}

	oldLast, oldJSON, oldFollow := accountantLast, accountantJSON, accountantFollow
	accountantLast, accountantJSON, accountantFollow = 50, false, false
	t.Cleanup(func() {
		accountantLast, accountantJSON, accountantFollow = oldLast, oldJSON, oldFollow
		accountantCmd.SetOut(nil)
		accountantCmd.SetContext(nil)
	})
	var output bytes.Buffer
	accountantCmd.SetOut(&output)
	accountantCmd.SetContext(context.Background())

	if err := accountantCmd.RunE(accountantCmd, nil); err != nil {
		t.Fatalf("accountant command: %v", err)
	}

	got := output.String()
	for _, want := range []string{
		"val", "medium", "Temporarily reduce Roy's working hours",
		"CHF 12000", "Ask HR for a calculation",
	} {
		if !strings.Contains(got, want) {
			t.Fatalf("output missing %q:\n%s", want, got)
		}
	}
	for _, unwanted := range []string{
		"Long conversational explanation",
		"This ordinary chat message",
	} {
		if strings.Contains(got, unwanted) {
			t.Fatalf("output contains chat text %q:\n%s", unwanted, got)
		}
	}
}

type lockedBuffer struct {
	mu sync.Mutex
	b  bytes.Buffer
}

func (b *lockedBuffer) Write(p []byte) (int, error) {
	b.mu.Lock()
	defer b.mu.Unlock()
	return b.b.Write(p)
}

func (b *lockedBuffer) String() string {
	b.mu.Lock()
	defer b.mu.Unlock()
	return b.b.String()
}

func TestAccountantCommandFollowsNewRecords(t *testing.T) {
	_, stream, _ := setupCommandStateAndStream(t)
	oldLast, oldJSON, oldFollow := accountantLast, accountantJSON, accountantFollow
	accountantLast, accountantJSON, accountantFollow = 50, true, true
	ctx, cancel := context.WithCancel(context.Background())
	t.Cleanup(func() {
		cancel()
		accountantLast, accountantJSON, accountantFollow = oldLast, oldJSON, oldFollow
		accountantCmd.SetOut(nil)
		accountantCmd.SetContext(nil)
	})
	var output lockedBuffer
	accountantCmd.SetOut(&output)
	accountantCmd.SetContext(ctx)

	errCh := make(chan error, 1)
	go func() {
		errCh <- accountantCmd.RunE(accountantCmd, nil)
	}()

	proposal := model.NewMessage(
		model.Participant{ID: "agent-zen", Name: "zen", Kind: model.KindAgent},
		"Conversation remains elsewhere.",
		nil,
	)
	proposal.Accountant = &model.AccountantRecord{
		Proposer: "zen",
		Idea:     "Trial a four-day fortnight",
	}
	if err := stream.Publish(context.Background(), proposal); err != nil {
		t.Fatal(err)
	}

	deadline := time.Now().Add(5 * time.Second)
	for !strings.Contains(output.String(), proposal.ID) && time.Now().Before(deadline) {
		time.Sleep(10 * time.Millisecond)
	}
	if !strings.Contains(output.String(), proposal.ID) {
		t.Fatalf("follow output did not receive proposal:\n%s", output.String())
	}

	cancel()
	select {
	case err := <-errCh:
		if err != nil {
			t.Fatalf("follow command: %v", err)
		}
	case <-time.After(2 * time.Second):
		t.Fatal("follow command did not stop after context cancellation")
	}
}

func TestAccountantCommandWritesNDJSON(t *testing.T) {
	_, stream, _ := setupCommandStateAndStream(t)
	proposal := model.NewMessage(
		model.Participant{ID: "agent-spike", Name: "spike", Kind: model.KindAgent},
		"Readable proposal.",
		nil,
	)
	proposal.Accountant = &model.AccountantRecord{
		Proposer:    "spike",
		Idea:        "Request the pension calculation",
		Probability: "high",
	}
	if err := stream.Publish(context.Background(), proposal); err != nil {
		t.Fatal(err)
	}

	oldLast, oldJSON, oldFollow := accountantLast, accountantJSON, accountantFollow
	accountantLast, accountantJSON, accountantFollow = 50, true, false
	t.Cleanup(func() {
		accountantLast, accountantJSON, accountantFollow = oldLast, oldJSON, oldFollow
		accountantCmd.SetOut(nil)
		accountantCmd.SetContext(nil)
	})
	var output bytes.Buffer
	accountantCmd.SetOut(&output)
	accountantCmd.SetContext(context.Background())

	if err := accountantCmd.RunE(accountantCmd, nil); err != nil {
		t.Fatalf("accountant command: %v", err)
	}

	var entry struct {
		MessageID  string                 `json:"message_id"`
		Author     string                 `json:"author"`
		Accountant model.AccountantRecord `json:"accountant"`
	}
	if err := json.Unmarshal(bytes.TrimSpace(output.Bytes()), &entry); err != nil {
		t.Fatalf("decode NDJSON line: %v\n%s", err, output.String())
	}
	if entry.MessageID != proposal.ID || entry.Author != "spike" ||
		entry.Accountant.Idea != "Request the pension calculation" {
		t.Fatalf("entry = %#v", entry)
	}
	if strings.Contains(output.String(), "Readable proposal") {
		t.Fatalf("JSON output contains chat content:\n%s", output.String())
	}
}
