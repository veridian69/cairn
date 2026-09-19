// Package garden serves one authenticated, scoped Garden conversation and durable inboxes.
package garden

import "github.com/veridian69/cairn/a2a/internal/gardenauth"

// Binding identifies the immutable authority boundary of a deployment.
type Binding struct {
	InstanceID     string           `json:"instance_id"`
	Scope          gardenauth.Scope `json:"scope"`
	Classification string           `json:"classification"`
}

// SendArgs addresses attention explicitly; recipients do not make messages private.
type SendArgs struct {
	Content    string   `json:"content"`
	Recipients []string `json:"recipients,omitempty"`
	ReplyTo    string   `json:"reply_to,omitempty"`
}

// ReadArgs selects a bounded, generation-aware history page.
type ReadArgs struct {
	AfterSeq   uint64 `json:"after_seq,omitempty"`
	Generation string `json:"generation,omitempty"`
	Limit      int    `json:"limit,omitempty"`
}

// PollArgs identifies one adapter process and its maximum wait.
type PollArgs struct {
	ConsumerID  string `json:"consumer_id"`
	WaitSeconds int    `json:"wait_seconds,omitempty"`
}

// AckArgs proves acceptance of a previously returned receipt.
type AckArgs struct {
	ConsumerID string `json:"consumer_id"`
	Receipt    string `json:"receipt"`
}

// Message is a redaction-aware conversation record with server-owned provenance.
type Message struct {
	ID         string   `json:"id"`
	Sequence   uint64   `json:"sequence"`
	AuthorID   string   `json:"author_id"`
	AuthorName string   `json:"author_name"`
	Content    string   `json:"content"`
	Recipients []string `json:"recipients"`
	ReplyTo    string   `json:"reply_to,omitempty"`
	CreatedAt  string   `json:"created_at"`
	Binding    Binding  `json:"binding"`
}

// PollResult contains at most one unacknowledged message.
type PollResult struct {
	Message        *Message `json:"message,omitempty"`
	Receipt        string   `json:"receipt,omitempty"`
	Generation     string   `json:"generation"`
	LeaseExpiresAt string   `json:"lease_expires_at"`
}

// ReadResult reports a history page without advancing an inbox.
type ReadResult struct {
	Messages   []Message `json:"messages"`
	Generation string    `json:"generation"`
	NextSeq    uint64    `json:"next_seq"`
	More       bool      `json:"more"`
}

// StatusResult describes the authenticated participant and deployment.
type StatusResult struct {
	Binding      Binding  `json:"binding"`
	Participant  string   `json:"participant"`
	Generation   string   `json:"generation"`
	Participants []string `json:"participants"`
}

// Error carries a stable safe code across MCP and HTTP failures.
type Error struct {
	Code    string `json:"code"`
	Message string `json:"message"`
}

func (e *Error) Error() string           { return e.Code + ": " + e.Message }
func failure(code, message string) error { return &Error{Code: code, Message: message} }
