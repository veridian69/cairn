package garden

import (
	"context"
	"crypto/ed25519"
	"crypto/rand"
	"crypto/x509"
	"crypto/x509/pkix"
	"encoding/pem"
	"math/big"
	"os"
	"path/filepath"
	"testing"
	"time"
)

func testPublicCertificate(t *testing.T) (string, string) {
	t.Helper()
	pub, key, err := ed25519.GenerateKey(rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	template := &x509.Certificate{SerialNumber: big.NewInt(1), Subject: pkix.Name{CommonName: "garden.example.test"}, DNSNames: []string{"garden.example.test"}, NotBefore: time.Now().Add(-time.Hour), NotAfter: time.Now().Add(time.Hour), KeyUsage: x509.KeyUsageDigitalSignature | x509.KeyUsageCertSign, ExtKeyUsage: []x509.ExtKeyUsage{x509.ExtKeyUsageServerAuth}, BasicConstraintsValid: true, IsCA: true}
	der, err := x509.CreateCertificate(rand.Reader, template, template, pub, key)
	if err != nil {
		t.Fatal(err)
	}
	rawKey, err := x509.MarshalPKCS8PrivateKey(key)
	if err != nil {
		t.Fatal(err)
	}
	dir := t.TempDir()
	certFile, keyFile := filepath.Join(dir, "server.crt"), filepath.Join(dir, "server.key")
	if err = os.WriteFile(certFile, pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: der}), 0600); err != nil {
		t.Fatal(err)
	}
	if err = os.WriteFile(keyFile, pem.EncodeToMemory(&pem.Block{Type: "PRIVATE KEY", Bytes: rawKey}), 0600); err != nil {
		t.Fatal(err)
	}
	return certFile, keyFile
}
func TestTLSLoopbackVerificationPinsPublicCertificateName(t *testing.T) {
	f := newFixture(t)
	cfg := hostConfigForTest(t, f)
	cert, key := testPublicCertificate(t)
	cfg.Gateway.TLSCertFile = cert
	cfg.Gateway.TLSKeyFile = key
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	host, err := startHost(ctx, cfg)
	if err != nil {
		t.Fatal(err)
	}
	done := make(chan error, 1)
	go func() { done <- host.run(ctx) }()
	defer func() {
		cancel()
		if err := <-done; err != nil {
			t.Error(err)
		}
	}()
	endpoint := "https://" + host.listener.Addr().String() + "/mcp"
	good := ClientConfig{Endpoint: endpoint, Token: "alice", TLSCAFile: cert, TLSServerName: "garden.example.test"}
	client, err := Dial(ctx, good)
	if err != nil {
		t.Fatal(err)
	}
	status, err := client.Status(ctx)
	_ = client.Close()
	if err != nil || status.Participant != "alice" {
		t.Fatal("TLS verification did not reach authenticated status")
	}
	for _, bad := range []ClientConfig{{Endpoint: endpoint, Token: "alice", TLSCAFile: cert}, {Endpoint: endpoint, Token: "alice", TLSCAFile: cert, TLSServerName: "wrong.example.test"}, {Endpoint: endpoint, Token: "alice", TLSServerName: "garden.example.test"}} {
		client, err := Dial(ctx, bad)
		if client != nil {
			_ = client.Close()
		}
		requireCode(t, err, "unavailable")
	}
}
func TestTLSClientOverridesRejectInvalidConfiguration(t *testing.T) {
	for _, cfg := range []ClientConfig{{Endpoint: "http://127.0.0.1:1/mcp", Token: "token", TLSServerName: "garden.example.test"}, {Endpoint: "https://127.0.0.1:1/mcp", Token: "token", TLSServerName: "https://garden.example.test"}, {Endpoint: "https://127.0.0.1:1/mcp", Token: "token", TLSCAFile: "relative.pem"}} {
		_, err := Dial(context.Background(), cfg)
		requireCode(t, err, "invalid_argument")
	}
	bad := filepath.Join(t.TempDir(), "bad.pem")
	_ = os.WriteFile(bad, []byte("not a certificate"), 0600)
	_, err := Dial(context.Background(), ClientConfig{Endpoint: "https://127.0.0.1:1/mcp", Token: "token", TLSCAFile: bad})
	requireCode(t, err, "invalid_argument")
}
