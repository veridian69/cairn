//go:build !windows

package main

import "testing"

func restrictTestSecret(t *testing.T, path string) {}
