package model

import (
	"time"

	"github.com/google/uuid"
)

const (
	KindHuman  = "human"
	KindAgent  = "agent"
	KindSystem = "system"
)

type Participant struct {
	ID       string `json:"id"`
	Name     string `json:"name"`
	Kind     string `json:"kind"`
	Provider string `json:"provider,omitempty"`
	Model    string `json:"model,omitempty"`
}

type Message struct {
	ID         string            `json:"id"`
	AuthorID   string            `json:"author_id"`
	AuthorName string            `json:"author_name"`
	Content    string            `json:"content"`
	ReplyTo    *string           `json:"reply_to,omitempty"`
	Accountant *AccountantRecord `json:"accountant,omitempty"`
	Metadata   map[string]any    `json:"metadata,omitempty"`
	CreatedAt  time.Time         `json:"created_at"`
}

func NewMessage(author Participant, content string, replyTo *string) Message {
	return Message{
		ID:         uuid.New().String(),
		AuthorID:   author.ID,
		AuthorName: author.Name,
		Content:    content,
		ReplyTo:    replyTo,
		Metadata:   make(map[string]any),
		CreatedAt:  time.Now().UTC(),
	}
}

func ShortID(id string) string {
	if len(id) <= 8 {
		return id
	}
	return id[:8]
}
