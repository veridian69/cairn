package mcpserver

// SetWaitBufferSizeForTest shrinks the wait subscription buffer so black-box
// tests can force the buffer-full backpressure path, and returns a restore
// func.
func SetWaitBufferSizeForTest(n int) (restore func()) {
	prev := waitBufferSize
	waitBufferSize = n
	return func() { waitBufferSize = prev }
}
