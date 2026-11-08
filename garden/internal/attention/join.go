package attention

import (
	"bytes"
	"context"
	"crypto/sha256"
	"crypto/tls"
	"crypto/x509"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"encoding/pem"
	"errors"
	"fmt"
	"io"
	"io/fs"
	"net"
	"net/http"
	"net/url"
	"os"
	"path/filepath"
	"reflect"
	"sync"
	"time"

	"github.com/veridian69/cairn/garden/internal/identityseal"
	"github.com/veridian69/cairn/garden/internal/invite"
)

// Join redeems an invite at its gateway and installs the identity it returns in the Garden home.
// The gateway is trusted only through the CA the invite pins: its certificate chain must contain
// that CA, and its leaf must verify for the endpoint host against that CA alone. The home is
// checked before the invite is spent, and the identity is written only after every check against
// the response has passed.

const (
	joinCAFile  = "tls/internal-ca.pem"
	joinTimeout = 30 * time.Second
	joinMaxBody = 64 * 1024
)

// joinDialContext is a test seam. It lets a test map the endpoint host to a local listener while
// the certificate still names the real host.
var joinDialContext = (&net.Dialer{Timeout: joinTimeout}).DialContext

// JoinResult reports what a join did. It holds names and paths only, never the token or secret.
type JoinResult struct {
	Home       string
	Name       string
	Harness    string
	Sealed     bool
	Written    []string
	DefaultSet bool
}

// Join redeems code and installs the returned identity in the home at dir, creating the home when it
// is absent. The whole transaction runs under the home lock, so the checks made before redemption
// still hold when the identity is written. A refusal after redemption spends the invite and writes
// nothing.
func Join(ctx context.Context, dir string, code invite.Code) (JoinResult, error) {
	result := JoinResult{}
	dst, err := filepath.Abs(dir)
	if err != nil {
		return result, errors.New("invalid Garden home")
	}
	result.Home = dst
	root, err := ensureHome(dst)
	if err != nil {
		return result, err
	}
	defer closeRoot(root)
	unlock, err := lockHome(root)
	if err != nil {
		return result, err
	}
	defer unlock()

	existing, err := joinPreflight(root, dst, code.Endpoint)
	if err != nil {
		return result, err
	}
	// The code names the identity, so a name already here is refused before the invite is spent.
	// The gateway's answer must then agree with the code.
	if code.Name != "" {
		token, sealed, err := identityFiles(root, code.Name)
		if err != nil {
			return result, err
		}
		if token || sealed {
			return result, fmt.Errorf("identity %s is already in this home; the invite was not used", code.Name)
		}
	}
	enrolled, pinned, err := redeemInvite(ctx, code, func(der []byte) error {
		return joinCheckCA(root, existing != nil, der)
	})
	if err != nil {
		return result, err
	}

	// Everything the response decides is checked before the first write.
	resp := enrolled
	if !participantName.MatchString(resp.Name) {
		return result, spent(errInvalidIdentity)
	}
	if !invite.ValidHarness(resp.Harness) {
		return result, spent(errors.New("the gateway returned an unknown harness"))
	}
	if (code.Name != "" && resp.Name != code.Name) || (code.Name != "" && resp.Harness != code.Harness) {
		return result, spent(errors.New("the gateway's identity does not match the invite code"))
	}
	// A human (cli) identity arrives already sealed under its passphrase, chosen by the operator
	// at invite time, so the plain token never leaves the gateway host; any other identity
	// arrives as a token. Anything else is refused.
	if resp.Harness == "cli" {
		if resp.Token != "" || identityseal.Check([]byte(resp.SealedIdentity)) != nil {
			return result, spent(errors.New("the gateway did not return a sealed human identity"))
		}
	} else if resp.SealedIdentity != "" || !validToken(resp.Token) {
		return result, spent(errors.New("the gateway returned an invalid credential"))
	}
	binding := Profile{GardenEndpoint: code.Endpoint, GardenTLSCAFile: joinCAFile, InstanceID: resp.InstanceID, Scope: resp.Scope, Classification: resp.Classification}
	if err := validateBinding(binding); err != nil {
		return result, spent(fmt.Errorf("the gateway returned an invalid binding: %v", err))
	}
	if existing != nil && (existing.Config.InstanceID != resp.InstanceID || !reflect.DeepEqual(existing.Config.Scope, resp.Scope) || existing.Config.Classification != resp.Classification) {
		return result, spent(errors.New("this home is bound to a different instance, scope or classification than the invite"))
	}
	token, sealed, err := identityFiles(root, resp.Name)
	if err != nil {
		return result, spent(err)
	}
	if token || sealed {
		return result, spent(fmt.Errorf("identity %s is already in this home", resp.Name))
	}
	var defaultData []byte
	if existing != nil && resp.Harness != "" {
		defaultData, err = defaultsUpdate(root, resp.Harness, resp.Name)
		if err != nil {
			return result, spent(err)
		}
	}

	identityRel := "identities/" + resp.Name + ".token"
	identityData := []byte(resp.Token + "\n")
	if resp.Harness == "cli" {
		identityRel = "identities/" + resp.Name + ".sealed"
		identityData = []byte(resp.SealedIdentity)
		result.Sealed = true
	}

	// Write phase. The CA and the identity come first and garden.json last. Anything this join
	// created is removed, and a replaced garden.json restored, if a later step refuses.
	_, caPresent, err := rootReadFile(root, joinCAFile, 64*1024, false)
	if err != nil {
		return result, spent(err)
	}
	var created []string
	var original []byte
	replaced := false
	fail := func(err error) (JoinResult, error) {
		if replaced {
			_ = rootReplace(root, "garden.json", original)
		}
		for i := len(created) - 1; i >= 0; i-- {
			_ = root.Remove(created[i])
		}
		return result, spent(err)
	}
	if !caPresent {
		if err := rootPrivateDir(root, "tls"); err != nil {
			return fail(err)
		}
		if err := rootCreatePublic(root, joinCAFile, pinnedPEM(pinned)); err != nil {
			return fail(err)
		}
		created = append(created, joinCAFile)
		result.Written = append(result.Written, filepath.Join(dst, joinCAFile))
	}
	if err := rootPrivateDir(root, "identities"); err != nil {
		return fail(err)
	}
	if err := rootCreateNew(root, identityRel, identityData); err != nil {
		return fail(err)
	}
	created = append(created, identityRel)
	result.Written = append(result.Written, filepath.Join(dst, identityRel))

	switch {
	case existing == nil:
		config := HomeConfig{GardenEndpoint: code.Endpoint, GardenTLSCAFile: joinCAFile, InstanceID: resp.InstanceID, Scope: resp.Scope, Classification: resp.Classification}
		if resp.Harness != "" {
			config.Defaults = map[string]string{resp.Harness: resp.Name}
		}
		data, err := json.MarshalIndent(config, "", "  ")
		if err != nil {
			return fail(errors.New("cannot encode garden.json"))
		}
		made, err := rootPublishLink(root, "garden.json", append(data, '\n'), false)
		if err != nil {
			return fail(err)
		}
		if !made {
			return fail(errors.New("garden.json appeared while joining; nothing was changed"))
		}
		created = append(created, "garden.json")
		result.Written = append(result.Written, filepath.Join(dst, "garden.json"))
		result.DefaultSet = resp.Harness != ""
	case defaultData != nil:
		original, _, err = rootReadFile(root, "garden.json", 32*1024, false)
		if err != nil {
			return fail(err)
		}
		if err := rootReplace(root, "garden.json", defaultData); err != nil {
			return fail(err)
		}
		replaced = true
		result.Written = append(result.Written, filepath.Join(dst, "garden.json"))
		result.DefaultSet = true
	}

	// Verify what was written the way the runtime reads it.
	home, err := LoadHome(dst)
	if err != nil {
		return fail(fmt.Errorf("written garden.json does not load: %v", err))
	}
	if resp.Harness != "" && home.Config.Defaults[resp.Harness] != resp.Name {
		return fail(errors.New("written garden.json does not carry the harness default"))
	}
	if result.Sealed {
		data, _, err := rootReadFile(root, identityRel, identityseal.MaxSealedBytes, true)
		if err != nil || !bytes.Equal(data, identityData) {
			return fail(fmt.Errorf("installed identity %s is not readable", resp.Name))
		}
	} else {
		value, err := ReadCredential(home.TokenFile(resp.Name))
		if err != nil || value != resp.Token {
			return fail(fmt.Errorf("installed token for %s is not readable", resp.Name))
		}
	}
	result.Name = resp.Name
	result.Harness = resp.Harness
	return result, nil
}

// joinPreflight checks an existing home before the invite is spent. It returns the home's
// garden.json, or nil when the home has none yet. The CA file is checked later, against the
// certificate the gateway presents.
func joinPreflight(root *os.Root, dst, endpoint string) (*Home, error) {
	for _, dir := range []string{"tls", "identities"} {
		if err := rootExistingPrivateDir(root, dir); err != nil {
			return nil, err
		}
	}
	if _, err := root.Lstat("garden.json"); errors.Is(err, fs.ErrNotExist) {
		return nil, nil
	} else if err != nil {
		return nil, errors.New("cannot inspect garden.json")
	}
	home, err := LoadHome(dst)
	if err != nil {
		return nil, fmt.Errorf("the home's garden.json is not usable (%v); fix it, or join a new home", err)
	}
	if home.Config.GardenEndpoint != endpoint {
		return nil, fmt.Errorf("this home is bound to %s, not the invite's endpoint; the invite was not used", home.Config.GardenEndpoint)
	}
	if home.Config.GardenTLSCAFile != filepath.Join(dst, joinCAFile) {
		return nil, fmt.Errorf("this home's garden.json does not pin %s; the invite was not used", joinCAFile)
	}
	return &home, nil
}

// joinCheckCA compares the home's CA file with the CA the gateway presented. When garden.json
// names the file it must exist. A file that is present must hold exactly that one certificate.
func joinCheckCA(root *os.Root, required bool, pinned []byte) error {
	current, exists, err := rootReadFile(root, joinCAFile, 64*1024, false)
	if err != nil {
		return err
	}
	if !exists {
		if required {
			return fmt.Errorf("garden.json names %s, which is missing; the invite was not used", joinCAFile)
		}
		return nil
	}
	if !sameCertificate(current, pinned) {
		return fmt.Errorf("%s is a different certificate from the gateway's CA; the invite was not used", joinCAFile)
	}
	return nil
}

// sameCertificate reports whether pemData holds exactly one certificate and it is der.
func sameCertificate(pemData, der []byte) bool {
	block, rest := pem.Decode(pemData)
	if block == nil || block.Type != "CERTIFICATE" || !bytes.Equal(block.Bytes, der) {
		return false
	}
	return len(bytes.TrimSpace(rest)) == 0
}

// pinnedPEM encodes the pinned CA for tls/internal-ca.pem.
func pinnedPEM(der []byte) []byte {
	return pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: der})
}

// rootCreatePublic creates rel as a 0644 file inside root and refuses anything already present.
// The mode is set explicitly, so the process umask cannot narrow a public CA file.
func rootCreatePublic(root *os.Root, rel string, data []byte) error {
	file, err := root.OpenFile(rel, os.O_WRONLY|os.O_CREATE|os.O_EXCL, 0644)
	if err != nil {
		return fmt.Errorf("cannot create %s: %w", rel, err)
	}
	_, err = file.Write(data)
	if err == nil {
		err = file.Sync()
	}
	if closeErr := file.Close(); err == nil {
		err = closeErr
	}
	if err == nil {
		err = root.Chmod(rel, 0644)
	}
	if err != nil {
		_ = root.Remove(rel)
		return fmt.Errorf("cannot write %s", rel)
	}
	return nil
}

// spent marks a refusal after redemption. The invite is gone and nothing was written.
func spent(err error) error {
	return fmt.Errorf("%w; the invite was used and nothing was written, so ask the operator for a new one", err)
}

// redeemInvite posts the invite secret to the gateway's /enrol over TLS pinned to code.CASHA256.
// check runs during the handshake with the pinned CA's DER, before any request is written, so a
// refusal there posts nothing. It returns the decoded response and the pinned CA's DER.
func redeemInvite(ctx context.Context, code invite.Code, check func(caDER []byte) error) (invite.EnrolResponse, []byte, error) {
	enrol, err := url.Parse(code.EnrolURL())
	if err != nil {
		return invite.EnrolResponse{}, nil, errors.New("invalid invite endpoint")
	}
	host := enrol.Hostname()

	var (
		mu        sync.Mutex
		pinned    []byte
		verifyErr error
	)
	verify := func(state tls.ConnectionState) error {
		ca, err := verifyPinnedChain(state.PeerCertificates, code.CASHA256, host)
		if err == nil {
			err = check(ca.Raw)
		}
		mu.Lock()
		defer mu.Unlock()
		if err != nil {
			verifyErr = err
			return err
		}
		pinned = ca.Raw
		return nil
	}
	client := &http.Client{
		Timeout: joinTimeout,
		Transport: &http.Transport{
			DialContext:         joinDialContext,
			TLSClientConfig:     &tls.Config{MinVersion: tls.VersionTLS12, ServerName: host, InsecureSkipVerify: true, VerifyConnection: verify},
			TLSHandshakeTimeout: joinTimeout,
			DisableKeepAlives:   true,
		},
		CheckRedirect: func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse },
	}

	body, err := json.Marshal(invite.EnrolRequest{Secret: base64.RawURLEncoding.EncodeToString(code.Secret)})
	if err != nil {
		return invite.EnrolResponse{}, nil, errors.New("cannot encode the invite request")
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, code.EnrolURL(), bytes.NewReader(body))
	if err != nil {
		return invite.EnrolResponse{}, nil, errors.New("cannot build the invite request")
	}
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Accept", "application/json")
	reply, err := client.Do(req)
	if err != nil {
		mu.Lock()
		vErr := verifyErr
		mu.Unlock()
		if vErr != nil {
			return invite.EnrolResponse{}, nil, vErr
		}
		return invite.EnrolResponse{}, nil, fmt.Errorf("cannot redeem the invite at %s: %v", host, err)
	}
	defer reply.Body.Close()

	switch reply.StatusCode {
	case http.StatusOK:
	case http.StatusConflict:
		return invite.EnrolResponse{}, nil, errors.New("the gateway has not loaded this identity yet; ask the operator to reload it, then run join again (the invite was not used)")
	case http.StatusNotFound:
		return invite.EnrolResponse{}, nil, errors.New("the invite is unknown or has already been used")
	case http.StatusGone:
		return invite.EnrolResponse{}, nil, errors.New("the invite has expired; ask the operator for a new one")
	case http.StatusTooManyRequests:
		return invite.EnrolResponse{}, nil, errors.New("the gateway is limiting joins; try again in a minute")
	default:
		return invite.EnrolResponse{}, nil, fmt.Errorf("the gateway refused the invite (HTTP %d)", reply.StatusCode)
	}
	data, err := io.ReadAll(io.LimitReader(reply.Body, joinMaxBody+1))
	if err != nil || len(data) > joinMaxBody {
		return invite.EnrolResponse{}, nil, errors.New("the gateway's reply is unreadable or too large")
	}
	var out invite.EnrolResponse
	dec := json.NewDecoder(bytes.NewReader(data))
	dec.DisallowUnknownFields()
	if dec.Decode(&out) != nil {
		return invite.EnrolResponse{}, nil, errors.New("the gateway's reply is not a valid join response")
	}
	if _, err := dec.Token(); !errors.Is(err, io.EOF) {
		return invite.EnrolResponse{}, nil, errors.New("the gateway's reply is not a valid join response")
	}
	mu.Lock()
	defer mu.Unlock()
	if pinned == nil {
		return invite.EnrolResponse{}, nil, errors.New("the gateway's certificate was not verified")
	}
	return out, pinned, nil
}

// verifyPinnedChain returns the presented CA whose SHA-256 over its DER is digest. The leaf, the
// first certificate, must verify for host against that CA alone; no system roots are used.
func verifyPinnedChain(certs []*x509.Certificate, digest, host string) (*x509.Certificate, error) {
	if len(certs) == 0 {
		return nil, errors.New("the gateway presented no certificate")
	}
	var ca *x509.Certificate
	for _, cert := range certs {
		sum := sha256.Sum256(cert.Raw)
		if hex.EncodeToString(sum[:]) == digest && cert.IsCA && cert.BasicConstraintsValid {
			ca = cert
			break
		}
	}
	if ca == nil {
		return nil, errors.New("the gateway's certificate chain does not include the CA the invite names; the invite was not used")
	}
	roots := x509.NewCertPool()
	roots.AddCert(ca)
	intermediates := x509.NewCertPool()
	for _, cert := range certs[1:] {
		intermediates.AddCert(cert)
	}
	if _, err := certs[0].Verify(x509.VerifyOptions{DNSName: host, Roots: roots, Intermediates: intermediates, KeyUsages: []x509.ExtKeyUsage{x509.ExtKeyUsageServerAuth}}); err != nil {
		return nil, fmt.Errorf("the gateway's certificate does not verify for %s against the invite's CA; the invite was not used", host)
	}
	return ca, nil
}
