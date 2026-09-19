package cmd

import (
	"context"
	"testing"

	"github.com/veridian69/cairn/a2a/internal/model"
)

type cancelAccountantOutput struct{ cancel context.CancelFunc }

func (w cancelAccountantOutput) Write(data []byte) (int, error) {
	w.cancel()
	return len(data), nil
}

// Cancellation can arrive after replay prints but before the live consumer
// exists. That is the same normal follow shutdown as cancellation after attach.
func TestAccountantFollowCancellationBetweenReplayAndSubscribe(t *testing.T) {
	_, stream, _ := setupCommandStateAndStream(t)
	message := model.NewMessage(model.Participant{ID: "test", Name: "test", Kind: model.KindAgent}, "proposal", nil)
	message.Accountant = &model.AccountantRecord{Proposer: "test", Idea: "test cancellation", Probability: "high"}
	if err := stream.Publish(context.Background(), message); err != nil {
		t.Fatal(err)
	}
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	oldLast, oldJSON, oldFollow := accountantLast, accountantJSON, accountantFollow
	accountantLast, accountantJSON, accountantFollow = 50, true, true
	t.Cleanup(func() {
		accountantLast, accountantJSON, accountantFollow = oldLast, oldJSON, oldFollow
		accountantCmd.SetOut(nil)
		accountantCmd.SetContext(nil)
	})
	accountantCmd.SetContext(ctx)
	accountantCmd.SetOut(cancelAccountantOutput{cancel: cancel})
	if err := accountantCmd.RunE(accountantCmd, nil); err != nil {
		t.Fatalf("cancelled follow must stop normally: %v", err)
	}
}
