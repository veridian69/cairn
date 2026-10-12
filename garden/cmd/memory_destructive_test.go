package cmd

import (
	"os"
	"path/filepath"
	"testing"
)

func TestMemoryResetRefusesWhileStoreLockIsHeld(t *testing.T) {
	store, _ := setupMemoryHome(t)
	old := resetYes
	resetYes = true
	t.Cleanup(func() { resetYes = old })
	if err := memoryResetCmd.RunE(memoryResetCmd, nil); err == nil {
		t.Fatal("reset should refuse while a store is open")
	}
	if err := store.Close(); err != nil {
		t.Fatal(err)
	}
	if err := memoryResetCmd.RunE(memoryResetCmd, nil); err != nil {
		t.Fatal(err)
	}
	if _, err := os.Stat(filepath.Join(DefaultDataDir(), "memory.db")); !os.IsNotExist(err) {
		t.Fatalf("memory.db survived reset: %v", err)
	}
}
