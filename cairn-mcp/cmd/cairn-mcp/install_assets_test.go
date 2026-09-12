package main

import (
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"

	"github.com/veridian69/cairn/cairn-mcp/internal/config"
)

func TestInstallAssetsAreSafeAndUseOneBinaryPath(t *testing.T) {
	root := filepath.Join("..", "..")
	unitPath := filepath.Join(root, "deploy", "cairn-mcp.service")
	scriptPath := filepath.Join(root, "scripts", "install-user")
	unitBytes, err := os.ReadFile(unitPath)
	if err != nil {
		t.Fatal(err)
	}
	scriptBytes, err := os.ReadFile(scriptPath)
	if err != nil {
		t.Fatal(err)
	}
	unit := string(unitBytes)
	script := string(scriptBytes)

	if !strings.Contains(unit, "RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX AF_NETLINK") {
		t.Fatal("unit does not allow resolver AF_NETLINK sockets")
	}
	if !strings.Contains(unit, "ExecStart=%h/.local/share/bin/cairn-mcp serve") {
		t.Fatal("unit does not use the documented binary path")
	}
	if !strings.Contains(script, `binary_path="$HOME/.local/share/bin/cairn-mcp"`) {
		t.Fatal("installer and unit binary paths disagree")
	}
	for _, forbidden := range []string{"CF-Access-Client-Secret=", "CF-Access-Client-Id=", "Environment="} {
		if strings.Contains(unit, forbidden) || strings.Contains(script, forbidden) {
			t.Fatalf("installation artefact contains forbidden %q", forbidden)
		}
	}
	command := exec.Command("bash", "-n", scriptPath)
	if output, err := command.CombinedOutput(); err != nil {
		t.Fatalf("bash -n: %v: %s", err, output)
	}
}

func TestInstallAssetsAndBinaryUseOneConfigDirectory(t *testing.T) {
	root := filepath.Join("..", "..")
	scriptPath := filepath.Join(root, "scripts", "install-user")
	unitPath := filepath.Join(root, "deploy", "cairn-mcp.service")

	home := t.TempDir()
	// A deliberately divergent XDG_CONFIG_HOME: the installer runs in the
	// operator's shell, but the systemd user manager is seeded at login and
	// carries no XDG_CONFIG_HOME, so anything the installer resolves from it
	// is invisible to the running unit.
	elsewhere := t.TempDir()
	installerConfigDir, installerUnitDir := installerPaths(t, scriptPath, home, elsewhere)

	t.Setenv("HOME", home)
	t.Setenv("XDG_CONFIG_HOME", elsewhere)
	binaryConfigDir, err := config.DefaultConfigDir()
	if err != nil {
		t.Fatalf("DefaultConfigDir() unexpected error: %v", err)
	}

	if installerConfigDir != binaryConfigDir {
		t.Fatalf("installer writes credentials to %q but the binary reads %q", installerConfigDir, binaryConfigDir)
	}
	if want := filepath.Join(home, ".config", "cairn"); installerConfigDir != want {
		t.Fatalf("config directory %q, want %q", installerConfigDir, want)
	}
	if want := filepath.Join(home, ".config", "systemd", "user"); installerUnitDir != want {
		t.Fatalf("unit directory %q, want %q — the user manager only searches there", installerUnitDir, want)
	}

	unitBytes, err := os.ReadFile(unitPath)
	if err != nil {
		t.Fatal(err)
	}
	for _, override := range []string{"--local-token-path", "--cf-client-id-path", "--cf-client-secret-path"} {
		if strings.Contains(string(unitBytes), override) {
			t.Fatalf("unit overrides %q instead of taking the one binary default", override)
		}
	}
}

// installerPaths evaluates the installer's own path assignments under a
// controlled environment, so the test pins what the script resolves rather
// than how it is spelled.
func installerPaths(t *testing.T, scriptPath, home, xdgConfigHome string) (string, string) {
	t.Helper()
	scriptBytes, err := os.ReadFile(scriptPath)
	if err != nil {
		t.Fatal(err)
	}
	var assignments []string
	for _, line := range strings.Split(string(scriptBytes), "\n") {
		for _, name := range []string{"config_dir=", "unit_dir="} {
			if strings.HasPrefix(line, name) {
				assignments = append(assignments, line)
			}
		}
	}
	program := "set -u\n" + strings.Join(assignments, "\n") + "\nprintf '%s\\n%s' \"$config_dir\" \"$unit_dir\"\n"
	command := exec.Command("bash", "-c", program)
	command.Env = []string{"HOME=" + home, "XDG_CONFIG_HOME=" + xdgConfigHome, "PATH=" + os.Getenv("PATH")}
	output, err := command.Output()
	if err != nil {
		t.Fatalf("evaluating installer paths: %v", err)
	}
	resolved := strings.Split(string(output), "\n")
	if len(resolved) != 2 {
		t.Fatalf("installer paths = %q, want a config directory and a unit directory", output)
	}
	return resolved[0], resolved[1]
}
