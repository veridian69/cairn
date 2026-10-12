package transport

import "testing"

func TestServerRefusesConcurrentStoreOwnership(t *testing.T) {
	dir := t.TempDir()
	first, err := NewServer(dir)
	if err != nil {
		t.Fatal(err)
	}
	defer first.Stop()
	second, err := NewServer(dir)
	if err == nil {
		second.Stop()
		t.Fatal("two local daemons opened the same persistent NATS store")
	}
	first.Stop()
	resumed, err := NewServer(dir)
	if err != nil {
		t.Fatal("stopped daemon retained store ownership")
	}
	resumed.Stop()
}
