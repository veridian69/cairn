package credentials

import (
	"bytes"
	"io"
	"os"
	"sync"
	"unicode/utf8"

	"crypto/subtle"

	"github.com/veridian69/cairn/cairn-mcp/internal/config"
)

// MaxSecretBytes caps every secret file read; callers outside this package share
// it rather than restating the number.
const MaxSecretBytes = 4096

type CredentialError struct {
	msg string
}

func (error *CredentialError) Error() string {
	return error.msg
}

type CredentialSnapshot struct {
	LocalToken     []byte
	CFClientID     string
	CFClientSecret string
}

type AccessCredentials struct {
	ClientID     string
	ClientSecret string
}

type CredentialStore struct {
	config      config.RelayConfig
	expectedUID int
	mutex       sync.RWMutex
	snapshot    CredentialSnapshot
}

type Store = CredentialStore

func NewCredentialStore(cfg config.RelayConfig) (*CredentialStore, error) {
	return NewRelayStore(cfg.CFClientIDPath, cfg.CFClientSecretPath, cfg.LocalTokenPath)
}

func NewAccessStore(clientIDPath, clientSecretPath string) (*Store, error) {
	return newStore(config.RelayConfig{
		CFClientIDPath:     clientIDPath,
		CFClientSecretPath: clientSecretPath,
	})
}

func NewRelayStore(clientIDPath, clientSecretPath, localTokenPath string) (*Store, error) {
	if localTokenPath == "" {
		return nil, &CredentialError{msg: "local token path is required"}
	}
	return newStore(config.RelayConfig{
		CFClientIDPath:     clientIDPath,
		CFClientSecretPath: clientSecretPath,
		LocalTokenPath:     localTokenPath,
	})
}

func newStore(cfg config.RelayConfig) (*CredentialStore, error) {
	store := &CredentialStore{
		config:      cfg,
		expectedUID: os.Geteuid(),
	}
	snapshot, err := loadSnapshot(cfg, os.Geteuid())
	if err != nil {
		return nil, err
	}
	store.snapshot = snapshot
	return store, nil
}

func (store *CredentialStore) Snapshot() CredentialSnapshot {
	store.mutex.RLock()
	defer store.mutex.RUnlock()
	return cloneSnapshot(store.snapshot)
}

func (store *CredentialStore) AccessSnapshot() AccessCredentials {
	store.mutex.RLock()
	defer store.mutex.RUnlock()
	return AccessCredentials{
		ClientID:     store.snapshot.CFClientID,
		ClientSecret: store.snapshot.CFClientSecret,
	}
}

func (store *CredentialStore) Reload() error {
	snapshot, err := loadSnapshot(store.config, store.expectedUID)
	if err != nil {
		return err
	}

	store.mutex.Lock()
	store.snapshot = snapshot
	store.mutex.Unlock()
	return nil
}

func cloneSnapshot(snapshot CredentialSnapshot) CredentialSnapshot {
	cloned := make([]byte, len(snapshot.LocalToken))
	copy(cloned, snapshot.LocalToken)
	return CredentialSnapshot{
		LocalToken:     cloned,
		CFClientID:     snapshot.CFClientID,
		CFClientSecret: snapshot.CFClientSecret,
	}
}

func TokenMatches(expected []byte, presented []byte) bool {
	return subtle.ConstantTimeCompare(expected, presented) == 1
}

func loadSnapshot(cfg config.RelayConfig, expectedUID int) (CredentialSnapshot, error) {
	var localToken []byte
	var err error
	if cfg.LocalTokenPath != "" {
		localToken, err = ReadSecretFile(cfg.LocalTokenPath, "local token", expectedUID, MaxSecretBytes)
		if err != nil {
			return CredentialSnapshot{}, err
		}
	}
	clientID, err := ReadSecretFile(cfg.CFClientIDPath, "Cloudflare client ID", expectedUID, MaxSecretBytes)
	if err != nil {
		return CredentialSnapshot{}, err
	}
	clientSecret, err := ReadSecretFile(cfg.CFClientSecretPath, "Cloudflare client secret", expectedUID, MaxSecretBytes)
	if err != nil {
		return CredentialSnapshot{}, err
	}
	if !utf8.ValidString(string(clientID)) {
		return CredentialSnapshot{}, &CredentialError{msg: "Cloudflare credential must be UTF-8"}
	}
	if !utf8.ValidString(string(clientSecret)) {
		return CredentialSnapshot{}, &CredentialError{msg: "Cloudflare credential must be UTF-8"}
	}
	return CredentialSnapshot{
		LocalToken:     localToken,
		CFClientID:     string(clientID),
		CFClientSecret: string(clientSecret),
	}, nil
}

func ReadSecretFile(path string, label string, expectedUID int, maxBytes int) ([]byte, error) {
	file, err := openSecretFile(path, label, expectedUID)
	if err != nil {
		return nil, err
	}
	defer file.Close()
	stat, err := file.Stat()
	if err != nil {
		return nil, &CredentialError{msg: label + " is unavailable"}
	}
	if stat.Size() > int64(maxBytes) {
		return nil, &CredentialError{msg: label + " is too large"}
	}

	value, err := io.ReadAll(io.LimitReader(file, int64(maxBytes)+1))
	if err != nil {
		return nil, &CredentialError{msg: label + " is unavailable"}
	}
	if len(value) > maxBytes {
		return nil, &CredentialError{msg: label + " is too large"}
	}
	if len(value) == 0 {
		return nil, &CredentialError{msg: label + " is empty"}
	}

	value = trimLineEnding(value)
	if len(value) == 0 {
		return nil, &CredentialError{msg: label + " is empty"}
	}

	return value, nil
}

func trimLineEnding(value []byte) []byte {
	if bytes.HasSuffix(value, []byte("\r\n")) {
		return value[:len(value)-2]
	}
	if bytes.HasSuffix(value, []byte("\n")) {
		return value[:len(value)-1]
	}
	return value
}
