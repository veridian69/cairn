package garden

import (
	"context"
	"net"
	"os"
	"strings"
	"sync/atomic"
	"testing"

	"github.com/google/uuid"
)

// TestSilentDaemonConnectionRedialsURLFile covers a gateway whose cached NATS socket stops
// carrying traffic without closing. The connection still reports as connected, so only a
// bounded probe and a re-dial from the URL file can recover; without them every call waits
// out the full handler deadline.
func TestSilentDaemonConnectionRedialsURLFile(t *testing.T) {
	var proxy *holdingProxy
	f := fixtureBeforeStart(t, func(f *fixture) {
		proxy = newHoldingProxy(t, strings.TrimPrefix(f.ns.ClientURL(), "nats://"))
		if err := os.WriteFile(f.cfg.DaemonURLFile, []byte("nats://"+proxy.addr), 0600); err != nil {
			t.Fatal(err)
		}
	})
	ctx := context.Background()
	a, b := f.client("alice"), f.client("bob")
	sent, err := a.Send(ctx, SendArgs{Content: "sent before the socket went silent", Recipients: []string{"bob"}})
	if err != nil {
		t.Fatal(err)
	}
	proxy.hold()
	if err = os.WriteFile(f.cfg.DaemonURLFile, []byte(f.ns.ClientURL()), 0600); err != nil {
		t.Fatal(err)
	}
	p, err := b.Poll(ctx, PollArgs{ConsumerID: uuid.NewString()})
	if err != nil || p.Message == nil || p.Message.ID != sent.ID {
		t.Fatalf("silent daemon connection was not replaced: %+v %v", p, err)
	}
}

// holdingProxy forwards bytes between the gateway and the NATS server until hold is called.
// After that both sockets stay open and every byte is discarded, which is what a dead path
// looks like to a client that has not yet noticed.
type holdingProxy struct {
	addr    string
	holding atomic.Bool
}

func newHoldingProxy(t *testing.T, upstream string) *holdingProxy {
	t.Helper()
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = ln.Close() })
	p := &holdingProxy{addr: ln.Addr().String()}
	go func() {
		for {
			client, err := ln.Accept()
			if err != nil {
				return
			}
			server, err := net.Dial("tcp", upstream)
			if err != nil {
				_ = client.Close()
				continue
			}
			go p.pipe(client, server)
			go p.pipe(server, client)
		}
	}()
	return p
}

func (p *holdingProxy) pipe(from, to net.Conn) {
	buf := make([]byte, 32*1024)
	for {
		n, err := from.Read(buf)
		if n > 0 && !p.holding.Load() {
			if _, werr := to.Write(buf[:n]); werr != nil {
				return
			}
		}
		if err != nil {
			return
		}
	}
}

func (p *holdingProxy) hold() { p.holding.Store(true) }
