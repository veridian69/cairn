// Package edge is the inbound mirror of the relay: it validates a Cloudflare
// Access assertion and forwards to Cairn as one configured principal per path.
package edge

import (
	"bytes"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"path"
	"regexp"
	"strings"
	"time"

	"github.com/veridian69/cairn/cairn-mcp/internal/config"
	"github.com/veridian69/cairn/cairn-mcp/internal/credentials"
)

const maxConfigBytes = 16 * 1024

// Lowercase ASCII DNS labels of 1-63 characters, ending in cloudflareaccess.com
// with at least one label before it.
var teamDomainPattern = regexp.MustCompile(`^([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+cloudflareaccess\.com$`)

type fileConfig struct {
	TeamDomain            string            `json:"team_domain"`
	Audience              string            `json:"audience"`
	AllowedEmail          string            `json:"allowed_email"`
	UpstreamURL           string            `json:"upstream_url"`
	AllowHTTPUpstream     bool              `json:"allow_http_upstream"`
	Routes                map[string]string `json:"routes"`
	ReadTimeoutSeconds    float64           `json:"read_timeout_seconds"`
	ConnectTimeoutSeconds float64           `json:"connect_timeout_seconds"`
	JWKSTimeoutSeconds    float64           `json:"jwks_timeout_seconds"`
	JWKSRefreshSeconds    float64           `json:"jwks_refresh_seconds"`
	JWKSMaxAgeSeconds     float64           `json:"jwks_max_age_seconds"`
}

type Config struct {
	TeamDomain        string
	Audience          string
	AllowedEmail      string
	UpstreamURL       string
	AllowHTTPUpstream bool
	Routes            map[string]string
	ReadTimeout       time.Duration
	ConnectTimeout    time.Duration
	JWKSTimeout       time.Duration
	JWKSRefresh       time.Duration
	JWKSMaxAge        time.Duration
}

func (c Config) CertsURL() string { return "https://" + c.TeamDomain + "/cdn-cgi/access/certs" }
func (c Config) Issuer() string   { return "https://" + c.TeamDomain }

func seconds(value float64, fallback time.Duration, name string) (time.Duration, error) {
	if value == 0 {
		return fallback, nil
	}
	if value < 0 || value > 86400 {
		return 0, fmt.Errorf("%s must be between 0 and 86400 seconds", name)
	}
	d := time.Duration(value * float64(time.Second))
	if d <= 0 {
		return 0, fmt.Errorf("%s must be between 0 and 86400 seconds", name)
	}
	return d, nil
}

// LoadConfig reads an owner-only JSON file through the same hardened reader
// as a credential file. Unknown fields are refused so a typo cannot
// silently disable a check.
func LoadConfig(filePath string, expectedUID int) (Config, error) {
	data, err := credentials.ReadSecretFile(filePath, "edge config", expectedUID, maxConfigBytes)
	if err != nil {
		return Config{}, err
	}
	decoder := json.NewDecoder(bytes.NewReader(data))
	decoder.DisallowUnknownFields()
	var raw fileConfig
	if err := decoder.Decode(&raw); err != nil {
		return Config{}, errors.New("edge config is not valid JSON for this schema")
	}
	if err := decoder.Decode(&struct{}{}); err != io.EOF {
		return Config{}, errors.New("edge config is not valid JSON for this schema")
	}
	if !teamDomainPattern.MatchString(raw.TeamDomain) {
		return Config{}, errors.New("team_domain must be a lowercase <team>.cloudflareaccess.com host name")
	}
	if raw.Audience == "" {
		return Config{}, errors.New("audience is required")
	}
	if !strings.Contains(raw.AllowedEmail, "@") {
		return Config{}, errors.New("allowed_email must be an email address")
	}
	if err := config.ValidateUpstream(raw.UpstreamURL, raw.AllowHTTPUpstream); err != nil {
		return Config{}, err
	}
	if len(raw.Routes) == 0 {
		return Config{}, errors.New("routes must name at least one inbound path")
	}
	for inbound, credentialPath := range raw.Routes {
		if !strings.HasPrefix(inbound, "/") || path.Clean(inbound) != inbound || inbound == "/healthz" {
			return Config{}, errors.New("route paths must be clean absolute paths other than /healthz")
		}
		if !strings.HasPrefix(credentialPath, "/") {
			return Config{}, errors.New("route credential paths must be absolute")
		}
	}
	cfg := Config{
		TeamDomain:        raw.TeamDomain,
		Audience:          raw.Audience,
		AllowedEmail:      raw.AllowedEmail,
		UpstreamURL:       raw.UpstreamURL,
		AllowHTTPUpstream: raw.AllowHTTPUpstream,
		Routes:            raw.Routes,
	}
	if cfg.ReadTimeout, err = seconds(raw.ReadTimeoutSeconds, 300*time.Second, "read_timeout_seconds"); err != nil {
		return Config{}, err
	}
	if cfg.ConnectTimeout, err = seconds(raw.ConnectTimeoutSeconds, 5*time.Second, "connect_timeout_seconds"); err != nil {
		return Config{}, err
	}
	if cfg.JWKSTimeout, err = seconds(raw.JWKSTimeoutSeconds, 5*time.Second, "jwks_timeout_seconds"); err != nil {
		return Config{}, err
	}
	if cfg.JWKSRefresh, err = seconds(raw.JWKSRefreshSeconds, time.Hour, "jwks_refresh_seconds"); err != nil {
		return Config{}, err
	}
	if cfg.JWKSMaxAge, err = seconds(raw.JWKSMaxAgeSeconds, 24*time.Hour, "jwks_max_age_seconds"); err != nil {
		return Config{}, err
	}
	return cfg, nil
}
