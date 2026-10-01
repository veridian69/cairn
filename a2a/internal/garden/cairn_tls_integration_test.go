package garden

import (
	"bufio"
	"context"
	"crypto/rand"
	"crypto/rsa"
	"crypto/tls"
	"crypto/x509"
	"crypto/x509/pkix"
	"encoding/json"
	"encoding/pem"
	"errors"
	"math/big"
	"net"
	"net/http/httptest"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/veridian69/cairn/a2a/internal/gardenauth"
	"github.com/veridian69/cairn/a2a/internal/transport"
)

type tlsFixture struct {
	Endpoint       string           `json:"endpoint"`
	InstanceID     string           `json:"instance_id"`
	Scope          gardenauth.Scope `json:"scope"`
	Classification string           `json:"classification"`
	Actors         map[string]struct {
		ID    string `json:"id"`
		Token string `json:"token"`
	} `json:"actors"`
}

func TestRealCairnGardenTLSCompatibility(t *testing.T) {
	python := os.Getenv("GARDEN_CAIRN_PYTHON")
	if python == "" {
		t.Skip("set GARDEN_CAIRN_PYTHON to run the disposable TLS compatibility fixture")
	}
	if os.Getenv("GARDEN_TLS_CHILD") != "" {
		runTLSCompatibilityChild(t, python)
		return
	}
	cert, key, root := makeTLSFixture(t)
	emptyRoots := t.TempDir()
	_, _, wrongRoot := makeTLSFixture(t)
	for mode, ca := range map[string]string{"trusted": root, "untrusted": wrongRoot} {
		t.Run(mode, func(t *testing.T) {
			ctx, cancel := context.WithTimeout(context.Background(), 45*time.Second)
			defer cancel()
			command := exec.CommandContext(ctx, os.Args[0], "-test.run=^TestRealCairnGardenTLSCompatibility$")
			command.Env = []string{
				"PATH=/usr/bin:/bin", "HOME=" + t.TempDir(), "PYTHONNOUSERSITE=1", "PYTHONDONTWRITEBYTECODE=1",
				"GARDEN_CAIRN_PYTHON=" + python, "GARDEN_TLS_CHILD=" + mode,
				"GARDEN_FIXTURE_TLS_CERT=" + cert, "GARDEN_FIXTURE_TLS_KEY=" + key,
				"SSL_CERT_FILE=" + ca, "SSL_CERT_DIR=" + emptyRoots,
			}
			err := command.Run()
			if err != nil {
				t.Fatalf("%s disposable TLS fixture failed", mode)
			}
		})
	}
}

func runTLSCompatibilityChild(t *testing.T, python string) {
	root, err := filepath.Abs("../../..")
	if err != nil {
		t.Fatal(err)
	}
	ctx, cancel := context.WithTimeout(context.Background(), 35*time.Second)
	defer cancel()
	command := exec.CommandContext(ctx, python, filepath.Join(root, "a2a", "integration", "cairn_fixture.py"))
	command.Dir = root
	command.Env = []string{
		"PATH=/usr/bin:/bin", "HOME=" + t.TempDir(), "PYTHONPATH=" + filepath.Join(root, "src"),
		"PYTHONNOUSERSITE=1", "PYTHONDONTWRITEBYTECODE=1",
		"GARDEN_FIXTURE_TLS_CERT=" + os.Getenv("GARDEN_FIXTURE_TLS_CERT"),
		"GARDEN_FIXTURE_TLS_KEY=" + os.Getenv("GARDEN_FIXTURE_TLS_KEY"),
		"SSL_CERT_FILE=" + os.Getenv("SSL_CERT_FILE"), "SSL_CERT_DIR=" + os.Getenv("SSL_CERT_DIR"),
	}
	stdin, err := command.StdinPipe()
	if err != nil {
		t.Fatal(err)
	}
	stdout, err := command.StdoutPipe()
	if err != nil {
		t.Fatal(err)
	}
	if err = command.Start(); err != nil {
		t.Fatal(err)
	}
	defer func() {
		_ = stdin.Close()
		if err := command.Wait(); err != nil || ctx.Err() != nil {
			t.Error("disposable TLS fixture did not stop cleanly")
		}
	}()
	scanner := bufio.NewScanner(stdout)
	scanner.Buffer(make([]byte, 4096), 64*1024)
	if !scanner.Scan() {
		t.Fatal("TLS fixture did not publish configuration")
	}
	var fixture tlsFixture
	if json.Unmarshal(scanner.Bytes(), &fixture) != nil || fixture.Actors["alice"].Token == "" {
		t.Fatal("invalid TLS fixture configuration")
	}
	config := gardenauth.Config{Endpoint: fixture.Endpoint, InstanceID: fixture.InstanceID, Scope: fixture.Scope, Classification: fixture.Classification}
	validator, err := gardenauth.New(config)
	if err != nil {
		t.Fatal(err)
	}
	if os.Getenv("GARDEN_TLS_CHILD") == "untrusted" {
		if _, err := validator.Authenticate(ctx, fixture.Actors["alice"].Token); !errors.Is(err, gardenauth.ErrUnavailable) {
			t.Fatal("untrusted certificate reached authority")
		}
		return
	}
	if _, err := validator.Authenticate(ctx, fixture.Actors["alice"].Token); err != nil {
		t.Fatal("trusted HTTPS diagnosis failed")
	}
	wrongName := config
	wrongName.Endpoint = strings.Replace(config.Endpoint, "127.0.0.1", "localhost", 1)
	wrong, err := gardenauth.New(wrongName)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := wrong.Authenticate(ctx, fixture.Actors["alice"].Token); !errors.Is(err, gardenauth.ErrUnavailable) {
		t.Fatal("wrong certificate DNS name authenticated")
	}

	dir := t.TempDir()
	nats, err := transport.NewServer(dir)
	if err != nil {
		t.Fatal(err)
	}
	defer nats.Stop()
	stream, err := transport.NewStream(nats.ClientURL())
	if err != nil {
		t.Fatal(err)
	}
	defer stream.Close()
	urlFile := filepath.Join(dir, "daemon.url")
	if err = os.WriteFile(urlFile, []byte(nats.ClientURL()), 0600); err != nil {
		t.Fatal(err)
	}
	server, err := New(ctx, Config{Listen: "127.0.0.1:0", DataDir: dir, DaemonURLFile: urlFile, Auth: config, Principals: map[string]string{fixture.Actors["alice"].ID: "alice"}})
	if err != nil {
		t.Fatal(err)
	}
	defer server.Close()
	certificate, err := tls.LoadX509KeyPair(os.Getenv("GARDEN_FIXTURE_TLS_CERT"), os.Getenv("GARDEN_FIXTURE_TLS_KEY"))
	if err != nil {
		t.Fatal(err)
	}
	httpServer := httptest.NewUnstartedServer(server.Handler())
	httpServer.TLS = &tls.Config{Certificates: []tls.Certificate{certificate}, MinVersion: tls.VersionTLS12}
	httpServer.StartTLS()
	defer httpServer.Close()
	client, err := Dial(ctx, ClientConfig{Endpoint: httpServer.URL + "/mcp", Token: fixture.Actors["alice"].Token})
	if err != nil {
		t.Fatal("real Garden TLS client rejected trusted server")
	}
	defer client.Close()
	if _, err = client.Status(ctx); err != nil {
		t.Fatal("real Garden TLS status failed")
	}
}

func makeTLSFixture(t *testing.T) (certPath, keyPath, rootPath string) {
	t.Helper()
	caKey, err := rsa.GenerateKey(rand.Reader, 2048)
	if err != nil {
		t.Fatal(err)
	}
	now := time.Now()
	ca := &x509.Certificate{SerialNumber: big.NewInt(1), Subject: pkix.Name{CommonName: "garden test CA"}, NotBefore: now.Add(-time.Minute), NotAfter: now.Add(time.Hour), IsCA: true, BasicConstraintsValid: true, KeyUsage: x509.KeyUsageCertSign | x509.KeyUsageDigitalSignature}
	caDER, err := x509.CreateCertificate(rand.Reader, ca, ca, &caKey.PublicKey, caKey)
	if err != nil {
		t.Fatal(err)
	}
	serverKey, err := rsa.GenerateKey(rand.Reader, 2048)
	if err != nil {
		t.Fatal(err)
	}
	leaf := &x509.Certificate{SerialNumber: big.NewInt(2), Subject: pkix.Name{CommonName: "garden.fixture.test"}, DNSNames: []string{"garden.fixture.test"}, IPAddresses: []net.IP{net.ParseIP("127.0.0.1")}, NotBefore: now.Add(-time.Minute), NotAfter: now.Add(time.Hour), KeyUsage: x509.KeyUsageDigitalSignature, ExtKeyUsage: []x509.ExtKeyUsage{x509.ExtKeyUsageServerAuth}, BasicConstraintsValid: true}
	leafDER, err := x509.CreateCertificate(rand.Reader, leaf, ca, &serverKey.PublicKey, caKey)
	if err != nil {
		t.Fatal(err)
	}
	dir := t.TempDir()
	certPath, keyPath = filepath.Join(dir, "server.pem"), filepath.Join(dir, "server-key.pem")
	root := pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: caDER})
	rootPath = filepath.Join(dir, "ca.pem")
	if err = os.WriteFile(rootPath, root, 0600); err != nil {
		t.Fatal(err)
	}
	if err = os.WriteFile(certPath, append(pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: leafDER}), root...), 0600); err != nil {
		t.Fatal(err)
	}
	key, err := x509.MarshalPKCS8PrivateKey(serverKey)
	if err != nil {
		t.Fatal(err)
	}
	if err = os.WriteFile(keyPath, pem.EncodeToMemory(&pem.Block{Type: "PRIVATE KEY", Bytes: key}), 0600); err != nil {
		t.Fatal(err)
	}
	return certPath, keyPath, rootPath
}
