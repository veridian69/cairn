package main

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log"
	"net"
	"net/http"
	"os"
	"os/signal"
	"strings"
	"syscall"

	benchmarking "github.com/veridian69/cairn/cairn-mcp/internal/benchmark"
	"github.com/veridian69/cairn/cairn-mcp/internal/bridge"
	"github.com/veridian69/cairn/cairn-mcp/internal/config"
	"github.com/veridian69/cairn/cairn-mcp/internal/credentials"
	"github.com/veridian69/cairn/cairn-mcp/internal/relay"
	"github.com/veridian69/cairn/cairn-mcp/internal/upstream"
)

func main() {
	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM, os.Interrupt)
	defer stop()
	os.Exit(run(ctx, os.Args[1:], os.Stdin, os.Stdout, os.Stderr))
}

type stdioOptions struct {
	endpoint         string
	clientIDPath     string
	clientSecretPath string
	httpClient       *http.Client
}

func run(ctx context.Context, argv []string, stdin io.ReadCloser, stdout, stderr io.Writer) int {
	return runWithBenchmark(ctx, argv, stdin, stdout, stderr, benchmarking.Run)
}

func runWithBenchmark(
	ctx context.Context,
	argv []string,
	stdin io.ReadCloser,
	stdout, stderr io.Writer,
	runBenchmark func(context.Context, benchmarking.Options) (benchmarking.Report, error),
) int {
	if len(argv) > 0 && argv[0] == "benchmark" {
		if hasHelp(argv[1:]) {
			benchmarking.PrintUsage(stdout)
			return 0
		}
		options, err := benchmarking.ParseOptions(argv[1:])
		if err != nil {
			fmt.Fprintf(stderr, "benchmark error: %v\n", err)
			return 2
		}
		report, err := runBenchmark(ctx, options)
		if err != nil {
			fmt.Fprintf(stderr, "benchmark error: %v\n", err)
			// A run that failed partway still retains the samples it took, and
			// repeating an authorised live run is expensive. The report already
			// carries passed=false, so this widens no contract.
			if report.Samples > 0 {
				_ = json.NewEncoder(stdout).Encode(report)
			}
			return 1
		}
		if err := json.NewEncoder(stdout).Encode(report); err != nil {
			fmt.Fprintln(stderr, "benchmark error: write report")
			return 1
		}
		if !report.Passed {
			return 1
		}
		return 0
	}
	if hasHelp(argv) {
		printUsage(stdout)
		return 0
	}
	if isUnknownCommand(argv) {
		fmt.Fprintf(stderr, "error: unknown command %q\n\n", argv[0])
		printUsage(stderr)
		return 2
	}

	command, cfg, err := config.ParseConfig(argv)
	if err != nil {
		fmt.Fprintf(stderr, "error: %v\n\n", err)
		printUsage(stderr)
		return 1
	}

	if command == "check" {
		if _, err := credentials.NewRelayStore(cfg.CFClientIDPath, cfg.CFClientSecretPath, cfg.LocalTokenPath); err != nil {
			fmt.Fprintln(stderr, "configuration invalid")
			return 1
		}
		fmt.Fprintln(stdout, "configuration valid")
		return 0
	}
	if command == "stdio" {
		client := relay.NewHTTPClient(cfg, 0)
		defer client.CloseIdleConnections()
		err := runStdio(ctx, stdin, stdout, stdioOptions{
			endpoint:         cfg.UpstreamURL,
			clientIDPath:     cfg.CFClientIDPath,
			clientSecretPath: cfg.CFClientSecretPath,
			httpClient:       client,
		})
		if err != nil {
			fmt.Fprintf(stderr, "stdio error: %v\n", err)
			return 1
		}
		return 0
	}

	store, err := credentials.NewRelayStore(cfg.CFClientIDPath, cfg.CFClientSecretPath, cfg.LocalTokenPath)
	if err != nil {
		fmt.Fprintln(stderr, "configuration invalid")
		return 1
	}

	logger := log.New(stderr, "", log.LstdFlags)
	app := relay.NewRelayApp(cfg, store, nil, logger)
	defer app.Close()

	server := newRelayHTTPServer(cfg, app, logger)

	listener, err := net.Listen("tcp", server.Addr)
	if err != nil {
		fmt.Fprintf(stderr, "listen error: %v\n", err)
		return 1
	}

	app.SetStarted(true)
	serverErrors := make(chan error, 1)
	go func() {
		serverErrors <- server.Serve(listener)
	}()

	sighup := make(chan os.Signal, 1)
	signal.Notify(sighup, syscall.SIGHUP)
	defer signal.Stop(sighup)

	for {
		select {
		case <-ctx.Done():
			shutdownCtx, cancel := context.WithTimeout(context.Background(), cfg.ShutdownGraceSeconds)
			app.SetStarted(false)
			_ = server.Shutdown(shutdownCtx)
			cancel()
			return 0
		case <-sighup:
			if err := app.ReloadCredentials(); err != nil {
				fmt.Fprintln(stderr, "credential reload rejected")
			} else {
				fmt.Fprintln(stderr, "credentials reloaded")
			}
		case err := <-serverErrors:
			if err != nil && !errors.Is(err, http.ErrServerClosed) {
				fmt.Fprintf(stderr, "server exited: %v\n", err)
				return 1
			}
			return 0
		}
	}
}

func newRelayHTTPServer(cfg config.RelayConfig, handler http.Handler, logger *log.Logger) *http.Server {
	return &http.Server{
		Addr:              net.JoinHostPort(cfg.BindHost, fmt.Sprintf("%d", cfg.Port)),
		Handler:           handler,
		ReadHeaderTimeout: cfg.HeaderReadTimeout,
		ReadTimeout:       cfg.ReadTimeout,
		WriteTimeout:      0,
		ErrorLog:          logger,
	}
}

func runStdio(ctx context.Context, stdin io.ReadCloser, stdout io.Writer, opts stdioOptions) error {
	store, err := credentials.NewAccessStore(opts.clientIDPath, opts.clientSecretPath)
	if err != nil {
		return err
	}
	session, err := upstream.NewSession(opts.endpoint, opts.httpClient, store)
	if err != nil {
		return err
	}
	return bridge.Run(ctx, stdin, stdout, session)
}

// isUnknownCommand runs after the benchmark branch has already returned, so the
// commands config accepts are the complete set of names still reachable here.
func isUnknownCommand(argv []string) bool {
	return len(argv) > 0 && !strings.HasPrefix(argv[0], "-") && !config.IsCommand(argv[0])
}

func hasHelp(argv []string) bool {
	for _, arg := range argv {
		if arg == "--help" || arg == "-h" {
			return true
		}
	}
	return false
}

func printUsage(w io.Writer) {
	fmt.Fprintln(w, "cairn-mcp [serve|stdio|check|benchmark] [options]")
	fmt.Fprintln(w)
	fmt.Fprintln(w, "commands:")
	fmt.Fprintln(w, "  serve        Run the relay (default)")
	fmt.Fprintln(w, "  stdio        Run the MCP stdio transport")
	fmt.Fprintln(w, "  check        Validate config and local credentials")
	fmt.Fprintln(w, "  benchmark    Run a machine-readable relay benchmark")
	fmt.Fprintln(w)
	fmt.Fprintln(w, "options:")
	fmt.Fprintln(w, "  --bind-host string (default 127.0.0.1)")
	fmt.Fprintln(w, "  --port int (default 8765)")
	fmt.Fprintln(w, "  --upstream-url string (default https://cairn.example.invalid/mcp)")
	fmt.Fprintln(w, "  --allow-http-upstream")
	fmt.Fprintln(w, "  --local-token-path string (default ~/.config/cairn/relay-token; serve and check only)")
	fmt.Fprintln(w, "  --cf-client-id-path string (default ~/.config/cairn/cf-access-client-id)")
	fmt.Fprintln(w, "  --cf-client-secret-path string (default ~/.config/cairn/cf-access-client-secret)")
	fmt.Fprintln(w, "  --connect-timeout float (seconds; upstream TCP connect, default 5)")
	fmt.Fprintln(w, "  --header-read-timeout float (seconds; inbound request headers, default 30)")
	fmt.Fprintln(w, "  --write-stall-timeout float (seconds; one stalled write to the client, default 30)")
	fmt.Fprintln(w, "  --read-timeout float (seconds; non-streaming request and upstream response header, default 300)")
	fmt.Fprintln(w, "  --stream-idle-timeout float (seconds; silence between reads on a GET stream, default 300)")
	fmt.Fprintln(w, "  --pool-timeout float (seconds; idle upstream connection lifetime, default 5)")
	fmt.Fprintln(w, "  --shutdown-grace float (seconds; graceful shutdown budget, default 10)")
	fmt.Fprintln(w, "  --help")
}
