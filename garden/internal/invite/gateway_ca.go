package invite

import (
	"bytes"
	"crypto/sha256"
	"crypto/x509"
	"encoding/hex"
	"encoding/pem"
	"errors"
)

// PinCA finds the CA that clients must pin in the gateway's TLS chain file. The chain is the
// server certificate followed by its CA. The CA is the first certificate that is a self-signed CA
// and verifies the server certificate for host, using that CA as the only root.
func PinCA(chainPEM []byte, host string) (*x509.Certificate, error) {
	var certs []*x509.Certificate
	for rest := chainPEM; ; {
		var block *pem.Block
		block, rest = pem.Decode(rest)
		if block == nil {
			break
		}
		if block.Type != "CERTIFICATE" {
			continue
		}
		cert, err := x509.ParseCertificate(block.Bytes)
		if err != nil {
			return nil, errors.New("the TLS chain contains an invalid certificate")
		}
		certs = append(certs, cert)
	}
	if len(certs) < 2 {
		return nil, errors.New("the TLS chain must end with its CA")
	}
	leaf := certs[0]
	if leaf.IsCA {
		return nil, errors.New("the TLS chain must start with the server certificate")
	}
	sawCA := false
	for i := 1; i < len(certs); i++ {
		ca := certs[i]
		if !ca.IsCA || !bytes.Equal(ca.RawIssuer, ca.RawSubject) || ca.CheckSignatureFrom(ca) != nil {
			continue
		}
		sawCA = true
		roots := x509.NewCertPool()
		roots.AddCert(ca)
		others := x509.NewCertPool()
		for j, c := range certs {
			if j != 0 && j != i {
				others.AddCert(c)
			}
		}
		_, err := leaf.Verify(x509.VerifyOptions{
			DNSName:       host,
			Roots:         roots,
			Intermediates: others,
			KeyUsages:     []x509.ExtKeyUsage{x509.ExtKeyUsageServerAuth},
		})
		if err == nil {
			return ca, nil
		}
	}
	if !sawCA {
		return nil, errors.New("the TLS chain must end with its CA")
	}
	return nil, errors.New("the TLS server certificate does not verify for the public endpoint host against the CA in the chain")
}

// CADigest returns the hex SHA-256 of the CA's DER encoding, the value an invite code pins.
func CADigest(ca *x509.Certificate) string {
	sum := sha256.Sum256(ca.Raw)
	return hex.EncodeToString(sum[:])
}
