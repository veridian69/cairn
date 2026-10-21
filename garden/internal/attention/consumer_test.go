package attention

import (
	"context"
	"errors"
	"os"
	"path/filepath"
	"strings"
	"syscall"
	"testing"

	"github.com/google/uuid"
)

// consumerFor is what one process start does: claim the identity, read its consumer, release.
func consumerFor(t *testing.T, dir string) string {
	t.Helper()
	claim, err := (Home{Dir: dir}).ClaimIdentity("spike")
	if err != nil {
		t.Fatal(err)
	}
	defer claim.Close()
	id, err := claim.ConsumerID()
	if err != nil {
		t.Fatal(err)
	}
	return id
}

func consumerRecord(dir string) string {
	return filepath.Join(dir, "run", "spike.consumer")
}

func TestConsumerIDSurvivesRestartOnSameHome(t *testing.T) {
	dir := t.TempDir()
	first := consumerFor(t, dir)
	if u, err := uuid.Parse(first); err != nil || u.String() != first {
		t.Fatalf("consumer %q is not a canonical UUID", first)
	}
	info, err := os.Stat(consumerRecord(dir))
	if err != nil || info.Mode().Perm() != 0600 {
		t.Fatalf("consumer record = %v, %v; want a 0600 file", info, err)
	}
	if again := consumerFor(t, dir); again != first {
		t.Fatalf("restart presented %s, want the same consumer %s", again, first)
	}
}

func TestMalformedOrInsecureConsumerRecordIsReplaced(t *testing.T) {
	cases := map[string]func(t *testing.T, path string) string{
		"malformed": func(t *testing.T, path string) string {
			if err := os.WriteFile(path, []byte("not-a-uuid"), 0600); err != nil {
				t.Fatal(err)
			}
			return "not-a-uuid"
		},
		"FIFO": func(t *testing.T, path string) string {
			if err := syscall.Mkfifo(path, 0600); err != nil {
				t.Fatal(err)
			}
			return ""
		},
		"nil UUID": func(t *testing.T, path string) string {
			if err := os.WriteFile(path, []byte(uuid.Nil.String()), 0600); err != nil {
				t.Fatal(err)
			}
			return uuid.Nil.String()
		},
		"group readable": func(t *testing.T, path string) string {
			id := uuid.NewString()
			if err := os.WriteFile(path, []byte(id), 0644); err != nil {
				t.Fatal(err)
			}
			if err := os.Chmod(path, 0644); err != nil {
				t.Fatal(err)
			}
			return id
		},
		"symlink to a foreign file": func(t *testing.T, path string) string {
			target := filepath.Join(t.TempDir(), "elsewhere")
			if err := os.WriteFile(target, []byte("keep me"), 0600); err != nil {
				t.Fatal(err)
			}
			if err := os.Symlink(target, path); err != nil {
				t.Fatal(err)
			}
			t.Cleanup(func() {
				if raw, err := os.ReadFile(target); err != nil || string(raw) != "keep me" {
					t.Errorf("symlink target was modified: %q, %v", raw, err)
				}
			})
			return target
		},
	}
	for name, plant := range cases {
		t.Run(name, func(t *testing.T) {
			dir := t.TempDir()
			if err := os.MkdirAll(filepath.Join(dir, "run"), 0700); err != nil {
				t.Fatal(err)
			}
			bad := plant(t, consumerRecord(dir))
			id := consumerFor(t, dir)
			if id == bad || strings.Contains(bad, id) {
				t.Fatalf("bad record %q was kept", bad)
			}
			if u, err := uuid.Parse(id); err != nil || u == uuid.Nil {
				t.Fatalf("replacement %q is not a UUID", id)
			}
			info, err := os.Lstat(consumerRecord(dir))
			if err != nil || !info.Mode().IsRegular() || info.Mode().Perm() != 0600 {
				t.Fatalf("replacement record = %v, %v; want a private regular file", info, err)
			}
			if again := consumerFor(t, dir); again != id {
				t.Fatalf("replacement is not stable: %s then %s", id, again)
			}
		})
	}
}

func TestConsumerRecordIsOnlyReachableThroughTheClaim(t *testing.T) {
	var nilClaim *Claim
	if _, err := nilClaim.ConsumerID(); err == nil {
		t.Fatal("consumer read without a claim")
	}
	dir := t.TempDir()
	claim, err := (Home{Dir: dir}).ClaimIdentity("spike")
	if err != nil {
		t.Fatal(err)
	}
	if _, err := (Home{Dir: dir}).ClaimIdentity("spike"); !errors.Is(err, ErrIdentityClaimed) {
		t.Fatalf("second claim = %v, want ErrIdentityClaimed", err)
	}
	_ = claim.Close()
}

func TestRestartRenewsOwnLeaseAndOtherHomeIsRefused(t *testing.T) {
	ctx := context.Background()
	f, _ := harnessRemote(t, "heartbeat")
	home := t.TempDir()
	other := t.TempDir()

	// First start on this machine takes the lease.
	first := &Remote{api: f, profile: Profile{Participant: "spike"}, harness: true, consumer: consumerFor(t, home), nudge: make(chan struct{}, 1)}
	if err := first.takeLease(ctx); err != nil {
		t.Fatal(err)
	}

	// Another home on another machine is refused while that lease is live.
	elsewhere := &Remote{api: f, profile: Profile{Participant: "spike"}, harness: true, consumer: consumerFor(t, other), nudge: make(chan struct{}, 1)}
	if err := elsewhere.takeLease(ctx); !errors.Is(err, ErrBusy) {
		t.Fatalf("other home lease = %v, want ErrBusy while the lease is live", err)
	}

	// Restarting on the same home, with the first process gone, renews the lease at once.
	restarted := &Remote{api: f, profile: Profile{Participant: "spike"}, harness: true, consumer: consumerFor(t, home), nudge: make(chan struct{}, 1)}
	if err := restarted.takeLease(ctx); err != nil {
		t.Fatalf("restart on the same home refused: %v", err)
	}
	if !restarted.isHeld() {
		t.Fatal("restart did not hold the lease")
	}
}
