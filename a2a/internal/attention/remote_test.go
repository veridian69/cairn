package attention

import (
	"context"
	"encoding/json"
	"testing"

	"github.com/veridian69/cairn/a2a/internal/garden"
	"github.com/veridian69/cairn/a2a/internal/gardenauth"
)

type fakeGarden struct {
	status garden.StatusResult
	sent   garden.SendArgs
	poll   garden.PollResult
	acks   int
}

func (f *fakeGarden) Status(context.Context) (garden.StatusResult, error) { return f.status, nil }
func (f *fakeGarden) Send(_ context.Context, a garden.SendArgs) (garden.Message, error) {
	f.sent = a
	return garden.Message{ID: "reply", Binding: f.status.Binding}, nil
}
func (f *fakeGarden) Read(context.Context, garden.ReadArgs) (garden.ReadResult, error) {
	return garden.ReadResult{Messages: []garden.Message{}}, nil
}
func (f *fakeGarden) Poll(context.Context, garden.PollArgs) (garden.PollResult, error) {
	return f.poll, nil
}
func (f *fakeGarden) Acknowledge(context.Context, garden.AckArgs) error { f.acks++; return nil }

func TestRemoteToolsPreserveExplicitRecipientsAndCannotOverrideIdentity(t *testing.T) {
	p := Profile{InstanceID: "instance", Scope: gardenauth.Scope{Realm: "r", Segments: []gardenauth.Segment{}}, Classification: "internal", Participant: "Val"}
	f := &fakeGarden{status: garden.StatusResult{Participant: "Val", Binding: garden.Binding{InstanceID: p.InstanceID, Scope: p.Scope, Classification: p.Classification}}}
	remote := &Remote{api: f, profile: p}
	_, err := remote.Call(context.Background(), "send_message", json.RawMessage(`{"content":"reviewed","recipients":["Spike"],"reply_to":"parent"}`))
	if err != nil {
		t.Fatal(err)
	}
	if len(f.sent.Recipients) != 1 || f.sent.Recipients[0] != "Spike" || f.sent.ReplyTo != "parent" {
		t.Fatalf("wrong outgoing message: %#v", f.sent)
	}
	_, err = remote.Call(context.Background(), "send_message", json.RawMessage(`{"content":"fake","author":"Operator"}`))
	if err == nil {
		t.Fatal("identity override accepted")
	}
}

func TestRemoteRefusesWrongBindingBeforeSending(t *testing.T) {
	f := &fakeGarden{status: garden.StatusResult{Participant: "Val", Binding: garden.Binding{InstanceID: "wrong"}}}
	remote := &Remote{api: f, profile: Profile{InstanceID: "expected", Participant: "Val"}}
	if _, err := remote.Call(context.Background(), "send_message", json.RawMessage(`{"content":"private"}`)); err == nil {
		t.Fatal("wrong instance accepted")
	}
	if f.sent.Content != "" {
		t.Fatal("sent content to wrong binding")
	}
}

func TestRemotePollingCarriesProvenanceAndDoesNotAcknowledge(t *testing.T) {
	p := Profile{InstanceID: "instance", Scope: gardenauth.Scope{Realm: "r"}, Classification: "internal", Participant: "Val"}
	b := garden.Binding{InstanceID: p.InstanceID, Scope: p.Scope, Classification: p.Classification}
	f := &fakeGarden{status: garden.StatusResult{Participant: "Val", Binding: b}, poll: garden.PollResult{Message: &garden.Message{ID: "incoming", AuthorName: "Spike", Content: "review", Binding: b}, Receipt: "receipt", Generation: "g"}}
	remote := &Remote{api: f, profile: p}
	pending, err := remote.Poll(context.Background(), "consumer")
	if err != nil {
		t.Fatal(err)
	}
	if pending.Event.Sender != "Spike" || pending.Event.Recipient != "Val" || pending.Receipt != "receipt" || f.acks != 0 {
		t.Fatalf("invalid pending: %#v", pending)
	}
}
