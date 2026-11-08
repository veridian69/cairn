package invite

import (
	"crypto/ecdsa"
	"crypto/elliptic"
	"crypto/rand"
	"crypto/x509"
	"crypto/x509/pkix"
	"encoding/pem"
	"math/big"
	"net"
	"strings"
	"testing"
	"time"
)

func inviteTestKey(t *testing.T) *ecdsa.PrivateKey {
	t.Helper()
	key, err := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	return key
}

func inviteTestSerial(t *testing.T) *big.Int {
	t.Helper()
	serial, err := rand.Int(rand.Reader, big.NewInt(1<<62))
	if err != nil {
		t.Fatal(err)
	}
	return serial
}

// inviteTestCA returns a self-signed CA certificate and its key.
func inviteTestCA(t *testing.T, name string) (*x509.Certificate, *ecdsa.PrivateKey) {
	t.Helper()
	key := inviteTestKey(t)
	tmpl := &x509.Certificate{
		SerialNumber:          inviteTestSerial(t),
		Subject:               pkix.Name{CommonName: name},
		NotBefore:             time.Now().Add(-time.Hour),
		NotAfter:              time.Now().Add(24 * time.Hour),
		IsCA:                  true,
		BasicConstraintsValid: true,
		KeyUsage:              x509.KeyUsageCertSign | x509.KeyUsageCRLSign,
	}
	der, err := x509.CreateCertificate(rand.Reader, tmpl, tmpl, &key.PublicKey, key)
	if err != nil {
		t.Fatal(err)
	}
	cert, err := x509.ParseCertificate(der)
	if err != nil {
		t.Fatal(err)
	}
	return cert, key
}

// inviteTestLeaf returns a server certificate for dns names, signed by ca.
func inviteTestLeaf(t *testing.T, ca *x509.Certificate, caKey *ecdsa.PrivateKey, dns []string) *x509.Certificate {
	t.Helper()
	key := inviteTestKey(t)
	tmpl := &x509.Certificate{
		SerialNumber: inviteTestSerial(t),
		Subject:      pkix.Name{CommonName: dns[0]},
		DNSNames:     dns,
		IPAddresses:  []net.IP{net.ParseIP("127.0.0.1")},
		NotBefore:    time.Now().Add(-time.Hour),
		NotAfter:     time.Now().Add(24 * time.Hour),
		KeyUsage:     x509.KeyUsageDigitalSignature,
		ExtKeyUsage:  []x509.ExtKeyUsage{x509.ExtKeyUsageServerAuth},
	}
	der, err := x509.CreateCertificate(rand.Reader, tmpl, ca, &key.PublicKey, caKey)
	if err != nil {
		t.Fatal(err)
	}
	cert, err := x509.ParseCertificate(der)
	if err != nil {
		t.Fatal(err)
	}
	return cert
}

func inviteTestPEM(certs ...*x509.Certificate) []byte {
	var b strings.Builder
	for _, c := range certs {
		_ = pem.Encode(&b, &pem.Block{Type: "CERTIFICATE", Bytes: c.Raw})
	}
	return []byte(b.String())
}

func TestInvitePinCAFindsTheChainsCA(t *testing.T) {
	ca, caKey := inviteTestCA(t, "garden internal CA")
	leaf := inviteTestLeaf(t, ca, caKey, []string{"garden.test"})

	got, err := PinCA(inviteTestPEM(leaf, ca), "garden.test")
	if err != nil {
		t.Fatalf("PinCA refused a valid chain: %v", err)
	}
	if string(got.Raw) != string(ca.Raw) {
		t.Fatal("PinCA returned a different certificate")
	}
	if CADigest(got) != CADigest(ca) || len(CADigest(got)) != 64 {
		t.Fatalf("digest %q is not the SHA-256 of the CA DER", CADigest(got))
	}
}

func TestInvitePinCARefusesUnusableChains(t *testing.T) {
	ca, caKey := inviteTestCA(t, "garden internal CA")
	leaf := inviteTestLeaf(t, ca, caKey, []string{"garden.test"})
	otherCA, otherKey := inviteTestCA(t, "other CA")
	otherLeaf := inviteTestLeaf(t, otherCA, otherKey, []string{"garden.test"})

	cases := map[string][]byte{
		"leaf only":             inviteTestPEM(leaf),
		"empty":                 nil,
		"garbage":               []byte("not a certificate"),
		"CA first":              inviteTestPEM(ca, leaf),
		"CA for another signer": inviteTestPEM(leaf, otherCA),
		"other leaf and CA":     inviteTestPEM(otherLeaf, ca),
	}
	for name, chain := range cases {
		if got, err := PinCA(chain, "garden.test"); err == nil {
			t.Errorf("%s: PinCA accepted the chain and returned %v", name, got.Subject)
		}
	}
	if _, err := PinCA(inviteTestPEM(leaf, ca), "wrong.test"); err == nil {
		t.Error("PinCA accepted a leaf for a different host")
	}
}

func TestInvitePinCARefusesChainWithoutSelfSignedCA(t *testing.T) {
	ca, caKey := inviteTestCA(t, "garden internal CA")
	leaf := inviteTestLeaf(t, ca, caKey, []string{"garden.test"})
	_, err := PinCA(inviteTestPEM(leaf), "garden.test")
	if err == nil || !strings.Contains(err.Error(), "must end with its CA") {
		t.Fatalf("chain without CA: %v", err)
	}
}
