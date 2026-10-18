package garden

import (
	"context"
	"os"
	"path/filepath"
	"testing"

	"github.com/veridian69/cairn/garden/internal/gardenauth"
	"github.com/veridian69/cairn/garden/internal/transport"
)

// A gateway whose daemon stops after the daemon check must fail before it opens
// state.db. Maintenance may already own the store, so the gateway cannot touch it.
func TestGatewayDoesNotOpenStateWhenDaemonStopsAfterEnsure(t *testing.T) {
	dir := t.TempDir()
	ns, err := transport.NewServer(dir)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(ns.Stop)
	stream, err := transport.NewManagedStream(ns.ClientURL(), transport.StreamOptions{})
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(stream.Close)
	urlfile := filepath.Join(t.TempDir(), "daemon.url")
	if err = os.WriteFile(urlfile, []byte(ns.ClientURL()), 0600); err != nil {
		t.Fatal(err)
	}
	cfg := Config{Listen: "127.0.0.1:0", DataDir: dir, DaemonURLFile: urlfile, Auth: gardenauth.Config{Endpoint: "http://127.0.0.1:9/memory/v1/diagnose", InstanceID: instance, Scope: gardenauth.Scope{Realm: "test", Segments: []gardenauth.Segment{}}, Classification: "internal"}, Principals: map[string]string{principalA: "alice"}}

	ran := false
	afterEnsure = func() {
		ran = true
		ns.Stop()
	}
	t.Cleanup(func() { afterEnsure = nil })
	srv, err := New(context.Background(), cfg)
	if err == nil {
		_ = srv.Close()
		t.Fatal("New succeeded after its daemon stopped")
	}
	if !ran {
		t.Fatal("afterEnsure seam did not run")
	}
	if _, statErr := os.Stat(filepath.Join(dir, "state.db")); !os.IsNotExist(statErr) {
		t.Fatalf("state.db was opened or created after the daemon stopped: %v", statErr)
	}
}
