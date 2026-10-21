package cmd

import (
	"bytes"
	"fmt"
	"net"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strings"
	"syscall"
	"testing"
	"time"

	"github.com/nats-io/nats.go"
)

func TestNotifyReady(t *testing.T) {
	t.Run("unset socket", func(t *testing.T) {
		t.Setenv("NOTIFY_SOCKET", "")
		if err := notifyReady(); err != nil {
			t.Fatal(err)
		}
	})
	t.Run("unavailable socket", func(t *testing.T) {
		t.Setenv("NOTIFY_SOCKET", filepath.Join(t.TempDir(), "missing.sock"))
		if err := notifyReady(); err == nil {
			t.Fatal("notifyReady succeeded without a listening socket")
		}
	})
}

func TestStartCommandNotify(t *testing.T) {
	if os.Getenv("GARDEN_TEST_NOTIFY_CHILD") == "1" {
		err := startCmd.RunE(startCmd, nil)
		if want := os.Getenv("GARDEN_TEST_NOTIFY_ERROR"); want != "" {
			if err == nil || !strings.Contains(err.Error(), want) {
				t.Fatalf("start error = %v, want %q", err, want)
			}
		} else if err != nil {
			t.Fatal(err)
		}
		return
	}

	for _, test := range []struct {
		name     string
		abstract bool
		failure  string
	}{
		{name: "filesystem"},
		{name: "abstract", abstract: true},
		{name: "config failure", failure: "config:"},
		{name: "PID publication failure", failure: "writing daemon PID:"},
	} {
		t.Run(test.name, func(t *testing.T) {
			if test.abstract && runtime.GOOS != "linux" {
				t.Skip("abstract Unix sockets require Linux")
			}
			// Keep the Unix socket path below the platform's length limit.
			dir, err := os.MkdirTemp("", "garden-notify-")
			if err != nil {
				t.Fatal(err)
			}
			t.Cleanup(func() { _ = os.RemoveAll(dir) })
			t.Setenv("HOME", dir)
			t.Setenv("GARDEN_TEST_NOTIFY_CHILD", "1")
			t.Setenv("GARDEN_TEST_NOTIFY_ERROR", test.failure)
			socket := filepath.Join(dir, "notify.sock")
			if test.abstract {
				socket = "@" + socket
			}
			t.Setenv("NOTIFY_SOCKET", socket)
			listener, err := net.ListenUnixgram("unixgram", &net.UnixAddr{Name: socket, Net: "unixgram"})
			if err != nil {
				t.Fatal(err)
			}
			defer listener.Close()
			if test.failure != "config:" {
				if err := os.MkdirAll(DefaultConfigDir(), 0700); err != nil {
					t.Fatal(err)
				}
				if err := os.WriteFile(filepath.Join(DefaultConfigDir(), "config.yaml"), []byte("agents: {}\nmemory:\n  enabled: false\n"), 0600); err != nil {
					t.Fatal(err)
				}
			}
			if test.failure == "writing daemon PID:" {
				if err := os.Mkdir(DaemonPIDPath(), 0700); err != nil {
					t.Fatal(err)
				}
			}

			child := exec.Command(os.Args[0], "-test.run=^TestStartCommandNotify$")
			var output bytes.Buffer
			child.Stdout = &output
			child.Stderr = &output
			if err := child.Start(); err != nil {
				t.Fatal(err)
			}
			done := make(chan error, 1)
			go func() { done <- child.Wait() }()
			defer func() {
				_ = child.Process.Kill()
				select {
				case err := <-done:
					if err != nil {
						t.Errorf("child: %v\n%s", err, output.String())
					}
				case <-time.After(10 * time.Second):
					t.Error("child did not exit")
				}
			}()

			if test.failure != "" {
				select {
				case err := <-done:
					done <- err
				case <-time.After(10 * time.Second):
					t.Fatal("startup failure did not exit")
				}
				if err := listener.SetReadDeadline(time.Now().Add(100 * time.Millisecond)); err != nil {
					t.Fatal(err)
				}
				var data [64]byte
				n, _, err := listener.ReadFromUnix(data[:])
				if timeout, ok := err.(net.Error); !ok || !timeout.Timeout() {
					t.Fatalf("notification on failed startup = %q, error = %v", data[:n], err)
				}
				return
			}

			if err := listener.SetReadDeadline(time.Now().Add(10 * time.Second)); err != nil {
				t.Fatal(err)
			}
			var data [64]byte
			n, _, err := listener.ReadFromUnix(data[:])
			if err != nil {
				t.Fatal(err)
			}
			if got := string(data[:n]); got != "READY=1" {
				t.Fatalf("notification = %q, want READY=1", got)
			}
			url, err := os.ReadFile(DaemonURLPath())
			if err != nil {
				t.Fatalf("daemon URL missing at readiness: %v", err)
			}
			pid, err := os.ReadFile(DaemonPIDPath())
			if err != nil || string(pid) != fmt.Sprint(child.Process.Pid) {
				t.Fatalf("daemon PID at readiness = %q, error = %v", pid, err)
			}
			nc, err := nats.Connect(string(url), nats.Timeout(time.Second), nats.NoReconnect())
			if err != nil {
				t.Fatalf("daemon not serving at readiness: %v", err)
			}
			nc.Close()
			if err := child.Process.Signal(syscall.SIGTERM); err != nil {
				t.Fatal(err)
			}
			select {
			case err := <-done:
				done <- err
			case <-time.After(10 * time.Second):
				t.Fatal("daemon did not stop after SIGTERM")
			}
		})
	}
}
