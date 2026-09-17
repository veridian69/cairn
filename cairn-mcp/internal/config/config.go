package config

import (
	"errors"
	"flag"
	"fmt"
	"io"
	"math"
	"net/url"
	"os"
	"path/filepath"
	"strings"
	"time"
)

// DefaultUpstreamURL is the canonical Cairn MCP endpoint. Every command and the
// benchmark resolve their upstream default from here, so a retarget is a
// one-line change rather than a hunt through duplicated constants.
const DefaultUpstreamURL = "https://cairn.example.invalid/mcp"

// Credential file names inside the configuration directory.
const (
	RelayTokenFileName     = "relay-token"
	CFClientIDFileName     = "cf-access-client-id"
	CFClientSecretFileName = "cf-access-client-secret"
)

// commands is the single authority on the commands ParseConfig accepts, in the
// order the usage text lists them. benchmark is dispatched in main before
// ParseConfig ever sees argv, so it is deliberately absent here. It stays
// unexported so no caller can mutate the accepted set.
var commands = []string{"serve", "stdio", "check"}

// IsCommand reports whether name is a command ParseConfig accepts.
func IsCommand(name string) bool {
	for _, candidate := range commands {
		if name == candidate {
			return true
		}
	}
	return false
}

type RelayConfig struct {
	BindHost             string
	Port                 int
	UpstreamURL          string
	AllowHTTPUpstream    bool
	LocalTokenPath       string
	CFClientIDPath       string
	CFClientSecretPath   string
	ConnectTimeout       time.Duration
	HeaderReadTimeout    time.Duration
	WriteStallTimeout    time.Duration
	ReadTimeout          time.Duration
	StreamIdleTimeout    time.Duration
	PoolTimeout          time.Duration
	ShutdownGraceSeconds time.Duration
}

func ParseConfig(argv []string) (string, RelayConfig, error) {
	command := "serve"
	args := argv
	if len(args) > 0 && !strings.HasPrefix(args[0], "-") {
		command = args[0]
		args = args[1:]
	}
	if !IsCommand(command) {
		return "", RelayConfig{}, fmt.Errorf("command must be one of %s", strings.Join(commands, ", "))
	}

	configDir, err := DefaultConfigDir()
	if err != nil {
		return "", RelayConfig{}, err
	}
	var (
		bindHost       string
		port           int
		upstream       string
		allowHTTP      bool
		localTokenPath = ""
		cfIDPath       string
		cfSecretPath   string
		connectTimeout float64
		headerRead     float64
		writeStall     float64
		readTimeout    float64
		streamIdle     float64
		poolTimeout    float64
		shutdownGrace  float64
	)
	if command != "stdio" {
		localTokenPath = filepath.Join(configDir, RelayTokenFileName)
	}

	parser := flag.NewFlagSet("cairn-mcp", flag.ContinueOnError)
	parser.SetOutput(io.Discard)
	parser.StringVar(&bindHost, "bind-host", "127.0.0.1", "Loopback bind interface")
	parser.IntVar(&port, "port", 8765, "Port for loopback relay")
	parser.StringVar(&upstream, "upstream-url", DefaultUpstreamURL, "Target upstream URL")
	parser.BoolVar(&allowHTTP, "allow-http-upstream", false, "Allow HTTP upstream URL")
	parser.StringVar(&localTokenPath, "local-token-path", localTokenPath, "Local bearer token path")
	parser.StringVar(&cfIDPath, "cf-client-id-path", filepath.Join(configDir, CFClientIDFileName), "Cloudflare client id path")
	parser.StringVar(&cfSecretPath, "cf-client-secret-path", filepath.Join(configDir, CFClientSecretFileName), "Cloudflare client secret path")
	parser.Float64Var(&connectTimeout, "connect-timeout", 5.0, "Upstream TCP connect timeout (seconds)")
	parser.Float64Var(&headerRead, "header-read-timeout", 30.0, "Time allowed to read an inbound request's headers (seconds)")
	parser.Float64Var(&writeStall, "write-stall-timeout", 30.0, "Maximum time a single write to the client may stall (seconds)")
	parser.Float64Var(&readTimeout, "read-timeout", 300.0, "Budget for a non-streaming request and for an upstream response header (seconds)")
	parser.Float64Var(&streamIdle, "stream-idle-timeout", 300.0, "Maximum silence between reads on a GET stream (seconds)")
	parser.Float64Var(&poolTimeout, "pool-timeout", 5.0, "Idle upstream connection lifetime in the HTTP pool (seconds)")
	parser.Float64Var(&shutdownGrace, "shutdown-grace", 10.0, "Shutdown grace (seconds)")

	if err := parser.Parse(args); err != nil {
		return "", RelayConfig{}, fmt.Errorf("cannot parse flags: %w", err)
	}

	if bindHost != "127.0.0.1" {
		return "", RelayConfig{}, errors.New("bind host must be 127.0.0.1")
	}
	if port < 1 || port > 65535 {
		return "", RelayConfig{}, errors.New("port must be between 1 and 65535")
	}
	if err := ValidateUpstream(upstream, allowHTTP); err != nil {
		return "", RelayConfig{}, err
	}

	connect, err := positiveSeconds("connect-timeout", connectTimeout)
	if err != nil {
		return "", RelayConfig{}, err
	}
	header, err := positiveSeconds("header-read-timeout", headerRead)
	if err != nil {
		return "", RelayConfig{}, err
	}
	stall, err := positiveSeconds("write-stall-timeout", writeStall)
	if err != nil {
		return "", RelayConfig{}, err
	}
	read, err := positiveSeconds("read-timeout", readTimeout)
	if err != nil {
		return "", RelayConfig{}, err
	}
	idle, err := positiveSeconds("stream-idle-timeout", streamIdle)
	if err != nil {
		return "", RelayConfig{}, err
	}
	pool, err := positiveSeconds("pool-timeout", poolTimeout)
	if err != nil {
		return "", RelayConfig{}, err
	}
	shutdown, err := positiveSeconds("shutdown-grace", shutdownGrace)
	if err != nil {
		return "", RelayConfig{}, err
	}

	return command, RelayConfig{
		BindHost:             bindHost,
		Port:                 port,
		UpstreamURL:          upstream,
		AllowHTTPUpstream:    allowHTTP,
		LocalTokenPath:       localTokenPath,
		CFClientIDPath:       cfIDPath,
		CFClientSecretPath:   cfSecretPath,
		ConnectTimeout:       connect,
		HeaderReadTimeout:    header,
		WriteStallTimeout:    stall,
		ReadTimeout:          read,
		StreamIdleTimeout:    idle,
		PoolTimeout:          pool,
		ShutdownGraceSeconds: shutdown,
	}, nil
}

// ValidateUpstream is the single authority on acceptable upstream URLs.
func ValidateUpstream(value string, allowHTTP bool) error {
	parsed, err := url.Parse(value)
	if err != nil {
		return fmt.Errorf("upstream URL must be an absolute HTTP(S) URL")
	}
	if parsed.Scheme != "http" && parsed.Scheme != "https" {
		return errors.New("upstream URL must be an absolute HTTP(S) URL")
	}
	if parsed.Hostname() == "" {
		return errors.New("upstream URL must be an absolute HTTP(S) URL")
	}
	if parsed.User != nil {
		return errors.New("upstream URL must not contain credentials")
	}
	if parsed.RawQuery != "" || parsed.Fragment != "" {
		return errors.New("upstream URL must not contain a query or fragment")
	}
	if parsed.Scheme == "http" && !allowHTTP {
		return errors.New("HTTP upstream requires --allow-http-upstream")
	}
	return nil
}

// DefaultConfigDir resolves the per-user configuration directory. An
// unavailable home directory is an error rather than a guess: guessing sent a
// stripped environment (cron, a systemd unit without HOME) at root's real
// credential files under an unconfirmed principal.
func DefaultConfigDir() (string, error) {
	home, err := os.UserHomeDir()
	if err != nil {
		return "", errors.New("cannot resolve the home directory for the configuration path")
	}
	return filepath.Join(home, ".config", "cairn"), nil
}

func positiveSeconds(name string, value float64) (time.Duration, error) {
	if math.IsNaN(value) || math.IsInf(value, 0) {
		return 0, fmt.Errorf("%s must be a number", name)
	}
	if value <= 0 {
		return 0, fmt.Errorf("%s must be greater than zero", name)
	}
	return time.Duration(value * float64(time.Second)), nil
}
