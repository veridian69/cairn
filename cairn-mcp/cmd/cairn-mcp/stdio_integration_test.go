package main

import (
	"bufio"
	"bytes"
	"context"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"sync/atomic"
	"testing"
	"time"
)

const stdioEndToEndTimeout = 5 * time.Second

type stdioRequest struct {
	body            string
	clientID        string
	clientSecret    string
	sessionID       string
	protocolVersion string
}

type stdioLineResult struct {
	line string
	err  error
}

func TestStdioEndToEnd(t *testing.T) {
	const (
		initializeRequest  = `{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18"}}`
		initializeResponse = `{"jsonrpc":"2.0","id":1,"result":{"protocolVersion":"2025-06-18","capabilities":{}}}`
		toolsListRequest   = `{"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}}`
		toolsListResponse  = `{"jsonrpc":"2.0","id":2,"result":{"tools":[]}}`
		clientID           = "client-id"
		clientSecret       = "client-secret"
		sessionID          = "stdio-session"
		protocolVersion    = "2025-06-18"
	)

	posts := make(chan stdioRequest, 2)
	deletes := make(chan stdioRequest, 1)
	var postCalls, deleteCalls atomic.Int32

	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		request := stdioRequest{
			clientID:        r.Header.Get("CF-Access-Client-Id"),
			clientSecret:    r.Header.Get("CF-Access-Client-Secret"),
			sessionID:       r.Header.Get("Mcp-Session-Id"),
			protocolVersion: r.Header.Get("Mcp-Protocol-Version"),
		}

		switch r.Method {
		case http.MethodPost:
			body, err := io.ReadAll(r.Body)
			if err != nil {
				http.Error(w, err.Error(), http.StatusBadRequest)
				return
			}
			request.body = string(body)
			postCalls.Add(1)
			select {
			case posts <- request:
			default:
			}

			switch request.body {
			case initializeRequest:
				w.Header().Set("Content-Type", "application/json")
				w.Header().Set("Mcp-Session-Id", sessionID)
				_, _ = io.WriteString(w, initializeResponse)
			case toolsListRequest:
				w.Header().Set("Content-Type", "text/event-stream")
				_, _ = io.WriteString(w, "data: {\"jsonrpc\":\"2.0\",\n")
				_, _ = io.WriteString(w, "data: \"id\":2,\"result\":{\"tools\":[]}}\n\n")
			default:
				http.Error(w, "unexpected POST", http.StatusBadRequest)
			}
		case http.MethodGet:
			// Legacy protocol versions may start a standalone SSE listener.
			w.WriteHeader(http.StatusMethodNotAllowed)
		case http.MethodDelete:
			deleteCalls.Add(1)
			select {
			case deletes <- request:
			default:
			}
			w.WriteHeader(http.StatusNoContent)
		default:
			w.WriteHeader(http.StatusMethodNotAllowed)
		}
	}))
	defer server.Close()

	stdin, stdinWriter := io.Pipe()
	stdoutReader, stdoutWriter := io.Pipe()
	t.Cleanup(func() {
		_ = stdinWriter.Close()
		_ = stdin.Close()
		_ = stdoutReader.Close()
	})

	var stderr bytes.Buffer
	commandDone := make(chan int, 1)
	go func() {
		code := run(context.Background(), []string{
			"stdio",
			"--allow-http-upstream",
			"--upstream-url", server.URL,
			"--cf-client-id-path", writeMode0600(t, "client-id", clientID),
			"--cf-client-secret-path", writeMode0600(t, "client-secret", clientSecret),
		}, stdin, stdoutWriter, &stderr)
		_ = stdoutWriter.Close()
		commandDone <- code
	}()

	stdout := bufio.NewReader(stdoutReader)
	writeStdioLine(t, stdinWriter, initializeRequest)
	assertStdioJSONLine(t, readStdioLine(t, stdout, "initialize response"), initializeResponse)

	writeStdioLine(t, stdinWriter, toolsListRequest)
	assertStdioJSONLine(t, readStdioLine(t, stdout, "tools/list response"), toolsListResponse)

	closeStdioInput(t, stdinWriter)
	deleted := readStdioRequest(t, deletes, "authenticated DELETE")
	assertStdioAccessHeaders(t, "DELETE", deleted, clientID, clientSecret)
	if deleted.sessionID != sessionID || deleted.protocolVersion != protocolVersion {
		t.Fatalf("DELETE MCP headers = %#v, want session %q and protocol %q", deleted, sessionID, protocolVersion)
	}

	if code := readStdioExitCode(t, commandDone); code != 0 {
		t.Fatalf("command exit code = %d, want 0", code)
	}
	if stderr.Len() != 0 {
		t.Fatalf("stderr = %q, want empty", stderr.String())
	}
	if remaining, err := io.ReadAll(stdoutReader); err != nil {
		t.Fatalf("read remaining stdout: %v", err)
	} else if len(remaining) != 0 {
		t.Fatalf("stdout contains bytes after two protocol lines: %q", remaining)
	}

	firstPost := readStdioRequest(t, posts, "initialize POST")
	secondPost := readStdioRequest(t, posts, "tools/list POST")
	if firstPost.body != initializeRequest {
		t.Fatalf("initialize POST body = %q, want %q", firstPost.body, initializeRequest)
	}
	assertStdioAccessHeaders(t, "initialize POST", firstPost, clientID, clientSecret)
	if firstPost.sessionID != "" || firstPost.protocolVersion != "" {
		t.Fatalf("initialize POST MCP headers = %#v, want empty", firstPost)
	}
	if secondPost.body != toolsListRequest {
		t.Fatalf("tools/list POST body = %q, want %q", secondPost.body, toolsListRequest)
	}
	assertStdioAccessHeaders(t, "tools/list POST", secondPost, clientID, clientSecret)
	if secondPost.sessionID != sessionID || secondPost.protocolVersion != protocolVersion {
		t.Fatalf("tools/list POST MCP headers = %#v, want session %q and protocol %q", secondPost, sessionID, protocolVersion)
	}
	if got := postCalls.Load(); got != 2 {
		t.Fatalf("POST calls = %d, want 2", got)
	}
	if got := deleteCalls.Load(); got != 1 {
		t.Fatalf("DELETE calls = %d, want 1", got)
	}
}

func assertStdioAccessHeaders(t *testing.T, operation string, request stdioRequest, clientID, clientSecret string) {
	t.Helper()
	if request.clientID != clientID || request.clientSecret != clientSecret {
		t.Fatalf("%s Cloudflare headers = %#v, want client ID %q and secret %q", operation, request, clientID, clientSecret)
	}
}

func assertStdioJSONLine(t *testing.T, line, want string) {
	t.Helper()
	if line != want {
		t.Fatalf("stdout JSON line = %q, want %q", line, want)
	}
	if !json.Valid([]byte(line)) {
		t.Fatalf("stdout line is not valid JSON: %q", line)
	}
}

func writeStdioLine(t *testing.T, writer *io.PipeWriter, line string) {
	t.Helper()
	done := make(chan error, 1)
	go func() {
		_, err := io.WriteString(writer, line+"\n")
		done <- err
	}()
	select {
	case err := <-done:
		if err != nil {
			t.Fatalf("write stdin line: %v", err)
		}
	case <-time.After(stdioEndToEndTimeout):
		t.Fatal("timed out writing stdin line")
	}
}

func closeStdioInput(t *testing.T, writer *io.PipeWriter) {
	t.Helper()
	done := make(chan error, 1)
	go func() { done <- writer.Close() }()
	select {
	case err := <-done:
		if err != nil {
			t.Fatalf("close stdin: %v", err)
		}
	case <-time.After(stdioEndToEndTimeout):
		t.Fatal("timed out closing stdin")
	}
}

func readStdioLine(t *testing.T, reader *bufio.Reader, description string) string {
	t.Helper()
	done := make(chan stdioLineResult, 1)
	go func() {
		line, err := reader.ReadString('\n')
		done <- stdioLineResult{line: line, err: err}
	}()
	select {
	case result := <-done:
		if result.err != nil {
			t.Fatalf("read %s: %v", description, result.err)
		}
		return result.line[:len(result.line)-1]
	case <-time.After(stdioEndToEndTimeout):
		t.Fatalf("timed out waiting for %s", description)
		return ""
	}
}

func readStdioRequest(t *testing.T, requests <-chan stdioRequest, description string) stdioRequest {
	t.Helper()
	select {
	case request := <-requests:
		return request
	case <-time.After(stdioEndToEndTimeout):
		t.Fatalf("timed out waiting for %s", description)
		return stdioRequest{}
	}
}

func readStdioExitCode(t *testing.T, commandDone <-chan int) int {
	t.Helper()
	select {
	case code := <-commandDone:
		return code
	case <-time.After(stdioEndToEndTimeout):
		t.Fatal("timed out waiting for command exit")
		return 0
	}
}
