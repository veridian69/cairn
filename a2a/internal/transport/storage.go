package transport

import (
	"context"
	"encoding/json"
	"errors"
	"path/filepath"
)

const storageSubject = "a2a.runtime.storage"

type storageIdentity struct {
	ServerID string `json:"server_id"`
	DataDir  string `json:"data_dir"`
}

// StorageDirectory asks the connected daemon for its actual canonical store.
// Callers must not infer this identity from their own configuration or URL file.
func (s *Stream) StorageDirectory(ctx context.Context) (string, error) {
	msg, err := s.nc.RequestWithContext(ctx, storageSubject, nil)
	if err != nil {
		return "", errors.New("daemon storage identity unavailable; restart the daemon with this version")
	}
	if len(msg.Data) > 16384 {
		return "", errors.New("invalid daemon storage identity")
	}
	var identity storageIdentity
	if json.Unmarshal(msg.Data, &identity) != nil || identity.ServerID != s.nc.ConnectedServerId() || !filepath.IsAbs(identity.DataDir) {
		return "", errors.New("invalid daemon storage identity")
	}
	return identity.DataDir, nil
}
