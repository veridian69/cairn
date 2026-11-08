package cmd

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"io/fs"
	"net/url"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"strings"
	"syscall"
	"time"

	"github.com/spf13/cobra"
	"github.com/veridian69/cairn/garden/internal/attention"
	"github.com/veridian69/cairn/garden/internal/garden"
	"github.com/veridian69/cairn/garden/internal/gardenauth"
	"github.com/veridian69/cairn/garden/internal/identityseal"
	"github.com/veridian69/cairn/garden/internal/invite"
)

func init() { rootCmd.AddCommand(newInviteCommand()) }

const (
	maxChainBytes       = 1 << 20
	inviteRevokeReason  = "garden_invite_failed"
	reissueRevokeReason = "garden_invite_reissued"
	reloadTimeout       = 60 * time.Second
)

var (
	// runCommand runs an external command. Tests replace it so that nothing reaches systemctl.
	// readPassphrase asks for a cli identity's passphrase on the terminal. Tests replace it.
	readPassphrase = attention.ReadPassphrase
	runCommand     = func(ctx context.Context, name string, args ...string) ([]byte, error) {
		return exec.CommandContext(ctx, name, args...).CombinedOutput()
	}
	unitPattern = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9@_.:-]{0,127}$`)
)

type inviteOptions struct {
	name           string
	harness        string
	adminTokenFile string
	gatewayConfig  string
	cairnURL       string
	ttl            time.Duration
	expires        string
	reloadUnit     string
	reissue        bool
}

func newInviteCommand() *cobra.Command {
	var o inviteOptions
	command := &cobra.Command{
		Use:   "invite NAME",
		Short: "Issue a single-use invite that a new machine redeems with garden join",
		Long: "Run on the gateway host as root. Creates a workload principal, one grant and one credential in\n" +
			"Cairn, checks the credential against the gateway's scope, seals it in the invites directory,\n" +
			"adds the principal to the gateway configuration and reloads the gateway. The code on stdout\n" +
			"carries the endpoint, the CA pin and the secret; the admin credential and the new token are\n" +
			"never printed.",
		Example:           "  garden invite zen --harness opencode --admin-token-file /root/cairn-admin.token",
		Args:              cobra.ExactArgs(1),
		PersistentPreRunE: func(*cobra.Command, []string) error { return nil },
		RunE: func(cmd *cobra.Command, args []string) error {
			o.name = args[0]
			return runInvite(cmd, o, time.Now())
		},
	}
	f := command.Flags()
	f.StringVar(&o.harness, "harness", "", "Make the identity the default for claude, codex, opencode or cli")
	f.StringVar(&o.adminTokenFile, "admin-token-file", "", "Cairn administrator credential file (mode 0600, owned by you), or - to read it from standard input")
	f.StringVar(&o.gatewayConfig, "gateway-config", "/etc/garden/garden.json", "Gateway configuration to update")
	f.StringVar(&o.cairnURL, "cairn-url", "", "Cairn admin API origin (default: scheme and host of auth.endpoint)")
	f.DurationVar(&o.ttl, "ttl", 24*time.Hour, "How long the invite stays redeemable; at most 168h")
	f.StringVar(&o.expires, "expires", "", "Credential and grant expiry: a duration such as 8760h, or an RFC3339 time (default one year)")
	f.StringVar(&o.reloadUnit, "reload-unit", "garden-mcp.service", "Unit to reload after the update; empty skips the reload")
	f.BoolVar(&o.reissue, "reissue", false, "Issue a new credential for NAME, which must already be mapped, and revoke the earlier ones recorded in the ledger")
	_ = command.MarkFlagRequired("admin-token-file")
	return command
}

// issuance records what the admin API has created so far, so that a failure can revoke it.
type issuance struct {
	admin             *invite.AdminClient
	realm             string
	principal         string
	grant             string
	credential        string
	grantUnknown      bool
	credentialUnknown bool
	recordPath        string
}

// fail removes the invite record, revokes the credential and then the grant, best effort, and
// returns err with every ID so that the operator can check Cairn.
func (i *issuance) fail(err error) error {
	var notes []string
	if i.recordPath != "" {
		if rerr := os.Remove(i.recordPath); rerr == nil || errors.Is(rerr, os.ErrNotExist) {
			notes = append(notes, "invite record removed")
		} else {
			notes = append(notes, "invite record not removed: "+rerr.Error())
		}
	}
	if i.credential != "" {
		notes = append(notes, revokeNote("credential", i.credential, i.admin.RevokeCredential(context.Background(), i.realm, i.credential, inviteRevokeReason)))
	}
	if i.grant != "" {
		notes = append(notes, revokeNote("grant", i.grant, i.admin.RevokeGrant(context.Background(), i.realm, i.grant, inviteRevokeReason)))
	}
	return fmt.Errorf("%w (principal %s, grant %s, credential %s; %s)", err, idOrNone(i.principal, false), idOrNone(i.grant, i.grantUnknown), idOrNone(i.credential, i.credentialUnknown), strings.Join(notes, "; "))
}

func revokeNote(kind, id string, err error) string {
	if err != nil {
		return fmt.Sprintf("%s %s not revoked: %v", kind, id, err)
	}
	return fmt.Sprintf("%s %s revoked", kind, id)
}

func idOrNone(id string, unknown bool) string {
	switch {
	case id != "":
		return id
	case unknown:
		return "unknown (check Cairn)"
	default:
		return "none"
	}
}

func runInvite(cmd *cobra.Command, o inviteOptions, now time.Time) error {
	ctx := context.Background()
	errOut := cmd.ErrOrStderr()

	cfg, err := garden.LoadConfig(o.gatewayConfig)
	if err != nil {
		return fmt.Errorf("gateway configuration: %w", err)
	}
	switch {
	case cfg.PublicEndpoint == "":
		return errors.New("the gateway configuration needs public_endpoint")
	case cfg.TLSCertFile == "":
		return errors.New("the gateway configuration needs tls_cert_file")
	case cfg.InvitesDir == "":
		return errors.New("the gateway configuration needs invites_dir")
	}
	label, err := invite.CheckName(o.name)
	if err != nil {
		return err
	}
	mappedPrincipal := ""
	for id, mapped := range cfg.Principals {
		if mapped == o.name {
			mappedPrincipal = id
		}
	}
	if o.reissue && mappedPrincipal == "" {
		return fmt.Errorf("%s is not mapped in %s, so there is no identity to reissue; run garden invite without --reissue to add it", o.name, o.gatewayConfig)
	}
	if !o.reissue && mappedPrincipal != "" {
		return fmt.Errorf("%s is already mapped in %s; use --reissue to issue a new credential for it and revoke the earlier ones", o.name, o.gatewayConfig)
	}
	if !o.reissue && len(cfg.Principals) >= 1000 {
		return errors.New("the gateway configuration already has 1000 principals")
	}
	if !invite.ValidHarness(o.harness) {
		return fmt.Errorf("--harness must be empty or one of %s", strings.Join(invite.Harnesses, ", "))
	}
	if o.ttl <= 0 || o.ttl > invite.MaxTTL {
		return fmt.Errorf("--ttl must be more than zero and at most %s", invite.MaxTTL)
	}
	expires, err := parseExpiry(o.expires, now)
	if err != nil {
		return err
	}
	if !expires.After(now.Add(o.ttl)) {
		return errors.New("--expires must be later than the invite expiry (now plus --ttl), or a late join gets a dead credential")
	}
	if o.reloadUnit != "" && !unitPattern.MatchString(o.reloadUnit) {
		return errors.New("--reload-unit is not a valid unit name")
	}
	if err := invite.CheckDir(cfg.InvitesDir); err != nil {
		return err
	}
	pruned, err := invite.Prune(cfg.InvitesDir, now)
	if err != nil {
		return err
	}
	if pruned > 0 {
		fmt.Fprintf(errOut, "removed %d expired invite(s)\n", pruned)
	}
	// The issuance ledger is read and validated under the directory lock before any admin call,
	// for every invite: a malformed or untrusted ledger must not be discovered after credentials exist.
	ledgerPath := invite.LedgerPath(o.gatewayConfig)
	// One invite operation at a time, from this ledger read to the last revocation: two
	// overlapping re-issues would otherwise both revoke the same earlier credential and leave
	// both replacements live (Val's review, 8 November 2026). It refuses rather than waits,
	// because the running invite may be waiting for someone to type a passphrase.
	releaseOperation, err := lockOperation(filepath.Dir(o.gatewayConfig))
	if err != nil {
		return err
	}
	defer releaseOperation()
	unlockLedger, err := lockDirectory(filepath.Dir(o.gatewayConfig))
	if err != nil {
		return err
	}
	ledgerEntries, err := invite.ReadLedger(ledgerPath)
	unlockLedger()
	if err != nil {
		return fmt.Errorf("refusing to issue: %w", err)
	}
	var earlier []invite.LedgerEntry
	if o.reissue {
		earlier = invite.PendingFor(ledgerEntries, mappedPrincipal)
	}

	endpoint, err := url.Parse(cfg.PublicEndpoint)
	if err != nil {
		return errors.New("public_endpoint is not a URL")
	}
	chain, err := readChain(cfg.TLSCertFile)
	if err != nil {
		return err
	}
	ca, err := invite.PinCA(chain, endpoint.Hostname())
	if err != nil {
		return err
	}
	secret, err := invite.NewSecret()
	if err != nil {
		return err
	}
	// Encoding here checks public_endpoint against the invite format before any admin call.
	codeText, err := invite.Code{Endpoint: cfg.PublicEndpoint, CASHA256: invite.CADigest(ca), Secret: secret, Name: o.name, Harness: o.harness}.Encode()
	if err != nil {
		return err
	}
	token, err := invite.LoadAdminToken(o.adminTokenFile)
	if err != nil {
		return err
	}
	base, err := invite.CairnBaseURL(o.cairnURL, cfg.Auth.Endpoint)
	if err != nil {
		return err
	}
	admin, err := invite.NewAdminClient(base, token)
	if err != nil {
		return err
	}
	gateway, err := gardenauth.New(cfg.Auth)
	if err != nil {
		return err
	}

	// A cli identity is sealed under a passphrase here, at invite time, so that the plain token
	// never leaves this host. The passphrase is asked before any admin call.
	var passphrase []byte
	if o.harness == "cli" {
		passphrase, err = readPassphrase(fmt.Sprintf("passphrase for %s: ", o.name), true)
		if err != nil {
			return fmt.Errorf("passphrase for the cli identity: %w (an invite for a cli identity needs an interactive terminal; under ssh, use ssh -t)", err)
		}
		defer wipeBytes(passphrase)
	}

	realm := cfg.Auth.Scope.Realm
	classification := cfg.Auth.Classification
	inviteExpires := now.Add(o.ttl).UTC().Truncate(time.Second)
	issued := &issuance{admin: admin, realm: realm}

	principalID := mappedPrincipal
	if !o.reissue {
		principalID, err = admin.CreatePrincipal(ctx, realm, label)
		if err != nil {
			return fmt.Errorf("create principal: %w", err)
		}
	}
	issued.principal = principalID

	grantID, err := admin.CreateGrant(ctx, invite.GrantSpec{
		PrincipalID:    principalID,
		Realm:          realm,
		Segments:       cfg.Auth.Scope.Segments,
		Classification: classification,
		ExpiresAt:      expires,
	})
	if err != nil {
		issued.grantUnknown = true
		return issued.fail(fmt.Errorf("create grant: %w", err))
	}
	issued.grant = grantID

	cred, err := admin.IssueCredential(ctx, realm, principalID, expires)
	issued.credential = cred.ID
	if err != nil {
		issued.credentialUnknown = cred.ID == ""
		return issued.fail(fmt.Errorf("issue credential: %w", err))
	}

	identity, err := gateway.Authenticate(ctx, cred.Plaintext)
	if err == nil && (identity.PrincipalID != principalID || !identity.CanRead || !identity.CanWrite) {
		err = errors.New("the new credential does not hold exactly retrieve and ingest on the gateway scope")
	}
	if err != nil {
		return issued.fail(fmt.Errorf("check credential: %w", err))
	}

	var record invite.Record
	if o.harness == "cli" {
		record, err = sealCLIInvite(secret, cred.Plaintext, o.name, principalID, inviteExpires, passphrase)
	} else {
		record, err = invite.Seal(secret, cred.Plaintext, o.name, o.harness, principalID, inviteExpires)
	}
	if err != nil {
		return issued.fail(fmt.Errorf("seal invite: %w", err))
	}
	record.GrantID = grantID
	record.CredentialID = cred.ID
	issued.recordPath = filepath.Join(cfg.InvitesDir, record.ID+".json")
	if err := invite.Write(cfg.InvitesDir, record); err != nil {
		return issued.fail(fmt.Errorf("write invite: %w", err))
	}

	// The configuration update and the ledger line share one directory lock, so no other invite
	// can interleave between them. The lock is not held across any admin call.
	unlock, err := lockDirectory(filepath.Dir(o.gatewayConfig))
	if err != nil {
		return issued.fail(fmt.Errorf("lock gateway configuration directory: %w", err))
	}
	if !o.reissue {
		if err = replaceGatewayPrincipal(o.gatewayConfig, principalID, o.name); err != nil {
			unlock()
			return issued.fail(fmt.Errorf("update gateway configuration: %w", err))
		}
	}
	if err = invite.AppendLedger(ledgerPath, invite.NewLedgerEntry(o.name, principalID, grantID, cred.ID, now, expires)); err != nil {
		unlock()
		return issued.fail(fmt.Errorf("record the issuance in the ledger (the gateway configuration may already map the principal; check it): %w", err))
	}
	unlock()

	fmt.Fprintf(errOut, "name: %s\n", o.name)
	fmt.Fprintf(errOut, "principal: %s\n", principalID)
	harness := o.harness
	if harness == "" {
		harness = "none"
	}
	fmt.Fprintf(errOut, "harness default: %s\n", harness)
	fmt.Fprintf(errOut, "credential expires: %s\n", expires.Format(time.RFC3339))
	fmt.Fprintf(errOut, "invite expires: %s\n", inviteExpires.Format(time.RFC3339))
	fmt.Fprintf(errOut, "single use: redeem the code once on the client with garden join\n")
	if o.harness == "cli" {
		fmt.Fprintln(errOut, "cli identity: the code yields only the passphrase-sealed key; the passphrase you just set is needed to use it, and the plain token is never shown")
	}

	var reissueErr error
	if o.reissue {
		fmt.Fprintln(errOut, "reissued: the gateway configuration and the gateway are unchanged, so no reload is needed")
		fmt.Fprintf(errOut, "on the client, remove identities/%s.token or identities/%s.sealed before garden join, or join refuses the new token\n", o.name, o.name)
		reissueErr = revokeEarlier(ctx, admin, realm, filepath.Dir(o.gatewayConfig), ledgerPath, earlier, now, errOut)
	} else if o.reloadUnit == "" {
		fmt.Fprintln(errOut, "gateway not reloaded: send SIGHUP to the gateway process before the code is redeemed")
	} else {
		rctx, cancel := context.WithTimeout(ctx, reloadTimeout)
		_, rerr := runCommand(rctx, "systemctl", "reload", o.reloadUnit)
		cancel()
		if rerr != nil {
			fmt.Fprintf(errOut, "warning: systemctl reload %s failed (%v)\n", o.reloadUnit, rerr)
			fmt.Fprintf(errOut, "the invite is still valid, but the gateway answers not_ready until it is reloaded: systemctl reload %s\n", o.reloadUnit)
		}
	}
	fmt.Fprintln(cmd.OutOrStdout(), codeText)
	return reissueErr
}

// ledgerMark records one revoked half in the ledger. It is a variable so that tests can make the
// record fail after a successful revocation.
var ledgerMark = invite.MarkLedger

// markLedgerHalf takes the directory lock and marks one half of an entry as revoked.
func markLedgerHalf(configDir, ledgerPath, half, id string, at time.Time) error {
	unlock, err := lockDirectory(configDir)
	if err != nil {
		return err
	}
	defer unlock()
	ok, err := ledgerMark(ledgerPath, half, id, at)
	if err != nil {
		return err
	}
	if !ok {
		return errors.New("no pending entry matches it in the ledger")
	}
	return nil
}

// revokeEarlier revokes the credentials and grants that the ledger records for the identity before
// this reissue. Each half is revoked on its own, and marked in the ledger straight after its own
// successful revocation, so that a failed half is retried by the next reissue and a finished half
// is never sent again. A rejected revocation is a failure: Cairn answers an already-revoked
// credential or grant with the same generic error. The new invite is valid whatever happens here.
func revokeEarlier(ctx context.Context, admin *invite.AdminClient, realm, configDir, ledgerPath string, earlier []invite.LedgerEntry, now time.Time, errOut io.Writer) error {
	if len(earlier) == 0 {
		fmt.Fprintln(errOut, "no earlier credentials are recorded in the issuance ledger for this identity; any it has (identities issued before the ledger existed) stay valid until they are revoked by hand")
		return nil
	}
	fmt.Fprintln(errOut, "sessions using the old token stop now, as each earlier credential is revoked:")
	var live, unrecorded []string
	for _, e := range earlier {
		if e.CredentialPending() {
			if err := admin.RevokeCredential(ctx, realm, e.CredentialID, reissueRevokeReason); err != nil {
				live = append(live, "credential "+e.CredentialID)
				fmt.Fprintf(errOut, "  credential %s not revoked: %v\n", e.CredentialID, err)
			} else if err := markLedgerHalf(configDir, ledgerPath, invite.LedgerCredential, e.CredentialID, now); err != nil {
				unrecorded = append(unrecorded, "credential "+e.CredentialID)
				fmt.Fprintf(errOut, "  credential %s revoked in Cairn but not recorded: %v\n", e.CredentialID, err)
			} else {
				fmt.Fprintf(errOut, "  revoked credential %s (issued %s)\n", e.CredentialID, e.IssuedAt)
			}
		}
		if e.GrantPending() {
			if err := admin.RevokeGrant(ctx, realm, e.GrantID, reissueRevokeReason); err != nil {
				live = append(live, "grant "+e.GrantID)
				fmt.Fprintf(errOut, "  grant %s not revoked: %v\n", e.GrantID, err)
			} else if err := markLedgerHalf(configDir, ledgerPath, invite.LedgerGrant, e.GrantID, now); err != nil {
				unrecorded = append(unrecorded, "grant "+e.GrantID)
				fmt.Fprintf(errOut, "  grant %s revoked in Cairn but not recorded: %v\n", e.GrantID, err)
			} else {
				fmt.Fprintf(errOut, "  revoked grant %s (issued %s)\n", e.GrantID, e.IssuedAt)
			}
		}
	}
	var problems []string
	if len(unrecorded) > 0 {
		problems = append(problems, fmt.Sprintf("revoked in Cairn but unrecorded: %s; mark each by hand in %s: set credential_revoked_at (for a credential) or grant_revoked_at (for a grant) on its entry to the revocation time", strings.Join(unrecorded, ", "), ledgerPath))
	}
	if len(live) > 0 {
		problems = append(problems, fmt.Sprintf("these earlier IDs are still live; a repeated garden invite --reissue retries them, or revoke them by hand: %s", strings.Join(live, ", ")))
	}
	if len(problems) > 0 {
		return fmt.Errorf("the new invite is valid, but %s", strings.Join(problems, "; "))
	}
	return nil
}

// parseExpiry reads --expires: a duration from now, or an RFC3339 time. Empty means one year.
func parseExpiry(raw string, now time.Time) (time.Time, error) {
	if raw == "" {
		return now.AddDate(1, 0, 0).UTC().Truncate(time.Second), nil
	}
	var t time.Time
	if d, err := time.ParseDuration(raw); err == nil {
		t = now.Add(d)
	} else if parsed, err := time.Parse(time.RFC3339, raw); err == nil {
		t = parsed
	} else {
		return time.Time{}, errors.New("--expires must be a duration such as 8760h, or an RFC3339 time")
	}
	t = t.UTC().Truncate(time.Second)
	if !t.After(now) {
		return time.Time{}, errors.New("--expires must be in the future")
	}
	return t, nil
}

func readChain(path string) ([]byte, error) {
	f, err := os.Open(path)
	if err != nil {
		return nil, fmt.Errorf("TLS chain file: %w", err)
	}
	defer f.Close()
	data, err := io.ReadAll(io.LimitReader(f, maxChainBytes+1))
	if err != nil {
		return nil, fmt.Errorf("TLS chain file: %w", err)
	}
	if len(data) > maxChainBytes {
		return nil, errors.New("TLS chain file is too large")
	}
	return data, nil
}

// errConfigCommitted reports that the gateway configuration was renamed into place but could not
// be confirmed afterwards. The principal may already be mapped, so the caller treats it as a
// failure to roll back, and the operator is told to check the file.
var errConfigCommitted = errors.New("the gateway configuration was replaced but could not be confirmed")

// addGatewayPrincipal maps principalID to name in the gateway configuration at path, under the
// config directory lock, so that two invites cannot each rewrite the file from the same original and
// lose the other's principal.
func addGatewayPrincipal(path, principalID, name string) error {
	unlock, err := lockDirectory(filepath.Dir(path))
	if err != nil {
		return err
	}
	defer unlock()
	return replaceGatewayPrincipal(path, principalID, name)
}

// replaceGatewayPrincipal is addGatewayPrincipal for a caller that already holds the directory
// lock. The new file is written beside the original with the original owner, group and mode,
// validated, then renamed over it, so the gateway never sees a half-written or invalid file.
func replaceGatewayPrincipal(path, principalID, name string) error {
	info, err := os.Lstat(path)
	if err != nil {
		return err
	}
	stat, ok := info.Sys().(*syscall.Stat_t)
	if !info.Mode().IsRegular() || !ok {
		return errors.New("the gateway configuration must be a regular file")
	}
	cfg, err := garden.LoadConfig(path)
	if err != nil {
		return err
	}
	if _, exists := cfg.Principals[principalID]; exists {
		return errors.New("the principal is already in the gateway configuration")
	}
	for _, mapped := range cfg.Principals {
		if mapped == name {
			return fmt.Errorf("%s is already mapped in the gateway configuration", name)
		}
	}
	cfg.Principals[principalID] = name
	data, err := json.MarshalIndent(cfg, "", "  ")
	if err != nil {
		return err
	}
	data = append(data, '\n')

	dir := filepath.Dir(path)
	tmp, err := os.CreateTemp(dir, ".garden-config-*.tmp")
	if err != nil {
		return err
	}
	tmpPath := tmp.Name()
	renamed := false
	defer func() {
		if !renamed {
			_ = os.Remove(tmpPath)
		}
	}()
	// Owner before mode: chown clears setuid and setgid bits.
	err = tmp.Chown(int(stat.Uid), int(stat.Gid))
	if err == nil {
		err = tmp.Chmod(info.Mode() & (fs.ModePerm | fs.ModeSetuid | fs.ModeSetgid | fs.ModeSticky))
	}
	if err == nil {
		_, err = tmp.Write(data)
	}
	if err == nil {
		err = tmp.Sync()
	}
	if closeErr := tmp.Close(); err == nil {
		err = closeErr
	}
	if err != nil {
		return err
	}
	if _, err := garden.LoadConfig(tmpPath); err != nil {
		return fmt.Errorf("the updated gateway configuration is invalid: %w", err)
	}
	if err := os.Rename(tmpPath, path); err != nil {
		return err
	}
	renamed = true
	if err := syncDirectory(dir); err != nil {
		return fmt.Errorf("%w: %v", errConfigCommitted, err)
	}
	confirmed, err := garden.LoadConfig(path)
	if err != nil {
		return fmt.Errorf("%w: %v", errConfigCommitted, err)
	}
	if confirmed.Principals[principalID] != name {
		return fmt.Errorf("%w: the principal is not mapped to %s", errConfigCommitted, name)
	}
	return nil
}

func syncDirectory(dir string) error {
	d, err := os.Open(dir)
	if err != nil {
		return err
	}
	defer d.Close()
	return d.Sync()
}

// lockDirectory takes an exclusive flock on dir, waiting up to 30 seconds.
func lockDirectory(dir string) (func(), error) {
	f, err := os.Open(dir)
	if err != nil {
		return nil, err
	}
	deadline := time.Now().Add(30 * time.Second)
	for {
		err = syscall.Flock(int(f.Fd()), syscall.LOCK_EX|syscall.LOCK_NB)
		if err == nil {
			return func() { _ = syscall.Flock(int(f.Fd()), syscall.LOCK_UN); _ = f.Close() }, nil
		}
		if !errors.Is(err, syscall.EWOULDBLOCK) || time.Now().After(deadline) {
			_ = f.Close()
			return nil, errors.New("another garden invite is updating the gateway configuration; try again")
		}
		time.Sleep(50 * time.Millisecond)
	}
}

// sealCLIInvite seals the issued token under the passphrase, checks that the sealed file opens to
// that same token, and returns the invite record, which carries only the sealed identity.
func sealCLIInvite(secret []byte, token, name, principalID string, expires time.Time, passphrase []byte) (invite.Record, error) {
	sealed, err := identityseal.Seal(name, token, passphrase)
	if err != nil {
		return invite.Record{}, err
	}
	opened, err := identityseal.Open(name, sealed, passphrase)
	if err != nil || opened != token {
		return invite.Record{}, errors.New("the sealed identity does not open to the issued token")
	}
	return invite.SealIdentity(secret, sealed, name, principalID, expires)
}

// wipeBytes overwrites a secret held in a byte slice.
func wipeBytes(b []byte) {
	for i := range b {
		b[i] = 0
	}
}

// operationLockFile serialises whole invite runs; it lives beside the gateway configuration and,
// like the ledger, belongs to root.
const operationLockFile = ".garden-invite.lock"

// lockOperation takes the invite operation lock without waiting.
func lockOperation(dir string) (func(), error) {
	f, err := os.OpenFile(filepath.Join(dir, operationLockFile), os.O_RDWR|os.O_CREATE|syscall.O_NOFOLLOW, 0600)
	if err != nil {
		return nil, fmt.Errorf("cannot open the invite lock in %s: %w", dir, err)
	}
	if err = syscall.Flock(int(f.Fd()), syscall.LOCK_EX|syscall.LOCK_NB); err != nil {
		_ = f.Close()
		if errors.Is(err, syscall.EWOULDBLOCK) {
			return nil, errors.New("another garden invite is running on this gateway; let it finish, then try again")
		}
		return nil, err
	}
	return func() { _ = syscall.Flock(int(f.Fd()), syscall.LOCK_UN); _ = f.Close() }, nil
}
