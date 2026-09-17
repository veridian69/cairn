package chat

import (
	"testing"
	"time"

	"github.com/veridian69/cairn/a2a/internal/model"
)

func testMessage(id, author, content string, createdAt time.Time, replyTo *string) model.Message {
	return model.Message{
		ID:         id,
		AuthorID:   author + "-id",
		AuthorName: author,
		Content:    content,
		ReplyTo:    replyTo,
		Metadata:   map[string]any{},
		CreatedAt:  createdAt,
	}
}

func TestStateAppendAndSelection(t *testing.T) {
	base := time.Date(2026, 3, 29, 12, 0, 0, 0, time.UTC)
	events := []StreamEvent{
		{Message: testMessage("m1", "claude", "one", base, nil), Seq: 1},
		{Message: testMessage("m2", "gemini", "two", base.Add(time.Minute), nil), Seq: 2},
	}
	st := NewState(events, "operator")

	if got := st.SelectedIndex(); got != 1 {
		t.Fatalf("SelectedIndex = %d, want 1", got)
	}
	if !st.AutoFollow() {
		t.Fatal("AutoFollow should start true")
	}
	if st.CurrentIdentity() != "operator" {
		t.Fatalf("CurrentIdentity = %q, want operator", st.CurrentIdentity())
	}

	st.MoveSelection(-1)
	if st.AutoFollow() {
		t.Fatal("AutoFollow should be false after moving off bottom")
	}

	st.Append(StreamEvent{Message: testMessage("m3", "claude", "three", base.Add(2*time.Minute), nil), Seq: 3})
	if got := st.UnseenCount(); got != 1 {
		t.Fatalf("UnseenCount = %d, want 1", got)
	}
	if got := st.SelectedIndex(); got != 0 {
		t.Fatalf("SelectedIndex = %d, want 0 while paused", got)
	}

	st.JumpToBottom()
	if !st.AutoFollow() {
		t.Fatal("AutoFollow should be true after JumpToBottom")
	}
	if got := st.UnseenCount(); got != 0 {
		t.Fatalf("UnseenCount = %d, want 0 after JumpToBottom", got)
	}
	if got := st.SelectedIndex(); got != 2 {
		t.Fatalf("SelectedIndex = %d, want 2 after JumpToBottom", got)
	}
}

func TestStateBuildThreadContextAndReplies(t *testing.T) {
	base := time.Date(2026, 3, 29, 12, 0, 0, 0, time.UTC)
	root := testMessage("root", "claude", "root", base, nil)
	replyToRoot := root.ID
	selected := testMessage("selected", "gemini", "selected", base.Add(time.Minute), &replyToRoot)
	replyToSelected := selected.ID
	childA := testMessage("child-a", "claude", "child-a", base.Add(2*time.Minute), &replyToSelected)
	childB := testMessage("child-b", "operator", "child-b", base.Add(3*time.Minute), &replyToSelected)
	replyToChildA := childA.ID
	grandchild := testMessage("grandchild", "gemini", "grandchild", base.Add(4*time.Minute), &replyToChildA)

	st := NewState([]StreamEvent{
		{Message: root, Seq: 1},
		{Message: selected, Seq: 2},
		{Message: childB, Seq: 3},
		{Message: childA, Seq: 4},
		{Message: grandchild, Seq: 5},
	}, "operator")

	st.MoveSelection(-3) // from grandchild to selected
	view, ok := st.BuildThread(8)
	if !ok {
		t.Fatal("BuildThread returned false")
	}
	if view.SelectedID != "selected" {
		t.Fatalf("SelectedID = %q, want selected", view.SelectedID)
	}
	if len(view.Context) != 2 {
		t.Fatalf("context len = %d, want 2", len(view.Context))
	}
	if view.Context[0].Message.ID != "root" || view.Context[0].Depth != 0 {
		t.Fatalf("unexpected context[0]: %+v", view.Context[0])
	}
	if view.Context[1].Message.ID != "selected" || view.Context[1].Depth != 1 {
		t.Fatalf("unexpected context[1]: %+v", view.Context[1])
	}
	if len(view.Replies) != 3 {
		t.Fatalf("replies len = %d, want 3", len(view.Replies))
	}
	if view.Replies[0].Message.ID != "child-a" || view.Replies[0].Depth != 1 {
		t.Fatalf("unexpected replies[0]: %+v", view.Replies[0])
	}
	if view.Replies[1].Message.ID != "grandchild" || view.Replies[1].Depth != 2 {
		t.Fatalf("unexpected replies[1]: %+v", view.Replies[1])
	}
	if view.Replies[2].Message.ID != "child-b" || view.Replies[2].Depth != 1 {
		t.Fatalf("unexpected replies[2]: %+v", view.Replies[2])
	}
}

func TestStateBuildThreadMissingParent(t *testing.T) {
	base := time.Date(2026, 3, 29, 12, 0, 0, 0, time.UTC)
	missing := "missing-parent"
	selected := testMessage("selected", "claude", "selected", base, &missing)
	st := NewState([]StreamEvent{{Message: selected, Seq: 1}}, "operator")

	view, ok := st.BuildThread(8)
	if !ok {
		t.Fatal("BuildThread returned false")
	}
	if len(view.Context) != 2 {
		t.Fatalf("context len = %d, want 2", len(view.Context))
	}
	if view.Context[0].Message.ID != "selected" {
		t.Fatalf("unexpected selected context item: %+v", view.Context[0])
	}
	if !view.Context[1].IsMissing || view.Context[1].MissingID != "missing-parent" {
		t.Fatalf("unexpected missing context item: %+v", view.Context[1])
	}
}

func TestStateFocusAndThreadToggle(t *testing.T) {
	base := time.Date(2026, 3, 29, 12, 0, 0, 0, time.UTC)
	st := NewState([]StreamEvent{{Message: testMessage("m1", "claude", "one", base, nil), Seq: 1}}, "operator")

	if st.Focus() != FocusInput {
		t.Fatalf("Focus = %q, want input", st.Focus())
	}
	st.ToggleFocus()
	if st.Focus() != FocusStream {
		t.Fatalf("Focus = %q, want stream", st.Focus())
	}
	if !st.OpenThread() {
		t.Fatal("OpenThread should succeed")
	}
	if !st.ThreadOpen() || st.Focus() != FocusThread {
		t.Fatalf("thread state unexpected: open=%v focus=%q", st.ThreadOpen(), st.Focus())
	}
	st.CloseThread()
	if st.ThreadOpen() {
		t.Fatal("ThreadOpen should be false after CloseThread")
	}
	if st.Focus() != FocusStream {
		t.Fatalf("Focus = %q, want stream after CloseThread", st.Focus())
	}
}

func TestStateSelectionEdgeCasesAndAutoFollowSetter(t *testing.T) {
	st := NewState(nil, "operator")
	if changed := st.SetSelectedIndex(3); changed {
		t.Fatal("SetSelectedIndex on empty state should report unchanged")
	}
	if st.SelectedIndex() != -1 {
		t.Fatalf("SelectedIndex on empty state = %d, want -1", st.SelectedIndex())
	}

	base := time.Date(2026, 3, 29, 12, 0, 0, 0, time.UTC)
	st = NewState([]StreamEvent{
		{Message: testMessage("m1", "claude", "one", base, nil), Seq: 1},
		{Message: testMessage("m2", "operator", "two", base.Add(time.Minute), nil), Seq: 2},
	}, "operator")
	st.SetSelectedIndex(0)
	if st.AutoFollow() {
		t.Fatal("AutoFollow should be false after selecting away from bottom")
	}
	st.SetAutoFollow(true)
	if !st.AutoFollow() {
		t.Fatal("SetAutoFollow(true) should enable auto-follow")
	}
	if st.SelectedIndex() != 1 {
		t.Fatalf("SelectedIndex after SetAutoFollow(true) = %d, want 1", st.SelectedIndex())
	}
}
