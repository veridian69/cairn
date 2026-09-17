package transport

import (
	"encoding/json"
	"errors"
	"os"
	"path/filepath"
	"sync"
	"time"

	natsserver "github.com/nats-io/nats-server/v2/server"
	"github.com/nats-io/nats.go"
	"github.com/veridian69/cairn/a2a/internal/maintenance"
)

// Server wraps an embedded NATS server with JetStream enabled.
type Server struct {
	ns                 *natsserver.Server
	identityConnection *nats.Conn
	storeLock          *maintenance.Lease
	stopOnce           sync.Once
}

// NewServer starts an embedded NATS server with JetStream, storing data in dataDir.
// The server binds to localhost on a random available port.
func NewServer(dataDir string) (_ *Server, err error) {
	if err = os.MkdirAll(dataDir, 0700); err != nil {
		return nil, err
	}
	lock, err := maintenance.AcquireStore(dataDir)
	if err != nil {
		return nil, err
	}
	defer func() {
		if err != nil {
			_ = lock.Close()
		}
	}()

	opts := &natsserver.Options{
		Host:      "127.0.0.1",
		Port:      -1,
		JetStream: true,
		StoreDir:  dataDir,
		NoLog:     true,
		NoSigs:    true,
	}

	ns, err := natsserver.NewServer(opts)
	if err != nil {
		return nil, err
	}
	ns.Start()

	if !ns.ReadyForConnections(5 * time.Second) {
		ns.Shutdown()
		ns.WaitForShutdown()
		return nil, errors.New("NATS server not ready")
	}
	canonical, err := filepath.Abs(dataDir)
	if err == nil {
		canonical, err = filepath.EvalSymlinks(canonical)
	}
	if err != nil {
		ns.Shutdown()
		ns.WaitForShutdown()
		return nil, err
	}
	nc, err := nats.Connect(ns.ClientURL())
	if err != nil {
		ns.Shutdown()
		ns.WaitForShutdown()
		return nil, err
	}
	payload, _ := json.Marshal(storageIdentity{ServerID: ns.ID(), DataDir: canonical})
	_, err = nc.Subscribe(storageSubject, func(msg *nats.Msg) { _ = msg.Respond(payload) })
	if err == nil {
		err = nc.FlushTimeout(2 * time.Second)
	}
	if err != nil {
		nc.Close()
		ns.Shutdown()
		ns.WaitForShutdown()
		return nil, err
	}
	return &Server{ns: ns, identityConnection: nc, storeLock: lock}, nil
}

// ClientURL returns the connection URL for NATS clients.
func (s *Server) ClientURL() string { return s.ns.ClientURL() }

// Stop shuts down the embedded NATS server and waits for it to finish.
func (s *Server) Stop() {
	s.stopOnce.Do(func() {
		if s.identityConnection != nil {
			s.identityConnection.Close()
		}
		s.ns.Shutdown()
		s.ns.WaitForShutdown()
		if s.storeLock != nil {
			_ = s.storeLock.Close()
		}
	})
}
