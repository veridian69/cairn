package maintenance

import (
	"bufio"
	"errors"
	"os"
	"os/exec"
	"testing"
)

func TestExclusiveLeaseRefusesWhileSharedLeaseIsHeld(t *testing.T) {
	if os.Getenv("A2A_MAINTENANCE_LOCK_HELPER") == "1" {
		lease, err := Acquire(os.Getenv("A2A_MAINTENANCE_CONFIG_DIR"), Shared)
		if err != nil {
			os.Exit(2)
		}
		defer lease.Close()
		_, _ = os.Stdout.WriteString("locked\n")
		_, _ = bufio.NewReader(os.Stdin).ReadByte()
		return
	}

	configDir := t.TempDir()
	child := exec.Command(os.Args[0], "-test.run=TestExclusiveLeaseRefusesWhileSharedLeaseIsHeld")
	child.Env = append(os.Environ(),
		"A2A_MAINTENANCE_LOCK_HELPER=1",
		"A2A_MAINTENANCE_CONFIG_DIR="+configDir,
	)
	stdout, err := child.StdoutPipe()
	if err != nil {
		t.Fatal(err)
	}
	stdin, err := child.StdinPipe()
	if err != nil {
		t.Fatal(err)
	}
	if err := child.Start(); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() {
		_ = stdin.Close()
		_ = child.Wait()
	})
	if line, err := bufio.NewReader(stdout).ReadString('\n'); err != nil || line != "locked\n" {
		t.Fatalf("helper ready = %q, %v", line, err)
	}

	_, err = Acquire(configDir, Exclusive)
	var inUse *InUseError
	if !errors.As(err, &inUse) {
		t.Fatalf("Acquire exclusive error = %v, want *InUseError", err)
	}
	if inUse.Path != configDir+"/runtime.lock" {
		t.Fatalf("lock path = %q", inUse.Path)
	}
}
