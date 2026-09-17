package cmd

import (
	"context"
	"errors"
	"net/http"
	"os/signal"
	"path/filepath"
	"syscall"
	"time"

	"github.com/spf13/cobra"
	"github.com/veridian69/cairn/a2a/internal/garden"
	"github.com/veridian69/cairn/a2a/internal/maintenance"
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

func init() {
	serveCmd.Flags().StringVar(&serveConfig, "config", "", "strict JSON Garden service configuration")
	_ = serveCmd.MarkFlagRequired("config")
	rootCmd.AddCommand(serveCmd)
}
