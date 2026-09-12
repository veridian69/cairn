package upstream

import (
	"encoding/json"
	"errors"
	"strings"
	"testing"
)

func TestReadSSEEmitsDataEvents(t *testing.T) {
	input := strings.NewReader(": keepalive\r\nid: 7\r\ndata: {\"jsonrpc\":\"2.0\",\r\ndata: \"method\":\"notifications/tools/list_changed\"}\r\n\r\ndata: {\"jsonrpc\":\"2.0\",\"id\":2,\"result\":{}}")
	var got []string
	err := readSSE(input, func(msg json.RawMessage) error {
		got = append(got, string(msg))
		return nil
	})
	if err != nil {
		t.Fatal(err)
	}
	want := []string{
		"{\"jsonrpc\":\"2.0\",\n\"method\":\"notifications/tools/list_changed\"}",
		"{\"jsonrpc\":\"2.0\",\"id\":2,\"result\":{}}",
	}
	if len(got) != len(want) {
		t.Fatalf("events = %v, want %v", got, want)
	}
	for i := range want {
		if got[i] != want[i] {
			t.Fatalf("event %d = %q, want %q", i, got[i], want[i])
		}
	}
}

func TestReadSSERejectsInvalidJSONData(t *testing.T) {
	err := readSSE(strings.NewReader("data: {broken}\n\n"), func(json.RawMessage) error { return nil })
	if err == nil {
		t.Fatal("readSSE() accepted invalid JSON")
	}
}

func TestReadSSEAcceptsExactFourMiBMultilinePayload(t *testing.T) {
	const payloadSize = 4 << 20
	middle := `"` + strings.Repeat("x", payloadSize-6) + `"`
	input := "data: [\r\ndata: " + middle + "\r\ndata: ]\r\n\r\n"
	var got int
	err := readSSE(strings.NewReader(input), func(msg json.RawMessage) error {
		got = len(msg)
		return nil
	})
	if err != nil {
		t.Fatal(err)
	}
	if got != payloadSize {
		t.Fatalf("payload size = %d, want %d", got, payloadSize)
	}
}

func TestReadSSERejectsPayloadOverFourMiB(t *testing.T) {
	const payloadSize = 4 << 20
	payload := `"` + strings.Repeat("x", payloadSize-1) + `"`
	err := readSSE(strings.NewReader("data: "+payload+"\n\n"), func(json.RawMessage) error { return nil })
	if err == nil {
		t.Fatal("readSSE() accepted a data event larger than 4 MiB")
	}
}

func TestReadSSEKeepsIgnoringIDsOnPOSTStreams(t *testing.T) {
	input := "id: unsafe\theader\ndata: {}\n\n"
	var received int
	if err := readSSE(strings.NewReader(input), func(json.RawMessage) error { received++; return nil }); err != nil {
		t.Fatal(err)
	}
	if received != 1 {
		t.Fatalf("received %d events", received)
	}
}

func TestListenerSSEDoesNotAcknowledgeIncompleteEvent(t *testing.T) {
	var received int
	cursor := "previous"
	err := readSSEWithCursor(strings.NewReader("id: incomplete\ndata: {}\n"), &cursor, func(json.RawMessage) error { received++; return nil })
	if err != nil {
		t.Fatal(err)
	}
	if received != 0 || cursor != "previous" {
		t.Fatal("acknowledged an event without its terminating blank line")
	}
}

func TestListenerCursorTracksAcceptedEventsOnly(t *testing.T) {
	for _, tc := range []struct {
		name, input, want string
		reject            bool
	}{
		{"id retained", "id: one\ndata: {}\n\ndata: {}\n\n", "one", false},
		{"id cleared", "id: one\ndata: {}\n\nid:\ndata: {}\n\n", "", false},
		{"NUL ignored", "id: unsafe\x00value\ndata: {}\n\n", "previous", false},
		{"emit rejected", "id: next\ndata: {}\n\n", "previous", true},
	} {
		t.Run(tc.name, func(t *testing.T) {
			cursor := "previous"
			err := readSSEWithCursor(strings.NewReader(tc.input), &cursor, func(json.RawMessage) error {
				if tc.reject {
					return errors.New("consumer closed")
				}
				return nil
			})
			if (err != nil) != tc.reject {
				t.Fatalf("unexpected error: %v", err)
			}
			if cursor != tc.want {
				t.Fatalf("cursor = %q, want %q", cursor, tc.want)
			}
		})
	}
}

func TestListenerSSERejectsUnsafeCursor(t *testing.T) {
	for _, value := range []string{"tab\tvalue", strings.Repeat("x", 4097)} {
		cursor := "previous"
		err := readSSEWithCursor(strings.NewReader("id: "+value+"\ndata: {}\n\n"), &cursor, func(json.RawMessage) error {
			t.Fatal("emitted event with unusable resume cursor")
			return nil
		})
		if err == nil || cursor != "previous" {
			t.Fatal("accepted unusable cursor")
		}
	}
}
