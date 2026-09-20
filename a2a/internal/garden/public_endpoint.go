package garden

import (
	"errors"
	"net/netip"
	"net/url"
	"regexp"
	"strconv"
	"strings"
)

// Resolvers may interpret one to four decimal, octal or hexadecimal pieces as
// IPv4. Only netip's canonical IP spelling is accepted before this DNS guard.
var legacyNumericHost = regexp.MustCompile(`^(?:[0-9]+|0x[0-9a-f]+)(?:\.(?:[0-9]+|0x[0-9a-f]+)){0,3}$`)

func authority(raw, scheme string) (string, int, error) {
	u, err := url.Parse("//" + raw)
	if err != nil || u.Host != raw || u.User != nil || u.Hostname() == "" || strings.ContainsAny(raw, "%\\") || strings.HasSuffix(raw, ":") {
		return "", 0, errors.New("invalid Garden authority")
	}
	host := strings.ToLower(u.Hostname())
	if addr, err := netip.ParseAddr(host); err == nil {
		host = addr.String()
	} else {
		if legacyNumericHost.MatchString(strings.TrimRight(host, ".")) {
			return "", 0, errors.New("Garden IP hostname must use canonical IP notation")
		}
		for _, ch := range host {
			if !(ch >= 'a' && ch <= 'z' || ch >= '0' && ch <= '9' || ch == '-' || ch == '.') {
				return "", 0, errors.New("invalid Garden hostname")
			}
		}
	}
	port := 443
	if scheme == "http" {
		port = 80
	}
	if u.Port() != "" {
		port, err = strconv.Atoi(u.Port())
		if err != nil || port < 1 || port > 65535 {
			return "", 0, errors.New("invalid Garden port")
		}
	}
	return host, port, nil
}

func endpointAuthority(endpoint string) (string, int, string, error) {
	u, err := url.Parse(endpoint)
	if err != nil || (u.Scheme != "http" && u.Scheme != "https") || u.User != nil || u.Path != "/mcp" || u.RawPath != "" || u.RawQuery != "" || u.ForceQuery || u.Fragment != "" {
		return "", 0, "", errors.New("Garden public_endpoint must be an HTTP(S) URL ending in /mcp without credentials, query or fragment")
	}
	host, port, err := authority(u.Host, u.Scheme)
	return host, port, u.Scheme, err
}

// Compare the advertised authority, never the listener address or forwarded
// headers. This permits port-forwarding without opening a DNS-rebinding bypass.
func matchesPublicAuthority(endpoint, requestHost string) bool {
	host, port, scheme, err := endpointAuthority(endpoint)
	if err != nil {
		return false
	}
	actualHost, actualPort, err := authority(requestHost, scheme)
	return err == nil && actualHost == host && actualPort == port
}
