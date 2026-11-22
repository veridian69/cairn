package attention

import (
	"context"
	"testing"
)

func TestDoctorCodexNeverResumesOrStartsTurn(t *testing.T) {
	fake := newFakeCodex(t, "msg-one", "probe")
	if err := checkCodexSocket(context.Background(), fake.socket, "thread-target"); err != nil {
		t.Fatal(err)
	}
	for _, method := range fake.received() {
		if method == "thread/resume" || method == "turn/start" {
			t.Fatalf("doctor sent %s", method)
		}
	}
}
