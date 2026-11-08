package garden

import (
	"context"
	"strings"
	"testing"

	"github.com/google/uuid"
	"github.com/veridian69/cairn/garden/internal/model"
)

// publishLocal appends count messages of the given content to the fixture's stream and returns their IDs.
func publishLocal(t *testing.T, f *fixture, count int, content string) []string {
	t.Helper()
	ids := make([]string, 0, count)
	for i := 0; i < count; i++ {
		m := model.NewMessage(model.Participant{ID: uuid.NewString(), Name: "local"}, content, nil)
		if err := f.stream.Publish(context.Background(), m); err != nil {
			t.Fatal(err)
		}
		ids = append(ids, m.ID)
	}
	return ids
}

func TestReadTailReturnsNewestAscendingWithCursor(t *testing.T) {
	f := newFixture(t)
	ctx := context.Background()
	c := f.client("alice")
	publishLocal(t, f, 12, "tail")
	p, err := f.stream.Position(ctx)
	if err != nil {
		t.Fatal(err)
	}
	all, err := c.Read(ctx, ReadArgs{Limit: 100})
	if err != nil {
		t.Fatal(err)
	}
	for _, tc := range []struct {
		tail int
		want int
	}{{1, 1}, {5, 5}, {100, 12}} {
		out, err := c.Read(ctx, ReadArgs{Tail: tc.tail})
		if err != nil {
			t.Fatalf("tail %d: %v", tc.tail, err)
		}
		if len(out.Messages) != tc.want {
			t.Fatalf("tail %d returned %d messages", tc.tail, len(out.Messages))
		}
		for i, m := range out.Messages {
			if m.Sequence != p.Last-uint64(tc.want-1-i) {
				t.Fatalf("tail %d: message %d has sequence %d", tc.tail, i, m.Sequence)
			}
			if m.ID != all.Messages[len(all.Messages)-tc.want+i].ID {
				t.Fatalf("tail %d does not match the newest history", tc.tail)
			}
		}
		if out.NextSeq != p.Last || out.More || out.Generation != all.Generation {
			t.Fatalf("tail %d cursor: next %d more %v generation %q", tc.tail, out.NextSeq, out.More, out.Generation)
		}
	}
}

func TestReadTailOnEmptyStreamReportsTheTail(t *testing.T) {
	f := newFixture(t)
	ctx := context.Background()
	out, err := f.client("alice").Read(ctx, ReadArgs{Tail: 3})
	if err != nil {
		t.Fatal(err)
	}
	p, err := f.stream.Position(ctx)
	if err != nil {
		t.Fatal(err)
	}
	if len(out.Messages) != 0 || out.More || out.NextSeq != p.Last || out.Generation == "" {
		t.Fatalf("empty tail: %+v (stream tail %d)", out, p.Last)
	}
}

func TestReadTailArgumentErrors(t *testing.T) {
	f := newFixture(t)
	ctx := context.Background()
	c := f.client("alice")
	publishLocal(t, f, 2, "x")
	gen, err := c.Read(ctx, ReadArgs{})
	if err != nil {
		t.Fatal(err)
	}
	for name, args := range map[string]ReadArgs{
		"negative":          {Tail: -1},
		"above limit":       {Tail: 101},
		"with after_seq":    {Tail: 1, AfterSeq: 1, Generation: gen.Generation},
		"with limit":        {Tail: 2, Limit: 2},
		"limit above 100":   {Limit: 101},
		"negative limit":    {Limit: -1},
		"after without gen": {AfterSeq: 1},
	} {
		t.Run(name, func(t *testing.T) {
			_, err := c.Read(ctx, args)
			requireCode(t, err, "invalid_argument")
		})
	}
	_, err = c.Read(ctx, ReadArgs{Tail: 2, Generation: "another-generation"})
	requireCode(t, err, "stream_reset")
	if out, err := c.Read(ctx, ReadArgs{Tail: 2, Generation: gen.Generation}); err != nil || len(out.Messages) != 2 {
		t.Fatalf("tail with the current generation: %+v %v", out, err)
	}
}

func TestReadTailKeepsTheNewestWithinTheBudget(t *testing.T) {
	f := newFixture(t)
	ctx := context.Background()
	c := f.client("alice")
	// Forty 64 KiB messages exceed the 2 MiB history budget, so the tail must drop the oldest.
	publishLocal(t, f, 40, strings.Repeat("y", 64*1024))
	p, err := f.stream.Position(ctx)
	if err != nil {
		t.Fatal(err)
	}
	out, err := c.Read(ctx, ReadArgs{Tail: 100})
	if err != nil {
		t.Fatal(err)
	}
	n := len(out.Messages)
	if n == 0 || n >= 40 {
		t.Fatalf("budget returned %d messages", n)
	}
	if out.Messages[n-1].Sequence != p.Last || out.NextSeq != p.Last {
		t.Fatalf("budgeted tail lost the newest message: last %d next %d tail %d", out.Messages[n-1].Sequence, out.NextSeq, p.Last)
	}
	for i := 1; i < n; i++ {
		if out.Messages[i].Sequence != out.Messages[i-1].Sequence+1 {
			t.Fatal("budgeted tail is not a contiguous suffix")
		}
	}
	if out.More {
		t.Fatal("tail reported more")
	}
}

func TestReadTailAppliesRedaction(t *testing.T) {
	f := newFixture(t)
	ctx := context.Background()
	ids := publishLocal(t, f, 3, "visible")
	if err := f.srv.db.Redact(ids[2], "removed", "test"); err != nil {
		t.Fatal(err)
	}
	out, err := f.client("alice").Read(ctx, ReadArgs{Tail: 3})
	if err != nil {
		t.Fatal(err)
	}
	if len(out.Messages) != 3 || out.Messages[2].Content != "[redacted: removed]" || out.Messages[0].Content != "visible" {
		t.Fatalf("tail redaction: %+v", out.Messages)
	}
}
