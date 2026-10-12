// cairn-edge maps a Cloudflare Access user login onto one Cairn principal per
// inbound path. It is the inbound mirror of the cairn-mcp relay and is
// deployed as a container beside Cairn, never installed as a client binary.
package main

import (
	"context"
	"errors"
	"flag"
	"fmt"
	"io"
	"log"
	"net"
	"net/http"
	"os"
	"os/signal"
	"strconv"
	"syscall"
	"time"

	"github.com/veridian69/cairn/cairn-mcp/internal/accessjwt"
	"github.com/veridian69/cairn/cairn-mcp/internal/edge"
)

const shutdownGrace = 10 * time.Second

func main() {
	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer stop()
	os.Exit(run(ctx, os.Args[1:], os.Stdout, os.Stderr))
}

func printUsage(w io.Writer) {
	fmt.Fprintln(w, "cairn-edge [serve|check] --config PATH [--bind-host 127.0.0.1] [--port 8790]")
}

// newJWKSClient never follows redirects: the fetched keys are trust anchors, so
// a 3xx must fail the fetch as a non-200 rather than lead somewhere else.
func newJWKSClient(timeout time.Duration) *http.Client {
	return &http.Client{
		Timeout:       timeout,
		CheckRedirect: func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse },
	}
}

func run(ctx context.Context, argv []string, stdout, stderr io.Writer) int {
	if len(argv) == 0 || argv[0] != "serve" && argv[0] != "check" {
		printUsage(stderr)
		return 2
	}
	command := argv[0]
	parser := flag.NewFlagSet("cairn-edge", flag.ContinueOnError)
	parser.SetOutput(io.Discard)
	var configPath, bindHost string
	var port int
	parser.StringVar(&configPath, "config", "/etc/cairn-edge/edge.json", "Edge configuration file")
	parser.StringVar(&bindHost, "bind-host", "127.0.0.1", "Bind interface")
	parser.IntVar(&port, "port", 8790, "Listen port")
	if err := parser.Parse(argv[1:]); err != nil {
		printUsage(stderr)
		return 2
	}
	cfg, err := edge.LoadConfig(configPath, os.Geteuid())
	if err != nil {
		fmt.Fprintln(stderr, "configuration invalid")
		return 1
	}
	tokens, err := edge.NewRouteTokens(cfg.Routes, os.Geteuid())
	if err != nil {
		fmt.Fprintln(stderr, "configuration invalid")
		return 1
	}
	if command == "check" {
		fmt.Fprintln(stdout, "configuration valid")
		return 0
	}
	logger := log.New(stderr, "", log.LstdFlags)
	jwksClient := newJWKSClient(cfg.JWKSTimeout)
	verifier := accessjwt.NewVerifier(cfg.CertsURL(), jwksClient, accessjwt.VerifierOptions{
		FetchTimeout: cfg.JWKSTimeout, RefreshEvery: cfg.JWKSRefresh, MaxAge: cfg.JWKSMaxAge,
	})
	if err := verifier.Fetch(ctx); err != nil {
		logger.Printf("initial key fetch failed; refusing to start")
		return 1
	}
	app := edge.NewApp(cfg, verifier, tokens, nil, logger)
	server := &http.Server{
		Handler:           app,
		ReadHeaderTimeout: 30 * time.Second,
		ReadTimeout:       cfg.ReadTimeout,
		WriteTimeout:      0,
	}
	listener, err := net.Listen("tcp", net.JoinHostPort(bindHost, strconv.Itoa(port)))
	if err != nil {
		logger.Printf("listen failed")
		return 1
	}
	runCtx, cancel := context.WithCancel(ctx)
	defer cancel()
	go verifier.Run(runCtx)
	reload := make(chan os.Signal, 1)
	signal.Notify(reload, syscall.SIGHUP)
	go func() {
		for range reload {
			if err := app.Reload(); err != nil {
				logger.Printf("reload failed; previous credentials kept")
			} else {
				logger.Printf("reloaded route credentials")
			}
		}
	}()
	app.SetStarted(true)
	serveErr := make(chan error, 1)
	go func() { serveErr <- server.Serve(listener) }()
	select {
	case <-ctx.Done():
	case err := <-serveErr:
		if !errors.Is(err, http.ErrServerClosed) {
			logger.Printf("serve failed")
			return 1
		}
	}
	app.SetStarted(false)
	shutdownCtx, shutdownCancel := context.WithTimeout(context.Background(), shutdownGrace)
	defer shutdownCancel()
	_ = server.Shutdown(shutdownCtx)
	return 0
}
