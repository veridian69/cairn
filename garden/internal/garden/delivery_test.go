package garden

import (
	"context"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/google/uuid"
)

// testClock is an injectable gateway clock for lease and pair-window tests.
type testClock struct {
	mu sync.Mutex
	t  time.Time
}

func (c *testClock) Now() time.Time {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.t
}

func (c *testClock) Advance(d time.Duration) {
	c.mu.Lock()
	defer c.mu.Unlock()
	c.t = c.t.Add(d)
}

func deliveryFor(t *testing.T, res SendResult, recipient string) Delivery {
	t.Helper()
	for _, d := range res.Deliveries {
		if d.Recipient == recipient {
			return d
		}
	}
	t.Fatalf("no delivery outcome for %s in %+v", recipient, res.Deliveries)
	return Delivery{}
}

func TestSendReportsPerRecipientDeliveryFromLeases(t *testing.T) {
	f := newFixture(t)
	ctx := context.Background()
	a, b := f.client("alice"), f.client("bob")
	if _, err := b.Heartbeat(ctx, HeartbeatArgs{ConsumerID: uuid.NewString(), Deliverable: true}); err != nil {
		t.Fatal(err)
	}
	res, err := a.Send(ctx, SendArgs{Content: "both", Recipients: []string{"alice", "bob"}})
	if err != nil {
		t.Fatal(err)
	}
	if d := deliveryFor(t, res, "bob"); d.Delivery != "notified" || d.Reason != "" {
		t.Fatalf("bob with a deliverable lease = %+v, want notified", d)
	}
	if d := deliveryFor(t, res, "alice"); d.Delivery != "suppressed" || d.Reason != "self" {
		t.Fatalf("alice addressing herself = %+v, want suppressed/self", d)
	}
	if res.Notice != "" || res.ID == "" || len(res.Recipients) != 2 {
		t.Fatalf("unexpected send result %+v", res)
	}
}

func TestSelfAddressedRecipientIsNeverReportedNotified(t *testing.T) {
	f := newFixture(t)
	ctx := context.Background()
	a := f.client("alice")
	if _, err := a.Heartbeat(ctx, HeartbeatArgs{ConsumerID: uuid.NewString(), Deliverable: true}); err != nil {
		t.Fatal(err)
	}
	res, err := a.Send(ctx, SendArgs{Content: "note to self", Recipients: []string{"alice", "bob"}})
	if err != nil {
		t.Fatal(err)
	}
	if d := deliveryFor(t, res, "alice"); d.Delivery != "suppressed" || d.Reason != "self" {
		t.Fatalf("self recipient = %+v, want suppressed/self", d)
	}
	if d := deliveryFor(t, res, "bob"); d.Delivery != "queued" {
		t.Fatalf("bob = %+v, want queued", d)
	}
	if res.Notice != "" {
		t.Fatalf("self-addressing raised the loop-guard notice: %q", res.Notice)
	}
}

func TestHeartbeatLeasesWithoutLeasingMessagesAndWhoReportsPresence(t *testing.T) {
	f := newFixture(t)
	ctx := context.Background()
	a, b := f.client("alice"), f.client("bob")
	owner := uuid.NewString()
	if _, err := a.Send(ctx, SendArgs{Content: "one", Recipients: []string{"bob"}}); err != nil {
		t.Fatal(err)
	}
	hb, err := b.Heartbeat(ctx, HeartbeatArgs{ConsumerID: owner, Deliverable: false})
	if err != nil || hb.Deliverable || hb.LeaseExpiresAt == "" {
		t.Fatalf("heartbeat = %+v, %v; want a lease that is not deliverable", hb, err)
	}
	who, err := a.Who(ctx)
	if err != nil {
		t.Fatal(err)
	}
	if presence := whoEntry(t, who, "bob"); !presence.Online || presence.Deliverable {
		t.Fatalf("bob presence = %+v, want online and not deliverable", presence)
	}
	res, err := a.Send(ctx, SendArgs{Content: "two", Recipients: []string{"bob"}})
	if err != nil || deliveryFor(t, res, "bob").Delivery != "queued" {
		t.Fatalf("send while not deliverable = %+v, %v; want queued", res.Deliveries, err)
	}
	if _, err = b.Heartbeat(ctx, HeartbeatArgs{ConsumerID: owner, Deliverable: true}); err != nil {
		t.Fatal(err)
	}
	res, err = a.Send(ctx, SendArgs{Content: "three", Recipients: []string{"bob"}})
	if err != nil || deliveryFor(t, res, "bob").Delivery != "notified" {
		t.Fatalf("send while deliverable = %+v, %v; want notified", res.Deliveries, err)
	}
	if _, err = b.Heartbeat(ctx, HeartbeatArgs{ConsumerID: uuid.NewString(), Deliverable: true}); err == nil {
		t.Fatal("second consumer took a live lease")
	}
	requireCode(t, err, "inbox_busy")
	// Heartbeat leases no message: the first pending message is still the owner's to poll.
	p, err := b.Poll(ctx, PollArgs{ConsumerID: owner})
	if err != nil || p.Message == nil || p.Message.Content != "one" {
		t.Fatalf("poll after heartbeat = %+v, %v; want the first queued message", p, err)
	}
	// An old adapter that only polls is deliverable.
	who, err = a.Who(ctx)
	if err != nil {
		t.Fatal(err)
	}
	if presence := whoEntry(t, who, "bob"); !presence.Online || !presence.Deliverable {
		t.Fatalf("bob after poll = %+v, want online and deliverable", presence)
	}
	if !a.Supports("heartbeat") || !a.Supports("who") {
		t.Fatal("new gateway does not advertise heartbeat and who in tools/list")
	}
}

func whoEntry(t *testing.T, who WhoResult, name string) Presence {
	t.Helper()
	for _, p := range who.Participants {
		if p.Name == name {
			return p
		}
	}
	t.Fatalf("who has no entry for %s: %+v", name, who)
	return Presence{}
}

func TestThreadDepthSuppressesAttentionWithoutLeasingItAndKeepsHistory(t *testing.T) {
	clk := &testClock{t: time.Date(2026, 10, 19, 9, 0, 0, 0, time.UTC)}
	f := fixtureBeforeStart(t, func(f *fixture) {
		f.cfg.LoopGuard = LoopGuard{ThreadDepth: 1}
		f.now = clk.Now
	})
	ctx := context.Background()
	a, b := f.client("alice"), f.client("bob")
	root, err := a.Send(ctx, SendArgs{Content: "question", Recipients: []string{"bob"}})
	if err != nil || deliveryFor(t, root, "bob").Reason != "" {
		t.Fatalf("root = %+v, %v; want unsuppressed", root.Deliveries, err)
	}
	reply, err := b.Send(ctx, SendArgs{Content: "loop reply", ReplyTo: root.ID, Recipients: []string{"alice"}})
	if err != nil {
		t.Fatal(err)
	}
	if d := deliveryFor(t, reply, "alice"); d.Delivery != "suppressed" || d.Reason != "thread_depth" {
		t.Fatalf("depth-2 reply = %+v, want suppressed thread_depth", d)
	}
	if !strings.Contains(reply.Notice, "involve the human") {
		t.Fatalf("notice = %q, want a human-involvement request", reply.Notice)
	}
	// The suppressed reply is not leased to alice, and the poll moves past it to the next real message.
	alice := uuid.NewString()
	p, err := a.Poll(ctx, PollArgs{ConsumerID: alice})
	if err != nil || p.Message != nil {
		t.Fatalf("alice poll after suppressed reply = %+v, %v; want nothing", p, err)
	}
	if _, err = b.Send(ctx, SendArgs{Content: "fresh start", Recipients: []string{"alice"}}); err != nil {
		t.Fatal(err)
	}
	p, err = a.Poll(ctx, PollArgs{ConsumerID: alice})
	if err != nil || p.Message == nil || p.Message.Content != "fresh start" {
		t.Fatalf("alice poll after new thread = %+v, %v; want the new thread", p, err)
	}
	history, err := a.Read(ctx, ReadArgs{})
	if err != nil {
		t.Fatal(err)
	}
	found := false
	for _, m := range history.Messages {
		found = found || m.Content == "loop reply"
	}
	if !found {
		t.Fatal("suppressed reply missing from history")
	}
}

func TestUnknownParentCountsAsRootAndPairRateWindowExpires(t *testing.T) {
	clk := &testClock{t: time.Date(2026, 10, 19, 9, 0, 0, 0, time.UTC)}
	f := fixtureBeforeStart(t, func(f *fixture) {
		f.cfg.LoopGuard = LoopGuard{PairRate: 2, PairWindowSeconds: 600}
		f.now = clk.Now
	})
	ctx := context.Background()
	a, b := f.client("alice"), f.client("bob")
	orphan, err := a.Send(ctx, SendArgs{Content: "orphan", ReplyTo: uuid.NewString(), Recipients: []string{"bob"}})
	if err != nil || deliveryFor(t, orphan, "bob").Reason != "" {
		t.Fatalf("reply to unknown parent = %+v, %v; want a root that is not suppressed", orphan.Deliveries, err)
	}
	// The orphan reply is the first attention request in this pair, so one more is allowed.
	res, err := a.Send(ctx, SendArgs{Content: "ping", Recipients: []string{"bob"}})
	if err != nil || deliveryFor(t, res, "bob").Reason != "" {
		t.Fatalf("second attention = %+v, %v; want unsuppressed", res.Deliveries, err)
	}
	// The third request in the window is suppressed, and the pair counts in both directions.
	res, err = a.Send(ctx, SendArgs{Content: "over", Recipients: []string{"bob"}})
	if err != nil || deliveryFor(t, res, "bob").Reason != "pair_rate" {
		t.Fatalf("third attention = %+v, %v; want pair_rate", res.Deliveries, err)
	}
	res, err = b.Send(ctx, SendArgs{Content: "back", Recipients: []string{"alice"}})
	if err != nil || deliveryFor(t, res, "alice").Reason != "pair_rate" {
		t.Fatalf("reverse attention = %+v, %v; want pair_rate", res.Deliveries, err)
	}
	// Addressing yourself never counts as attention, so it is reported as self, not pair_rate.
	res, err = b.Send(ctx, SendArgs{Content: "self", Recipients: []string{"bob"}})
	if err != nil || deliveryFor(t, res, "bob").Reason != "self" {
		t.Fatalf("self send = %+v, %v", res.Deliveries, err)
	}
	// A mixed send names only the loop-guard reason in its notice, never self.
	res, err = b.Send(ctx, SendArgs{Content: "mixed", Recipients: []string{"bob", "alice"}})
	if err != nil || deliveryFor(t, res, "alice").Reason != "pair_rate" || deliveryFor(t, res, "bob").Reason != "self" {
		t.Fatalf("mixed send = %+v, %v", res.Deliveries, err)
	}
	if !strings.Contains(res.Notice, "alice (pair_rate)") || strings.Contains(res.Notice, "self") {
		t.Fatalf("mixed send notice = %q, want alice (pair_rate) only", res.Notice)
	}
	clk.Advance(601 * time.Second)
	res, err = a.Send(ctx, SendArgs{Content: "after window", Recipients: []string{"bob"}})
	if err != nil || deliveryFor(t, res, "bob").Reason != "" {
		t.Fatalf("attention after window = %+v, %v; want unsuppressed", res.Deliveries, err)
	}
}

func TestLoopGuardConfigIsValidatedAndDefaulted(t *testing.T) {
	f := newFixture(t)
	if got := f.srv.cfg.LoopGuard; got != (LoopGuard{ThreadDepth: 30, PairRate: 20, PairWindowSeconds: 600}) {
		t.Fatalf("default loop guard = %+v", got)
	}
	bad := f.cfg
	bad.LoopGuard = LoopGuard{ThreadDepth: -1}
	if err := bad.validate(); err == nil {
		t.Fatal("negative thread depth accepted")
	}
}

func TestUpgradeAddsDeliverableColumnToOldSchemaAndKeepsPendingMessages(t *testing.T) {
	f := newFixture(t)
	ctx := context.Background()
	a := f.client("alice")
	if _, err := a.Send(ctx, SendArgs{Content: "before upgrade", Recipients: []string{"bob"}}); err != nil {
		t.Fatal(err)
	}
	// Recreate the pre-upgrade schema on the live database, then restart the gateway so it migrates.
	for _, stmt := range []string{
		"ALTER TABLE garden_inbox DROP COLUMN deliverable",
		"DROP TABLE garden_message_depth",
		"DROP TABLE garden_attention",
	} {
		if _, err := f.srv.sql.Exec(stmt); err != nil {
			t.Fatalf("%s: %v", stmt, err)
		}
	}
	f.http.Close()
	if err := f.srv.Close(); err != nil {
		t.Fatal(err)
	}
	f.start()
	var schema string
	if err := f.srv.sql.QueryRow("SELECT sql FROM sqlite_master WHERE type='table' AND name='garden_inbox'").Scan(&schema); err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(schema, "deliverable INTEGER NOT NULL DEFAULT 0") {
		t.Fatalf("migrated garden_inbox lacks deliverable: %s", schema)
	}
	// A second start must find the column already present and change nothing.
	f.http.Close()
	if err := f.srv.Close(); err != nil {
		t.Fatal(err)
	}
	f.start()
	b := f.client("bob")
	hb, err := b.Heartbeat(ctx, HeartbeatArgs{ConsumerID: uuid.NewString(), Deliverable: true})
	if err != nil || !hb.Deliverable {
		t.Fatalf("heartbeat after migration = %+v, %v", hb, err)
	}
	p, err := b.Poll(ctx, PollArgs{ConsumerID: uuid.NewString()})
	if err == nil && p.Message != nil {
		t.Fatalf("poll by a second consumer while the lease is live = %+v", p)
	}
	requireCode(t, err, "inbox_busy")
}

func TestDepthRowsArePrunedOnceTheStreamDropsTheirMessages(t *testing.T) {
	f := newFixture(t)
	ctx := context.Background()
	now := time.Now()
	for seq, id := range []string{uuid.NewString(), uuid.NewString(), uuid.NewString()} {
		if err := f.srv.recordLoopGuard(ctx, id, uint64(seq+1), 1, 1, "alice", nil, now, time.Minute); err != nil {
			t.Fatal(err)
		}
	}
	// The stream now retains only sequence 3 onwards.
	if err := f.srv.recordLoopGuard(ctx, uuid.NewString(), 4, 3, 1, "alice", nil, now, time.Minute); err != nil {
		t.Fatal(err)
	}
	var kept, oldest int
	if err := f.srv.sql.QueryRowContext(ctx, "SELECT COUNT(*), MIN(seq) FROM garden_message_depth").Scan(&kept, &oldest); err != nil {
		t.Fatal(err)
	}
	if kept != 2 || oldest != 3 {
		t.Fatalf("depth rows = %d from seq %d, want 2 from seq 3", kept, oldest)
	}
}

func TestParentNoLongerRetainedCountsAsRootBeforePruning(t *testing.T) {
	f := newFixture(t)
	ctx := context.Background()
	parent := uuid.NewString()
	if err := f.srv.recordLoopGuard(ctx, parent, 1, 1, 30, "alice", nil, time.Now(), time.Minute); err != nil {
		t.Fatal(err)
	}
	if depth, found, err := f.srv.messageDepth(ctx, parent, 1); err != nil || !found || depth != 30 {
		t.Fatalf("retained parent = %d, %v, %v; want depth 30", depth, found, err)
	}
	// Retention has moved past the parent, but its row is not pruned until the next send records.
	if _, found, err := f.srv.messageDepth(ctx, parent, 2); err != nil || found {
		t.Fatalf("expired parent found = %v, %v; want a root", found, err)
	}
}

func TestWhoOmitsPrincipalsRemovedFromTheConfiguration(t *testing.T) {
	f := newFixture(t)
	ctx := context.Background()
	// A principal removed from the configuration keeps its inbox row, possibly with a live lease.
	if _, err := f.srv.sql.ExecContext(ctx, "INSERT INTO garden_inbox(principal,name,participant,generation,cursor,consumer,lease_until,deliverable) VALUES(?,?,?,?,?,?,?,1)",
		"removed-principal", "carol", uuid.NewString(), "gen", 0, uuid.NewString(), time.Now().Add(time.Hour).UnixNano()); err != nil {
		t.Fatal(err)
	}
	res, err := f.client("alice").Who(ctx)
	if err != nil {
		t.Fatal(err)
	}
	for _, p := range res.Participants {
		if p.Name == "carol" {
			t.Fatalf("who lists removed principal carol: %+v", res.Participants)
		}
	}
	if len(res.Participants) != 2 {
		t.Fatalf("who = %+v, want alice and bob", res.Participants)
	}
}
