// Package garden serves one authenticated, scoped Garden conversation and durable inboxes.
package garden

import "github.com/veridian69/cairn/garden/internal/gardenauth"

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
	// ReportDelivery asks for the per-recipient SendResult. Without it the send returns the
	// plain Message that clients built before delivery reporting decode.
	ReportDelivery bool `json:"report_delivery,omitempty"`
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

// HeartbeatArgs renews the session lease and declares whether the session can receive attention.
type HeartbeatArgs struct {
	ConsumerID  string `json:"consumer_id"`
	Deliverable bool   `json:"deliverable"`
}

// HeartbeatResult reports the lease that heartbeat renewed.
type HeartbeatResult struct {
	Generation     string `json:"generation"`
	LeaseExpiresAt string `json:"lease_expires_at"`
	Deliverable    bool   `json:"deliverable"`
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

// Delivery is the outcome for one recipient of a send. Reason is set only for suppression.
type Delivery struct {
	Recipient string `json:"recipient"`
	Delivery  string `json:"delivery"`
	Reason    string `json:"reason,omitempty"`
}

// SendResult is the stored message plus the per-recipient delivery outcomes.
type SendResult struct {
	Message
	Deliveries []Delivery `json:"deliveries"`
	// Notice asks the sender to involve the human when the loop guard suppressed attention.
	Notice string `json:"notice,omitempty"`
}

// Presence reports whether a participant holds a live session lease and whether it is deliverable.
type Presence struct {
	Name        string `json:"name"`
	Online      bool   `json:"online"`
	Deliverable bool   `json:"deliverable"`
}

// WhoResult lists every configured participant with its presence.
type WhoResult struct {
	Participants []Presence `json:"participants"`
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
