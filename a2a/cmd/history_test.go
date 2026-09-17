package cmd

import (
	"bytes"
	"io"
	"os"
	"strings"
	"testing"
	"time"

	"github.com/veridian69/cairn/a2a/internal/model"
)

func TestRenderContentHonorsRawAndRedaction(t *testing.T) {
	msg := model.Message{ID: "msg-1", Content: "original content"}
	reasons := map[string]string{"msg-1": "secret"}

	if got := renderContent(msg, reasons, false); got != "[redacted: secret]" {
		t.Fatalf("renderContent() = %q, want %q", got, "[redacted: secret]")
	}
	if got := renderContent(msg, reasons, true); got != "original content" {
		t.Fatalf("renderContent(raw) = %q, want %q", got, "original content")
	}
}

func TestPrintThreadShowsAncestorChainAndDescendants(t *testing.T) {
	root := model.Message{
		ID:         "root-message-id",
		AuthorName: "operator",
		Content:    "root content",
		CreatedAt:  time.Date(2026, 3, 29, 10, 0, 0, 0, time.UTC),
	}
	replyToRoot := root.ID
	selected := model.Message{
		ID:         "selected-message-id",
		AuthorName: "claude",
		Content:    "selected content",
		ReplyTo:    &replyToRoot,
		CreatedAt:  time.Date(2026, 3, 29, 10, 1, 0, 0, time.UTC),
	}
	replyToSelected := selected.ID
	child := model.Message{
		ID:         "child-message-id",
		AuthorName: "gpt",
		Content:    "child content",
		ReplyTo:    &replyToSelected,
		CreatedAt:  time.Date(2026, 3, 29, 10, 2, 0, 0, time.UTC),
	}
	sibling := model.Message{
		ID:         "sibling-message-id",
		AuthorName: "other",
		Content:    "sibling content",
		ReplyTo:    &replyToRoot,
		CreatedAt:  time.Date(2026, 3, 29, 10, 3, 0, 0, time.UTC),
	}

	output := captureStdout(t, func() {
		err := printThread([]model.Message{child, root, sibling, selected}, selected.ID, map[string]string{
			child.ID: "sensitive",
		}, false)
		if err != nil {
			t.Fatalf("printThread returned error: %v", err)
		}
	})

	rootPos := strings.Index(output, "root-mes")
	selectedPos := strings.Index(output, "selected")
	childPos := strings.Index(output, "child-me")
	if rootPos == -1 || selectedPos == -1 || childPos == -1 {
		t.Fatalf("thread output missing expected messages: %s", output)
	}
	if !(rootPos < selectedPos && selectedPos < childPos) {
		t.Fatalf("thread output was not in ancestor-then-descendant order: %s", output)
	}
	if strings.Contains(output, "sibling-message") {
		t.Fatalf("thread output should not include sibling replies: %s", output)
	}
	if !strings.Contains(output, "[redacted: sensitive]") {
		t.Fatalf("thread output should render redacted child content: %s", output)
	}
	if !strings.Contains(output, "  [2026-03-29 12:01:00]") {
		t.Fatalf("selected reply should be indented one level: %s", output)
	}
	if !strings.Contains(output, "    [2026-03-29 12:02:00]") {
		t.Fatalf("child reply should be indented two levels: %s", output)
	}
}

func TestPrintThreadErrorsForUnknownMessage(t *testing.T) {
	err := printThread(nil, "missing", nil, false)
	if err == nil {
		t.Fatal("printThread should fail for an unknown thread root")
	}
	if !strings.Contains(err.Error(), `thread message "missing" not found`) {
		t.Fatalf("unexpected error: %v", err)
	}
}

func captureStdout(t *testing.T, fn func()) string {
	t.Helper()
	originalStdout := os.Stdout
	reader, writer, err := os.Pipe()
	if err != nil {
		t.Fatalf("creating pipe: %v", err)
	}
	os.Stdout = writer
	defer func() {
		os.Stdout = originalStdout
	}()

	fn()

	if err := writer.Close(); err != nil {
		t.Fatalf("closing writer: %v", err)
	}

	var buf bytes.Buffer
	if _, err := io.Copy(&buf, reader); err != nil {
		t.Fatalf("reading captured stdout: %v", err)
	}
	if err := reader.Close(); err != nil {
		t.Fatalf("closing reader: %v", err)
	}
	return buf.String()
}
