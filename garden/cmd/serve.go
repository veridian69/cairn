package cmd

import (
	"context"
	"errors"
	"fmt"
	"io"
	"net/http"
	"os"
	"os/signal"
	"path/filepath"
	"syscall"
	"time"

	"github.com/spf13/cobra"
	"github.com/veridian69/cairn/garden/internal/garden"
	"github.com/veridian69/cairn/garden/internal/maintenance"
)

var serveConfig string
var serveCmd = &cobra.Command{
	Use: "serve --config FILE", Short: "Serve an authenticated shared Garden over MCP HTTP", Args: cobra.NoArgs,
	PersistentPreRunE: func(*cobra.Command, []string) error { return nil },
	RunE: func(cmd *cobra.Command, _ []string) error {
		cfg, err := garden.LoadConfig(serveConfig)
		if err != nil {
			return err
		}
		cfg.Version = version
		lease, err := maintenance.Acquire(filepath.Dir(cfg.DaemonURLFile), maintenance.Shared)
		if err != nil {
			return err
		}
		defer lease.Close()
		ctx, stop := signal.NotifyContext(cmd.Context(), syscall.SIGINT, syscall.SIGTERM)
		defer stop()
		srv, err := garden.New(ctx, cfg)
		if err != nil {
			return err
		}
		defer srv.Close()
		httpServer := &http.Server{Addr: cfg.Listen, Handler: srv.Handler(), ReadHeaderTimeout: 5 * time.Second, ReadTimeout: 10 * time.Second, WriteTimeout: 40 * time.Second, IdleTimeout: 60 * time.Second, MaxHeaderBytes: 16384}
		done := make(chan struct{})
		defer close(done)
		// SIGHUP rereads --config. Reload applies only principal changes; anything else is refused.
		hup := make(chan os.Signal, 1)
		signal.Notify(hup, syscall.SIGHUP)
		defer signal.Stop(hup)
		go func() {
			principals := cfg.Principals
			for {
				select {
				case <-done:
					return
				case <-hup:
					principals = reloadFromFile(ctx, srv, serveConfig, version, principals, os.Stderr)
				}
			}
		}()
		go func() {
			select {
			case <-ctx.Done():
				_ = srv.Close()
				shutdown, cancel := context.WithTimeout(context.Background(), 5*time.Second)
				defer cancel()
				_ = httpServer.Shutdown(shutdown)
			case <-done:
			}
		}()
		if cfg.TLSCertFile != "" {
			err = httpServer.ListenAndServeTLS(cfg.TLSCertFile, cfg.TLSKeyFile)
		} else {
			err = httpServer.ListenAndServe()
		}
		if errors.Is(err, http.ErrServerClosed) {
			return nil
		}
		return err
	},
}

// reloadFromFile applies the configuration at path to srv and writes one line to out. It returns
// the principal map now in force: the new one on success, the current one when the reload is refused.
func reloadFromFile(ctx context.Context, srv *garden.Server, path, version string, current map[string]string, out io.Writer) map[string]string {
	next, err := garden.LoadConfig(path)
	if err == nil {
		next.Version = version
		rctx, cancel := context.WithTimeout(ctx, 10*time.Second)
		err = srv.Reload(rctx, next)
		cancel()
	}
	if err != nil {
		fmt.Fprintf(out, "garden: reload of %s refused, keeping the running configuration: %v\n", path, err)
		return current
	}
	added, removed := 0, 0
	for id := range next.Principals {
		if _, ok := current[id]; !ok {
			added++
		}
	}
	for id := range current {
		if _, ok := next.Principals[id]; !ok {
			removed++
		}
	}
	fmt.Fprintf(out, "garden: reloaded %s: %d principal(s) added, %d removed\n", path, added, removed)
	return next.Principals
}

func init() {
	serveCmd.Flags().StringVar(&serveConfig, "config", "", "strict JSON Garden service configuration")
	_ = serveCmd.MarkFlagRequired("config")
	rootCmd.AddCommand(serveCmd)
}
