package transport

import (
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"sync"
	"time"

	natsserver "github.com/nats-io/nats-server/v2/server"
	"github.com/nats-io/nats.go"
	"github.com/veridian69/cairn/garden/internal/maintenance"
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
	// NoLog keeps the server quiet, and it also hides why a start failed: Start returns
	// without a listener when JetStream cannot be enabled, and reports the reason only as
	// a fatal log line. Keep that line for the error below.
	failure := &startFailure{}
	ns.SetLogger(failure, false, false)
	ns.Start()

	// A fatal line means Start has given up, so there is nothing left to wait for.
	ready := false
	for deadline := time.Now().Add(5 * time.Second); !ready && time.Now().Before(deadline) && !failure.failed(); {
		ready = ns.ReadyForConnections(50 * time.Millisecond)
	}
	if !ready {
		ns.Shutdown()
		ns.WaitForShutdown()
		if reason := failure.reason(); reason != "" {
			return nil, fmt.Errorf("NATS server not ready: %s", reason)
		}
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

// startFailure is the embedded server's logger. It keeps the first fatal line, or failing that
// the last error line, and drops everything else.
type startFailure struct {
	mu          sync.Mutex
	fatal, last string
}

func (f *startFailure) Fatalf(format string, v ...any) {
	f.mu.Lock()
	defer f.mu.Unlock()
	if f.fatal == "" {
		f.fatal = fmt.Sprintf(format, v...)
	}
}

func (f *startFailure) Errorf(format string, v ...any) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.last = fmt.Sprintf(format, v...)
}

func (*startFailure) Noticef(string, ...any) {}
func (*startFailure) Warnf(string, ...any)   {}
func (*startFailure) Debugf(string, ...any)  {}
func (*startFailure) Tracef(string, ...any)  {}

func (f *startFailure) failed() bool {
	f.mu.Lock()
	defer f.mu.Unlock()
	return f.fatal != ""
}

func (f *startFailure) reason() string {
	f.mu.Lock()
	defer f.mu.Unlock()
	if f.fatal != "" {
		return f.fatal
	}
	return f.last
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
