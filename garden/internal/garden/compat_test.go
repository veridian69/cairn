package garden

import (
	"bytes"
	"context"
	"encoding/json"
	"reflect"
	"sort"
	"strings"
	"testing"

	"github.com/google/uuid"
	"github.com/modelcontextprotocol/go-sdk/mcp"
)

// The structs below are the response shapes clients built before delivery reporting.
// They are decoded with DisallowUnknownFields, as the pre-upgrade client does.
type legacyStatusResult struct {
	Binding      Binding  `json:"binding"`
	Participant  string   `json:"participant"`
	Generation   string   `json:"generation"`
	Participants []string `json:"participants"`
}

// callRaw calls a tool through the MCP session and decodes its structured content strictly
// into out, exactly as a client that predates this change would.
func callRaw(t *testing.T, c *Client, name string, args any, out any) {
	t.Helper()
	result, err := c.session.CallTool(context.Background(), &mcp.CallToolParams{Name: name, Arguments: args})
	if err != nil {
		t.Fatalf("%s: %v", name, err)
	}
	if result.IsError {
		t.Fatalf("%s returned an error result: %+v", name, result.Content)
	}
	raw, err := json.Marshal(result.StructuredContent)
	if err != nil {
		t.Fatal(err)
	}
	dec := json.NewDecoder(bytes.NewReader(raw))
	dec.DisallowUnknownFields()
	if err := dec.Decode(out); err != nil {
		t.Fatalf("%s response does not decode with the pre-upgrade shape: %v\n%s", name, err, raw)
	}
}

func jsonKeys(t *testing.T, v any) []string {
	t.Helper()
	raw, err := json.Marshal(v)
	if err != nil {
		t.Fatal(err)
	}
	var fields map[string]json.RawMessage
	if err := json.Unmarshal(raw, &fields); err != nil {
		t.Fatal(err)
	}
	keys := make([]string, 0, len(fields))
	for k := range fields {
		keys = append(keys, k)
	}
	return keys
}

func TestPreUpgradeClientsDecodeNewGatewayResponses(t *testing.T) {
	f := newFixture(t)
	ctx := context.Background()
	alice := f.client("alice")

	var status legacyStatusResult
	callRaw(t, alice, "status", struct{}{}, &status)
	if status.Participant != "alice" || len(status.Participants) != 2 {
		t.Fatalf("legacy status = %+v", status)
	}

	// A send without report_delivery returns exactly the plain message shape.
	var sent Message
	callRaw(t, alice, "send_message", SendArgs{Content: "legacy send", Recipients: []string{"bob"}}, &sent)
	if sent.Content != "legacy send" || sent.ID == "" {
		t.Fatalf("legacy send = %+v", sent)
	}
	raw, err := alice.session.CallTool(ctx, &mcp.CallToolParams{Name: "send_message", Arguments: SendArgs{Content: "shape", Recipients: []string{"bob"}}})
	if err != nil {
		t.Fatal(err)
	}
	if strings.Contains(strings.Join(jsonKeys(t, raw.StructuredContent), ","), "deliveries") {
		t.Fatal("plain send leaked the per-recipient deliveries field")
	}
	if !reflect.DeepEqual(sorted(jsonKeys(t, raw.StructuredContent)), sorted(jsonKeys(t, Message{}))) {
		t.Fatalf("plain send keys = %v, want the Message keys", jsonKeys(t, raw.StructuredContent))
	}

	// The pre-upgrade poll path still decodes its response.
	bob := f.client("bob")
	var polled PollResult
	callRaw(t, bob, "poll_inbox", PollArgs{ConsumerID: uuid.NewString()}, &polled)
	if polled.Message == nil || polled.Message.Content != "legacy send" {
		t.Fatalf("legacy poll = %+v", polled)
	}
}

func TestNewClientOptsInToDeliveryReportingOnlyWhenAdvertised(t *testing.T) {
	f := newFixture(t)
	ctx := context.Background()
	alice := f.client("alice")
	if !alice.Supports("heartbeat") || !alice.Supports("who") {
		t.Fatal("new gateway tools not advertised")
	}
	sent, err := alice.Send(ctx, SendArgs{Content: "reported", Recipients: []string{"bob"}})
	if err != nil {
		t.Fatal(err)
	}
	if len(sent.Deliveries) != 1 || sent.Deliveries[0].Recipient != "bob" {
		t.Fatalf("new client send deliveries = %+v", sent.Deliveries)
	}

	// Against a gateway that does not advertise heartbeat, the client sends no report flag
	// and reads the plain message shape into SendResult.Message.
	legacy := f.client("alice")
	legacy.tools = map[string]bool{"send_message": true, "status": true}
	plain, err := legacy.Send(ctx, SendArgs{Content: "plain", Recipients: []string{"bob"}})
	if err != nil || plain.ID == "" || plain.Deliveries != nil {
		t.Fatalf("send with older tool list = %+v, %v", plain, err)
	}
	if _, err := legacy.Heartbeat(ctx, HeartbeatArgs{ConsumerID: uuid.NewString()}); err == nil {
		t.Fatal("heartbeat issued against a gateway without it")
	}
}

func sorted(keys []string) []string {
	out := append([]string(nil), keys...)
	sort.Strings(out)
	return out
}
