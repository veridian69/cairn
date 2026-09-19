package snapshot

import "time"

const manifestFormatVersion = 1

// Snapshot describes a saved runtime state.
type Snapshot struct {
	// ID is the filesystem-safe snapshot identifier.
	ID string
	// CreatedAt is the snapshot publication time.
	CreatedAt time.Time
	// Size is the number of bytes covered by the manifest.
	Size int64
}

// RestoreResult identifies both the selected and automatic rollback snapshots.
type RestoreResult struct {
	// RestoredID is the selected snapshot made live.
	RestoredID string
	// RollbackID is the snapshot created from the replaced runtime.
	RollbackID string
}

type manifest struct {
	FormatVersion       int        `json:"format_version"`
	ID                  string     `json:"id"`
	CreatedAt           time.Time  `json:"created_at"`
	BinaryVersion       string     `json:"binary_version"`
	CanonicalDataDir    string     `json:"canonical_data_dir"`
	ConfigSHA256        string     `json:"config_sha256"`
	StateSchemaVersion  int        `json:"state_schema_version"`
	MemorySchemaVersion int        `json:"memory_schema_version"`
	TotalBytes          int64      `json:"total_bytes"`
	Files               []fileInfo `json:"files"`
}

type fileInfo struct {
	Path   string `json:"path"`
	Size   int64  `json:"size"`
	SHA256 string `json:"sha256"`
	Mode   uint32 `json:"mode"`
}
