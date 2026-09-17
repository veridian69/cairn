package upstream

import (
	"bufio"
	"bytes"
	"encoding/json"
	"errors"
	"io"
	"strings"
)

const (
	maxSSEDataEventSize = 4 << 20
	maxSSEScanTokenSize = maxSSEDataEventSize + 64*1024
)

func readSSE(src io.Reader, emit func(json.RawMessage) error) error {
	return readSSEWithCursor(src, nil, emit)
}

// The cursor is advanced by the caller only after it accepts an event. A
// partial event or a failed emit must not acknowledge undelivered data.
func readSSEWithCursor(src io.Reader, cursor *string, emit func(json.RawMessage) error) error {
	eventID := ""
	if cursor != nil {
		eventID = *cursor
	}
	scanner := bufio.NewScanner(src)
	scanner.Buffer(make([]byte, 64<<10), maxSSEScanTokenSize)

	var data bytes.Buffer
	haveData := false
	flush := func() error {
		if !haveData {
			return nil
		}
		payload := bytes.TrimSpace(data.Bytes())
		if !json.Valid(payload) {
			return errors.New("invalid JSON in SSE data event")
		}
		message := bytes.Clone(payload)
		data.Reset()
		haveData = false
		if err := emit(message); err != nil {
			return err
		}
		if cursor != nil {
			*cursor = eventID
		}
		return nil
	}

	for scanner.Scan() {
		line := strings.TrimSuffix(scanner.Text(), "\r")
		if line == "" {
			if err := flush(); err != nil {
				return err
			}
			continue
		}
		if cursor != nil && (line == "id" || strings.HasPrefix(line, "id:")) {
			value := strings.TrimPrefix(strings.TrimPrefix(line, "id"), ":")
			value = strings.TrimPrefix(value, " ")
			if strings.ContainsRune(value, '\x00') {
				continue // SSE ignores IDs containing NUL.
			}
			if len(value) > 4096 || strings.IndexFunc(value, func(r rune) bool { return r < 0x20 || r == 0x7f }) >= 0 {
				return errors.New("invalid SSE event ID")
			}
			eventID = value
			continue
		}
		if !strings.HasPrefix(line, "data:") {
			continue
		}

		value := strings.TrimPrefix(line, "data:")
		value = strings.TrimPrefix(value, " ")
		additional := len(value)
		if haveData {
			additional++
		}
		if additional > maxSSEDataEventSize-data.Len() {
			return errors.New("SSE data event exceeds 4 MiB limit")
		}
		if haveData {
			_ = data.WriteByte('\n')
		}
		_, _ = data.WriteString(value)
		haveData = true
	}
	if err := scanner.Err(); err != nil {
		return err
	}
	if cursor != nil {
		return nil // An interrupted listener event must be replayed, not acknowledged.
	}
	return flush()
}
