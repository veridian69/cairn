package model

import (
	"testing"
	"time"
)

func TestNewMessagePopulatesCoreFields(t *testing.T) {
	replyTo := "parent-id"
	author := Participant{ID: "p-1", Name: "operator", Kind: KindHuman}
	before := time.Now().UTC()
	msg := NewMessage(author, "hello agora", &replyTo)
	after := time.Now().UTC()

	if msg.ID == "" {
		t.Fatal("NewMessage should generate an ID")
	}
	if msg.AuthorID != author.ID || msg.AuthorName != author.Name {
		t.Fatalf("author fields = (%q, %q), want (%q, %q)", msg.AuthorID, msg.AuthorName, author.ID, author.Name)
	}
	if msg.Content != "hello agora" {
		t.Fatalf("Content = %q, want %q", msg.Content, "hello agora")
	}
	if msg.ReplyTo == nil || *msg.ReplyTo != replyTo {
		t.Fatalf("ReplyTo = %#v, want %q", msg.ReplyTo, replyTo)
	}
	if msg.Metadata == nil {
		t.Fatal("Metadata should be initialized")
	}
	if msg.CreatedAt.Before(before) || msg.CreatedAt.After(after) {
		t.Fatalf("CreatedAt = %v should be between %v and %v", msg.CreatedAt, before, after)
	}
}

func TestShortIDTruncatesLongIDsAndPreservesShortOnes(t *testing.T) {
	if got := ShortID("12345678"); got != "12345678" {
		t.Fatalf("ShortID exact length = %q, want %q", got, "12345678")
	}
	if got := ShortID("123456789abcdef"); got != "12345678" {
		t.Fatalf("ShortID long = %q, want %q", got, "12345678")
	}
	if got := ShortID("short"); got != "short" {
		t.Fatalf("ShortID short = %q, want %q", got, "short")
	}
}
