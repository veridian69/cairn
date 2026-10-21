package snapshot

import (
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"time"

	"github.com/veridian69/cairn/garden/internal/config"
)

// layout separates the host's JSON configuration and lease from daemon storage.
type layout struct {
	configPath string
	configFile string
	leaseDir   string
	backupRoot string
}

// NewManagerWithBackupDir constructs a daemon manager with an optional backup root.
func NewManagerWithBackupDir(configDir, backupDir, binaryVersion string) *Manager {
	if backupDir == "" {
		backupDir = filepath.Join(configDir, "backups")
	}
	return &Manager{
		configDir: configDir, binaryVersion: binaryVersion,
		layout: layout{filepath.Join(configDir, "config.yaml"), "config.yaml", configDir, backupDir},
		now:    time.Now, availableBytes: filesystemAvailableBytes,
	}
}

// NewHostManager constructs a manager for an installer host configuration.
// An empty backupDir selects backups beside the host configuration file.
func NewHostManager(hostConfig, backupDir, binaryVersion string) (*Manager, error) {
	path, err := filepath.Abs(hostConfig)
	if err != nil {
		return nil, err
	}
	_, leaseDir, err := readHostPaths(path)
	if err != nil {
		return nil, err
	}
	manager := NewManagerWithBackupDir(filepath.Dir(path), backupDir, binaryVersion)
	manager.layout.configPath = path
	manager.layout.configFile = "host.json"
	manager.layout.leaseDir = leaseDir
	return manager, nil
}

func readHostPaths(path string) (string, string, error) {
	raw, err := os.ReadFile(path)
	if err != nil {
		return "", "", fmt.Errorf("reading host config: %w", err)
	}
	return hostPaths(raw)
}

func hostPaths(raw []byte) (string, string, error) {
	// Snapshot needs only storage paths; authentication and listener settings
	// remain opaque, and the original bytes are always archived unchanged.
	// host.json cannot tell Kubernetes from Docker: both carry container-local
	// paths. The installer refuses Kubernetes before it runs a snapshot.
	var doc struct {
		Gateway struct {
			DataDir       string `json:"data_dir"`
			DaemonURLFile string `json:"daemon_url_file"`
		} `json:"gateway"`
	}
	if err := json.Unmarshal(raw, &doc); err != nil {
		return "", "", fmt.Errorf("parsing host config: %w", err)
	}
	if !filepath.IsAbs(doc.Gateway.DataDir) || !filepath.IsAbs(doc.Gateway.DaemonURLFile) {
		return "", "", fmt.Errorf("Garden data_dir and daemon_url_file must be absolute")
	}
	return filepath.Clean(doc.Gateway.DataDir), filepath.Dir(doc.Gateway.DaemonURLFile), nil
}

func (m *Manager) readConfig() ([]byte, string, error) {
	raw, err := os.ReadFile(m.layout.configPath)
	if err != nil {
		return nil, "", fmt.Errorf("reading config: %w", err)
	}
	if m.layout.configFile == "host.json" {
		dataDir, leaseDir, err := hostPaths(raw)
		if err != nil {
			return nil, "", err
		}
		if leaseDir != m.layout.leaseDir {
			return nil, "", fmt.Errorf("host lease directory changed; retry with the current host config")
		}
		return raw, dataDir, nil
	}
	cfg, err := config.Parse(raw)
	if err != nil {
		return nil, "", err
	}
	return raw, cfg.Stream.DataDir, nil
}

func (m *Manager) manifestConfigFile() string {
	if m.layout.configFile == "host.json" {
		return "host.json"
	}
	return ""
}

func (doc manifest) configFile() (string, error) {
	switch doc.ConfigFile {
	case "":
		return "config.yaml", nil
	case "host.json":
		return "host.json", nil
	default:
		return "", fmt.Errorf("unsupported snapshot config_file %q", doc.ConfigFile)
	}
}
