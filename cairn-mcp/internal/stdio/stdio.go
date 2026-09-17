package stdio

import (
	"bufio"
	"bytes"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"sync"
)

const MaxMessageBytes = 4 << 20

type Reader struct {
	scanner *bufio.Scanner
}

func NewReader(src io.Reader) *Reader {
	s := bufio.NewScanner(src)
	s.Buffer(make([]byte, 64<<10), MaxMessageBytes+1)
	return &Reader{scanner: s}
}

func (r *Reader) Read() (json.RawMessage, error) {
	if !r.scanner.Scan() {
		if err := r.scanner.Err(); err != nil {
			return nil, fmt.Errorf("read stdio message: %w", err)
		}
		return nil, io.EOF
	}
	b := bytes.TrimSpace(r.scanner.Bytes())
	if len(b) == 0 || len(b) > MaxMessageBytes || !json.Valid(b) {
		return nil, errors.New("invalid JSON-RPC line on stdin")
	}
	return bytes.Clone(b), nil
}

type Writer struct {
	dst io.Writer
	mu  sync.Mutex
}

func NewWriter(dst io.Writer) *Writer {
	return &Writer{dst: dst}
}

func (w *Writer) Write(message json.RawMessage) error {
	frame, err := frameMessage(message)
	if err != nil {
		return err
	}

	w.mu.Lock()
	defer w.mu.Unlock()

	n, err := w.dst.Write(frame)
	if err != nil {
		return fmt.Errorf("write stdio message: %w", err)
	}
	if n != len(frame) {
		return io.ErrShortWrite
	}
	return nil
}

func frameMessage(message json.RawMessage) ([]byte, error) {
	if !json.Valid(message) {
		return nil, errors.New("invalid JSON-RPC message")
	}

	capacity := len(message)
	if capacity > MaxMessageBytes {
		capacity = MaxMessageBytes
	}
	frame := make([]byte, 0, capacity+1)
	inString := false
	escaped := false
	for _, current := range message {
		if !inString && isJSONSpace(current) {
			continue
		}
		if len(frame) == MaxMessageBytes {
			return nil, errors.New("compacted JSON-RPC message exceeds 4 MiB limit")
		}
		frame = append(frame, current)

		if !inString {
			inString = current == '"'
			continue
		}
		if escaped {
			escaped = false
			continue
		}
		switch current {
		case '\\':
			escaped = true
		case '"':
			inString = false
		}
	}
	return append(frame, '\n'), nil
}

func isJSONSpace(value byte) bool {
	return value == ' ' || value == '\t' || value == '\r' || value == '\n'
}
