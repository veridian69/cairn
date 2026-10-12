package garden

import (
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

// A real TLS listener supplies LocalAddrContextKey, unlike a recorder. This
// reproduces kubectl port-forward's loopback socket with the public HTTP Host.
func TestPublicEndpointThroughLoopbackTLS(t *testing.T) {
	for _, endpoint := range []string{"https://garden.example:9443/mcp", "https://garden.example/mcp", "https://[2001:db8::1]:9443/mcp"} {
		t.Run(endpoint, func(t *testing.T) {
			f := fixtureBeforeStart(t, func(f *fixture) {
				f.cfg.PublicEndpoint = endpoint
			})
			f.http.Close()
			f.http = httptest.NewTLSServer(f.srv.Handler())
			authority := strings.TrimSuffix(strings.TrimPrefix(endpoint, "https://"), "/mcp")
			hosts := []struct {
				host string
				want int
			}{{authority, 200}, {"evil.example:9443", 403}, {"127.0.0.1:9443", 403}, {authority + ".evil", 403}}
			if authority == "garden.example" {
				hosts = append(hosts, struct {
					host string
					want int
				}{"GARDEN.EXAMPLE:443", 200}, struct {
					host string
					want int
				}{"garden.example:8443", 403})
			}
			for _, tc := range hosts {
				req, err := http.NewRequest(http.MethodPost, f.http.URL+"/mcp", strings.NewReader(`{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-03-26","capabilities":{},"clientInfo":{"name":"test","version":"1"}}}`))
				if err != nil {
					t.Fatal(err)
				}
				req.Host = tc.host
				req.Header.Set("Authorization", "Bearer alice")
				req.Header.Set("Content-Type", "application/json")
				req.Header.Set("Accept", "application/json, text/event-stream")
				resp, err := f.http.Client().Do(req)
				if err != nil {
					t.Fatal(err)
				}
				body, _ := io.ReadAll(resp.Body)
				resp.Body.Close()
				if resp.StatusCode != tc.want {
					t.Errorf("Host %q: got %d want %d: %s", tc.host, resp.StatusCode, tc.want, body)
				}
				if tc.want == http.StatusOK && !strings.Contains(string(body), `"protocolVersion":"2025-03-26"`) {
					t.Errorf("Host %q did not complete MCP initialise: %s", tc.host, body)
				}
			}
		})
	}
}

func TestPublicEndpointRejectsMalformedConfiguration(t *testing.T) {
	f := newFixture(t)
	for _, endpoint := range []string{"https://garden.example/other", "https://user@garden.example/mcp", "https://garden.example/mcp?", "https://garden.example/mcp#fragment", "https://garden.example:0/mcp", "https://garden.example:65536/mcp", "https://garden.example:/mcp", "https://*/mcp", "https://garden.example/%6dcp", "file://garden.example/mcp"} {
		cfg := f.cfg
		cfg.PublicEndpoint = endpoint
		if err := cfg.validate(); err == nil {
			t.Errorf("accepted invalid public endpoint %q", endpoint)
		}
	}
}

func TestPublicAuthorityMatching(t *testing.T) {
	for _, tc := range []struct {
		endpoint, host string
		want           bool
	}{
		{"https://garden.example:9443/mcp", "garden.example:9443", true},
		{"https://garden.example:9443/mcp", "garden.example", false},
		{"https://garden.example/mcp", "garden.example:443", true},
		{"https://garden.example/mcp", "garden.example:443@evil.example", false},
		{"https://garden.example/mcp", "garden.example:443/", false},
		{"https://garden.example/mcp", "garden.example:443?x", false},
		{"https://garden.example/mcp", "garden.example:", false},
		{"https://garden.example/mcp", "garden.example:65536", false},
		{"https://garden.example/mcp", "garden.example.evil", false},
		{"http://garden.example/mcp", "garden.example:80", true},
		{"https://[2001:db8::1]/mcp", "[2001:0db8:0:0:0:0:0:1]:443", true},
		{"https://[2001:db8::1]/mcp", "[2001:db8::2]:443", false},
	} {
		if got := matchesPublicAuthority(tc.endpoint, tc.host); got != tc.want {
			t.Errorf("%q against %q: got %v want %v", tc.host, tc.endpoint, got, tc.want)
		}
	}
}

func TestUnconfiguredPublicEndpointPreservesLoopbackProtection(t *testing.T) {
	f := newFixture(t)
	req, _ := http.NewRequest(http.MethodPost, f.http.URL+"/mcp", strings.NewReader(`{}`))
	req.Host = "unconfigured.example"
	req.Header.Set("Authorization", "Bearer alice")
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Accept", "application/json, text/event-stream")
	resp, err := f.http.Client().Do(req)
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusForbidden {
		t.Fatalf("unconfigured external Host got %d", resp.StatusCode)
	}
}

func TestPublicEndpointRejectsAmbiguousNumericHosts(t *testing.T) {
	for _, host := range []string{"127.1", "2130706433", "0x7f000001", "0177.0.0.1", "127.0x0.0.1", "127.1.", "2130706433.", "0x7f000001.", "127.0.0.1."} {
		endpoint := "https://" + host + "/mcp"
		if _, _, _, err := endpointAuthority(endpoint); err == nil {
			t.Errorf("accepted legacy numeric IP hostname %q", host)
		}
	}
	for _, host := range []string{"127.0.0.1", "192.0.2.1", "[2001:db8::1]", "garden.example", "127.garden.example", "0x7f000001.example"} {
		endpoint := "https://" + host + "/mcp"
		if _, _, _, err := endpointAuthority(endpoint); err != nil {
			t.Errorf("rejected unambiguous hostname %q: %v", host, err)
		}
	}
}
