package garden

import (
	"context"
	"crypto/tls"
	"encoding/json"
	"errors"
	"io"
	"net"
	"net/http"
	"os"
	"path/filepath"
	"reflect"
	"sync"
	"syscall"
	"time"

	"github.com/veridian69/cairn/a2a/internal/maintenance"
	"github.com/veridian69/cairn/a2a/internal/transport"
	"golang.org/x/sys/unix"
)

// StreamConfig selects server-local conversation retention. Zero values retain
// the existing daemon defaults: no age or byte limit.
type StreamConfig struct {
	MaxAge   string `json:"max_age"`
	MaxBytes int64  `json:"max_bytes"`
}

// HostConfig owns a local conversation runtime and its authenticated gateway.
// It deliberately has no provider, agent-worker or memory-store configuration.
type HostConfig struct {
	Gateway Config       `json:"gateway"`
	Stream  StreamConfig `json:"stream"`
}

// LoadHostConfig reads strict, bounded configuration for a foreground host.
func LoadHostConfig(path string) (HostConfig, error) {
	var cfg HostConfig
	f, err := os.Open(path)
	if err != nil {
		return cfg, err
	}
	defer f.Close()
	raw, err := io.ReadAll(io.LimitReader(f, 128*1024+1))
	if err != nil || len(raw) > 128*1024 {
		return cfg, errors.New("invalid or oversized Garden host configuration")
	}
	if err = checkJSON(raw, reflect.TypeOf(cfg)); err != nil {
		return cfg, errors.New("invalid Garden host configuration JSON")
	}
	if err = json.Unmarshal(raw, &cfg); err != nil {
		return cfg, errors.New("invalid Garden host configuration JSON")
	}
	_, err = cfg.validate()
	return cfg, err
}
func (c HostConfig) validate() (time.Duration, error) {
	if err := c.Gateway.validate(); err != nil {
		return 0, err
	}
	var age time.Duration
	var err error
	if c.Stream.MaxAge != "" && c.Stream.MaxAge != "0" {
		age, err = time.ParseDuration(c.Stream.MaxAge)
	}
	if err != nil || age < 0 || c.Stream.MaxBytes < 0 {
		return 0, errors.New("Garden retention must use a nonnegative duration and byte limit")
	}
	return age, nil
}

// RunHost serves until cancelled or a startup/listener failure occurs. It owns
// NATS and gateway shutdown, and never creates local memory or provider workers.
func RunHost(ctx context.Context, cfg HostConfig) error {
	if err := ctx.Err(); err != nil {
		return err
	}
	host, err := startHost(ctx, cfg)
	if err != nil {
		return err
	}
	return host.run(ctx)
}

type hostRuntime struct {
	gateway   *Server
	nats      *transport.Server
	stream    *transport.Stream
	listener  net.Listener
	http      *http.Server
	lease     *maintenance.Lease
	storeLock *os.File
	urlPath   string
	urlInfo   os.FileInfo
	closeOnce sync.Once
	closeErr  error
}

func startHost(ctx context.Context, cfg HostConfig) (host *hostRuntime, err error) {
	age, err := cfg.validate()
	if err != nil {
		return nil, err
	}
	if err = ctx.Err(); err != nil {
		return nil, err
	}
	var tlsConfig *tls.Config
	if cfg.Gateway.TLSCertFile != "" {
		certificate, e := tls.LoadX509KeyPair(cfg.Gateway.TLSCertFile, cfg.Gateway.TLSKeyFile)
		if e != nil {
			return nil, errors.New("cannot load Garden TLS certificate and key")
		}
		tlsConfig = &tls.Config{MinVersion: tls.VersionTLS12, Certificates: []tls.Certificate{certificate}}
	}
	host = &hostRuntime{urlPath: cfg.Gateway.DaemonURLFile}
	defer func(owned *hostRuntime) {
		if err != nil {
			_ = owned.close()
		}
	}(host)
	host.lease, err = maintenance.Acquire(filepath.Dir(host.urlPath), maintenance.Exclusive)
	if err != nil {
		return nil, err
	}
	if err = validateHostURLFile(host.urlPath); err != nil {
		return nil, err
	}
	if err = os.MkdirAll(cfg.Gateway.DataDir, 0700); err != nil {
		return nil, err
	}
	host.storeLock, err = os.OpenFile(filepath.Join(cfg.Gateway.DataDir, "host.lock"), os.O_CREATE|os.O_RDWR, 0600)
	if err != nil {
		return nil, err
	}
	if err = unix.Flock(int(host.storeLock.Fd()), unix.LOCK_EX|unix.LOCK_NB); err != nil {
		return nil, errors.New("another Garden host owns this store")
	}
	host.listener, err = net.Listen("tcp", cfg.Gateway.Listen)
	if err != nil {
		return nil, err
	}
	if tlsConfig != nil {
		host.listener = tls.NewListener(host.listener, tlsConfig)
	}
	host.nats, err = transport.NewServer(cfg.Gateway.DataDir)
	if err != nil {
		return nil, err
	}
	// Existing settings are checked without mutation. Create the initial stream
	// with its final retention so later starts never update its persisted identity.
	host.stream, err = transport.NewManagedStream(host.nats.ClientURL(), transport.StreamOptions{MaxAge: age, MaxBytes: cfg.Stream.MaxBytes})
	if err != nil {
		return nil, err
	}
	if err = host.publishURL(host.nats.ClientURL()); err != nil {
		return nil, err
	}
	host.gateway, err = New(ctx, cfg.Gateway)
	if err != nil {
		return nil, err
	}
	host.http = &http.Server{Handler: host.gateway.Handler(), ReadHeaderTimeout: 5 * time.Second, ReadTimeout: 10 * time.Second, WriteTimeout: 40 * time.Second, IdleTimeout: 60 * time.Second, MaxHeaderBytes: 16384, BaseContext: func(net.Listener) context.Context { return ctx }}
	return host, nil
}
func validateHostURLFile(path string) error {
	info, err := os.Lstat(path)
	if errors.Is(err, os.ErrNotExist) {
		return nil
	}
	if err != nil {
		return err
	}
	stat, ok := info.Sys().(*syscall.Stat_t)
	if !info.Mode().IsRegular() || info.Mode().Perm()&0077 != 0 || !ok || int(stat.Uid) != os.Geteuid() {
		return errors.New("refusing an unowned or non-private Garden daemon URL file")
	}
	return nil
}
func (h *hostRuntime) publishURL(value string) error {
	f, err := os.CreateTemp(filepath.Dir(h.urlPath), ".garden-url-*")
	if err != nil {
		return err
	}
	name := f.Name()
	defer os.Remove(name)
	if _, err = f.WriteString(value + "\n"); err == nil {
		err = f.Sync()
	}
	if closeErr := f.Close(); err == nil {
		err = closeErr
	}
	if err != nil {
		return err
	}
	if err = validateHostURLFile(h.urlPath); err != nil {
		return err
	}
	if err = os.Rename(name, h.urlPath); err != nil {
		return err
	}
	h.urlInfo, err = os.Stat(h.urlPath)
	return err
}
func (h *hostRuntime) run(ctx context.Context) error {
	defer h.close()
	done := make(chan error, 1)
	go func() { done <- h.http.Serve(h.listener) }()
	select {
	case <-ctx.Done():
		_ = h.close()
		err := <-done
		if errors.Is(err, http.ErrServerClosed) || errors.Is(err, net.ErrClosed) {
			return nil
		}
		return err
	case err := <-done:
		if errors.Is(err, http.ErrServerClosed) {
			return nil
		}
		return err
	}
}
func (h *hostRuntime) close() error {
	h.closeOnce.Do(func() {
		if h.gateway != nil {
			_ = h.gateway.Close()
		}
		if h.http != nil {
			shutdown, cancel := context.WithTimeout(context.Background(), 2*time.Second)
			_ = h.http.Shutdown(shutdown)
			cancel()
			_ = h.http.Close()
		}
		if h.listener != nil {
			_ = h.listener.Close()
		}
		if h.stream != nil {
			h.stream.Close()
		}
		if h.nats != nil {
			h.nats.Stop()
		}
		if h.urlInfo != nil {
			info, err := os.Lstat(h.urlPath)
			if err == nil && os.SameFile(info, h.urlInfo) {
				h.closeErr = os.Remove(h.urlPath)
			}
		}
		if h.storeLock != nil {
			_ = unix.Flock(int(h.storeLock.Fd()), unix.LOCK_UN)
			_ = h.storeLock.Close()
		}
		if h.lease != nil {
			_ = h.lease.Close()
		}
	})
	return h.closeErr
}
