package garden

import (
	"crypto/tls"
	"crypto/x509"
	"io"
	"net/netip"
	"os"
	"path/filepath"
	"regexp"
	"strings"
)

var tlsDNSName = regexp.MustCompile(`^[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?$`)

func clientTLS(cfg ClientConfig, scheme string) (*tls.Config, error) {
	if cfg.TLSCAFile == "" && cfg.TLSServerName == "" {
		return nil, nil
	}
	if scheme != "https" {
		return nil, failure("invalid_argument", "Garden TLS settings require an HTTPS endpoint")
	}
	if cfg.TLSServerName != "" {
		if _, err := netip.ParseAddr(cfg.TLSServerName); err != nil && (!tlsDNSName.MatchString(cfg.TLSServerName) || strings.Contains(cfg.TLSServerName, "..")) {
			return nil, failure("invalid_argument", "Invalid Garden TLS server name")
		}
	}
	config := &tls.Config{MinVersion: tls.VersionTLS12, ServerName: cfg.TLSServerName}
	if cfg.TLSCAFile != "" {
		if !filepath.IsAbs(cfg.TLSCAFile) {
			return nil, failure("invalid_argument", "Garden TLS CA file must be absolute")
		}
		info, err := os.Lstat(cfg.TLSCAFile)
		if err != nil || !info.Mode().IsRegular() {
			return nil, failure("invalid_argument", "Cannot read regular Garden TLS CA file")
		}
		f, err := os.Open(cfg.TLSCAFile)
		if err != nil {
			return nil, failure("invalid_argument", "Cannot read Garden TLS CA file")
		}
		defer f.Close()
		raw, err := io.ReadAll(io.LimitReader(f, 1024*1024+1))
		if err != nil || len(raw) > 1024*1024 {
			return nil, failure("invalid_argument", "Invalid or oversized Garden TLS CA file")
		}
		roots, err := x509.SystemCertPool()
		if err != nil {
			roots = x509.NewCertPool()
		}
		if !roots.AppendCertsFromPEM(raw) {
			return nil, failure("invalid_argument", "Garden TLS CA file contains no certificate")
		}
		config.RootCAs = roots
	}
	return config, nil
}
