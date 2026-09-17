package main

import (
	"os"
	"os/exec"
	"strings"
	"testing"
)

func TestMainVersionEntrypoint(t *testing.T) {
	cmd := exec.Command(os.Args[0], "-test.run=TestMainHelperProcess", "--", "--version")
	cmd.Env = append(os.Environ(), "A2A_MAIN_HELPER=1")
	output, err := cmd.CombinedOutput()
	if err != nil {
		t.Fatalf("main helper process failed: %v\n%s", err, string(output))
	}
	if !strings.Contains(string(output), "a2a version") {
		t.Fatalf("main entrypoint output = %q, want version text", string(output))
	}
}

func TestMainHelperProcess(t *testing.T) {
	if os.Getenv("A2A_MAIN_HELPER") != "1" {
		return
	}
	args := []string{"a2a"}
	for i, arg := range os.Args {
		if arg == "--" {
			args = append(args, os.Args[i+1:]...)
			break
		}
	}
	os.Args = args
	main()
	os.Exit(0)
}
