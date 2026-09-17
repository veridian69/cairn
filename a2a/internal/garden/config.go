package garden

import (
	"encoding/json"
	"errors"
	"io"
	"net"
	"net/netip"
	"os"
	"path/filepath"
	"reflect"
	"regexp"
	"strings"

	"github.com/google/uuid"
	"github.com/veridian69/cairn/a2a/internal/gardenauth"
)

// Config binds a single daemon and storage directory to Cairn authority.
type Config struct {
	Listen        string            `json:"listen"`
	DataDir       string            `json:"data_dir"`
	DaemonURLFile string            `json:"daemon_url_file"`
	TLSCertFile   string            `json:"tls_cert_file,omitempty"`
	TLSKeyFile    string            `json:"tls_key_file,omitempty"`
	Auth          gardenauth.Config `json:"auth"`
	Principals    map[string]string `json:"principals"`
}

// LoadConfig reads bounded JSON and rejects unknown fields or trailing values.
func LoadConfig(path string) (Config, error) {
	f, err := os.Open(path)
	if err != nil {
		return Config{}, err
	}
	defer f.Close()
	data, err := io.ReadAll(io.LimitReader(f, 128*1024+1))
	if err != nil || len(data) > 128*1024 {
		return Config{}, errors.New("invalid or oversized Garden configuration")
	}
	if err = checkJSON(data, reflect.TypeOf(Config{})); err != nil {
		return Config{}, errors.New("ambiguous Garden configuration JSON")
	}
	var cfg Config
	dec := json.NewDecoder(strings.NewReader(string(data)))
	dec.DisallowUnknownFields()
	if err = dec.Decode(&cfg); err != nil {
		return Config{}, errors.New("invalid Garden configuration JSON")
	}
	if dec.Decode(&struct{}{}) != io.EOF {
		return Config{}, errors.New("trailing Garden configuration JSON")
	}
	return cfg, cfg.validate()
}

var participantName = regexp.MustCompile(`^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,63}$`)

func canonicalUUID(s string) bool {
	u, err := uuid.Parse(s)
	return err == nil && u.String() == s && u != uuid.Nil
}
func (c Config) validate() error {
	host, _, err := net.SplitHostPort(c.Listen)
	if err != nil {
		return errors.New("Garden listen must be host:port")
	}
	if (c.TLSCertFile == "") != (c.TLSKeyFile == "") {
		return errors.New("both TLS certificate and key are required")
	}
	if c.TLSCertFile == "" {
		addr, e := netip.ParseAddr(host)
		if e != nil || !addr.IsLoopback() {
			return errors.New("plain HTTP requires numeric loopback listen address")
		}
	}
	if !filepath.IsAbs(c.DataDir) || !filepath.IsAbs(c.DaemonURLFile) {
		return errors.New("Garden data_dir and daemon_url_file must be absolute")
	}
	if len(c.Principals) == 0 || len(c.Principals) > 1000 {
		return errors.New("Garden requires 1..1000 principals")
	}
	names := map[string]bool{}
	for id, name := range c.Principals {
		if !canonicalUUID(id) || !participantName.MatchString(name) || names[name] {
			return errors.New("invalid or duplicate Garden principal mapping")
		}
		names[name] = true
	}
	_, err = gardenauth.New(c.Auth)
	return err
}
