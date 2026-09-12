package stdio

import (
	"bytes"
	"encoding/json"
	"fmt"
	"strings"
	"sync"
	"testing"
)

func TestReaderReturnsOneJSONMessagePerLine(t *testing.T) {
	r := NewReader(strings.NewReader("{\"jsonrpc\":\"2.0\",\"id\":1}\n{\"jsonrpc\":\"2.0\",\"id\":2}\n"))
	first, err := r.Read()
	if err != nil {
		t.Fatal(err)
	}
	second, err := r.Read()
	if err != nil {
		t.Fatal(err)
	}
	if string(first) != `{"jsonrpc":"2.0","id":1}` || string(second) != `{"jsonrpc":"2.0","id":2}` {
		t.Fatalf("messages = %s, %s", first, second)
	}
}

func TestReaderRejectsMalformedJSON(t *testing.T) {
	r := NewReader(strings.NewReader("{broken}\n"))
	if _, err := r.Read(); err == nil {
		t.Fatal("Read() accepted malformed JSON")
	}
}

func TestReaderAcceptsMaxMessageBytes(t *testing.T) {
	message := `"` + strings.Repeat("a", MaxMessageBytes-2) + `"`
	r := NewReader(strings.NewReader(message + "\n"))
	got, err := r.Read()
	if err != nil {
		t.Fatal(err)
	}
	if len(got) != MaxMessageBytes {
		t.Fatalf("message length = %d, want %d", len(got), MaxMessageBytes)
	}
}

func TestReaderRejectsMessageOverMaxMessageBytes(t *testing.T) {
	message := `"` + strings.Repeat("a", MaxMessageBytes-1) + `"`
	r := NewReader(strings.NewReader(message + "\n"))
	if _, err := r.Read(); err == nil {
		t.Fatal("Read() accepted a message exceeding MaxMessageBytes")
	}
}

func TestWriterSerialisesConcurrentMessages(t *testing.T) {
	var dst bytes.Buffer
	w := NewWriter(&dst)
	var wg sync.WaitGroup
	for i := 0; i < 20; i++ {
		wg.Add(1)
		go func(id int) {
			defer wg.Done()
			if err := w.Write(json.RawMessage(fmt.Sprintf(`{"id":%d}`, id))); err != nil {
				t.Errorf("Write() error = %v", err)
			}
		}(i)
	}
	wg.Wait()
	for _, line := range strings.Split(strings.TrimSpace(dst.String()), "\n") {
		if !json.Valid([]byte(line)) {
			t.Fatalf("interleaved output line %q", line)
		}
	}
}

func TestWriterCompactsJSONToOneNDJSONLine(t *testing.T) {
	message := json.RawMessage("{\n  \"jsonrpc\": \"2.0\",\n  \"id\": 1\n}")
	var dst bytes.Buffer

	if err := NewWriter(&dst).Write(message); err != nil {
		t.Fatal(err)
	}
	if got, want := dst.String(), "{\"jsonrpc\":\"2.0\",\"id\":1}\n"; got != want {
		t.Fatalf("output = %q, want %q", got, want)
	}
}

func TestWriterAcceptsExactMaxMessageBytes(t *testing.T) {
	message := json.RawMessage(`"` + strings.Repeat("a", MaxMessageBytes-2) + `"`)
	var dst bytes.Buffer

	if err := NewWriter(&dst).Write(message); err != nil {
		t.Fatal(err)
	}
	if got, want := dst.Len(), MaxMessageBytes+1; got != want {
		t.Fatalf("output bytes = %d, want %d including newline", got, want)
	}
}

func TestWriterRejectsCompactedMessageOverMaxWithoutWriting(t *testing.T) {
	message := json.RawMessage(`"` + strings.Repeat("a", MaxMessageBytes-1) + `"`)
	var dst bytes.Buffer

	if err := NewWriter(&dst).Write(message); err == nil {
		t.Fatal("Write() accepted a compacted message exceeding MaxMessageBytes")
	}
	if dst.Len() != 0 {
		t.Fatalf("destination changed after oversized write: %d bytes", dst.Len())
	}
}

func TestWriterRejectsInvalidJSONWithoutWriting(t *testing.T) {
	var dst bytes.Buffer
	w := NewWriter(&dst)
	if err := w.Write(json.RawMessage("{broken}")); err == nil {
		t.Fatal("Write() accepted malformed JSON")
	}
	if dst.Len() != 0 {
		t.Fatalf("destination changed after invalid write: %q", dst.String())
	}
}
