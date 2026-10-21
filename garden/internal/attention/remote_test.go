package attention

import (
	"context"
	"encoding/json"
	"errors"
	"strings"
	"sync"
	"testing"

	"github.com/veridian69/cairn/garden/internal/garden"
	"github.com/veridian69/cairn/garden/internal/gardenauth"
)

type fakeGarden struct {
	status garden.StatusResult
	sent   garden.SendArgs
	poll   garden.PollResult
	acks   int
	// heartbeats records every heartbeat so tests can see when deliverability was declared.
	heartbeats []garden.HeartbeatArgs
	who        garden.WhoResult
	polls      int
	mu         sync.Mutex
	// supported lists the optional tools the fake gateway advertises.
	supported map[string]bool
	// holder is the consumer that owns the lease; a different live consumer is refused.
	holder  string
	pollErr error
}

func (f *fakeGarden) Supports(tool string) bool { return f.supported[tool] }

func (f *fakeGarden) Status(context.Context) (garden.StatusResult, error) { return f.status, nil }
func (f *fakeGarden) Send(_ context.Context, a garden.SendArgs) (garden.SendResult, error) {
	f.sent = a
	return garden.SendResult{Message: garden.Message{ID: "reply", Binding: f.status.Binding}}, nil
}
func (f *fakeGarden) Heartbeat(_ context.Context, a garden.HeartbeatArgs) (garden.HeartbeatResult, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.heartbeats = append(f.heartbeats, a)
	if f.holder != "" && f.holder != a.ConsumerID {
		return garden.HeartbeatResult{}, &garden.Error{Code: "inbox_busy", Message: "another adapter owns this inbox lease"}
	}
	f.holder = a.ConsumerID
	return garden.HeartbeatResult{Deliverable: a.Deliverable}, nil
}
func (f *fakeGarden) Who(context.Context) (garden.WhoResult, error) { return f.who, nil }
func (f *fakeGarden) Read(context.Context, garden.ReadArgs) (garden.ReadResult, error) {
	return garden.ReadResult{Messages: []garden.Message{}}, nil
}
func (f *fakeGarden) Poll(context.Context, garden.PollArgs) (garden.PollResult, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.polls++
	if f.pollErr != nil {
		return garden.PollResult{}, f.pollErr
	}
	return f.poll, nil
}
func (f *fakeGarden) Acknowledge(context.Context, garden.AckArgs) error { f.acks++; return nil }

func TestRemoteToolsPreserveExplicitRecipientsAndCannotOverrideIdentity(t *testing.T) {
	p := Profile{InstanceID: "instance", Scope: gardenauth.Scope{Realm: "r", Segments: []gardenauth.Segment{}}, Classification: "internal", Participant: "Val"}
	f := &fakeGarden{status: garden.StatusResult{Participant: "Val", Participants: []string{"Val", "Spike"}, Binding: garden.Binding{InstanceID: p.InstanceID, Scope: p.Scope, Classification: p.Classification}}}
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

func TestRemoteNormalisesRecipientNamesToRosterParticipants(t *testing.T) {
	p := Profile{InstanceID: "instance", Scope: gardenauth.Scope{Realm: "r", Segments: []gardenauth.Segment{}}, Classification: "internal", Participant: "Spike"}
	b := garden.Binding{InstanceID: p.InstanceID, Scope: p.Scope, Classification: p.Classification}
	f := &fakeGarden{status: garden.StatusResult{Participant: "Spike", Participants: []string{"Spike", "Val", "val2", "val_3"}, Binding: b}}
	remote := &Remote{api: f, profile: p, resolve: true}
	for input, want := range map[string]string{"Val": "Val", "spike": "Spike", "Val 2": "val2", "val-2": "val2", "VAL_2": "val2", "val 3": "val_3"} {
		f.sent = garden.SendArgs{}
		if _, err := remote.Call(context.Background(), "send_message", json.RawMessage(`{"content":"hi","recipients":["`+input+`"]}`)); err != nil {
			t.Fatalf("%q refused: %v", input, err)
		}
		if len(f.sent.Recipients) != 1 || f.sent.Recipients[0] != want {
			t.Fatalf("%q sent as %#v, want %q", input, f.sent.Recipients, want)
		}
	}
}

func TestRemoteUnknownOrAmbiguousRecipientListsKnownNames(t *testing.T) {
	p := Profile{InstanceID: "instance", Scope: gardenauth.Scope{Realm: "r", Segments: []gardenauth.Segment{}}, Classification: "internal", Participant: "Spike"}
	b := garden.Binding{InstanceID: p.InstanceID, Scope: p.Scope, Classification: p.Classification}
	f := &fakeGarden{status: garden.StatusResult{Participant: "Spike", Participants: []string{"Spike", "val2", "val_2"}, Binding: b}}
	remote := &Remote{api: f, profile: p, resolve: true}
	for _, input := range []string{"Zed", "Val-2"} {
		f.sent = garden.SendArgs{}
		_, err := remote.Call(context.Background(), "send_message", json.RawMessage(`{"content":"hi","recipients":["`+input+`"]}`))
		var safe *SafeError
		if !errors.As(err, &safe) {
			t.Fatalf("%q not a safe error: %v", input, err)
		}
		if !strings.Contains(safe.Message, "known names: Spike, val2, val_2") {
			t.Fatalf("%q does not list known names: %q", input, safe.Message)
		}
		if f.sent.Content != "" {
			t.Fatalf("%q sent despite bad recipient", input)
		}
	}
}

func TestProfileConnectionPassesRecipientsUntouched(t *testing.T) {
	p := Profile{InstanceID: "instance", Scope: gardenauth.Scope{Realm: "r", Segments: []gardenauth.Segment{}}, Classification: "internal", Participant: "Spike"}
	b := garden.Binding{InstanceID: p.InstanceID, Scope: p.Scope, Classification: p.Classification}
	f := &fakeGarden{status: garden.StatusResult{Participant: "Spike", Participants: []string{"Spike", "val2", "val-2"}, Binding: b}}
	remote := &Remote{api: f, profile: p}
	for _, input := range []string{"val2", "val-2", "Val 2"} {
		f.sent = garden.SendArgs{}
		if _, err := remote.Call(context.Background(), "send_message", json.RawMessage(`{"content":"hi","recipients":["`+input+`"]}`)); err != nil {
			t.Fatalf("%q refused on a profile connection: %v", input, err)
		}
		if len(f.sent.Recipients) != 1 || f.sent.Recipients[0] != input {
			t.Fatalf("%q rewritten to %#v on a profile connection", input, f.sent.Recipients)
		}
	}
}

func TestHarnessConnectionPrefersExactNamesAndFlagsDisplayFormAmbiguity(t *testing.T) {
	p := Profile{InstanceID: "instance", Scope: gardenauth.Scope{Realm: "r", Segments: []gardenauth.Segment{}}, Classification: "internal", Participant: "Spike"}
	b := garden.Binding{InstanceID: p.InstanceID, Scope: p.Scope, Classification: p.Classification}
	f := &fakeGarden{status: garden.StatusResult{Participant: "Spike", Participants: []string{"Spike", "val2", "val-2"}, Binding: b}}
	remote := &Remote{api: f, profile: p, resolve: true}
	for _, want := range []string{"val2", "val-2"} {
		f.sent = garden.SendArgs{}
		if _, err := remote.Call(context.Background(), "send_message", json.RawMessage(`{"content":"hi","recipients":["`+want+`"]}`)); err != nil {
			t.Fatalf("exact name %q refused: %v", want, err)
		}
		if len(f.sent.Recipients) != 1 || f.sent.Recipients[0] != want {
			t.Fatalf("exact name %q sent as %#v", want, f.sent.Recipients)
		}
	}
	f.sent = garden.SendArgs{}
	_, err := remote.Call(context.Background(), "send_message", json.RawMessage(`{"content":"hi","recipients":["Val 2"]}`))
	var safe *SafeError
	if !errors.As(err, &safe) || !strings.Contains(safe.Message, "ambiguous") || !strings.Contains(safe.Message, "known names: Spike, val2, val-2") {
		t.Fatalf("display form not reported as ambiguous: %v", err)
	}
	if f.sent.Content != "" {
		t.Fatal("ambiguous recipient was sent")
	}
}
