package bridge

import (
	"context"
	"encoding/json"
	"io"
	"sync"
	"time"

	"github.com/veridian69/cairn/cairn-mcp/internal/stdio"
	"github.com/veridian69/cairn/cairn-mcp/internal/upstream"
)

const (
	maxActiveSends       = 32
	cleanEOFDrainTimeout = 5 * time.Second
	cleanupTimeout       = 5 * time.Second
)

type Remote interface {
	Send(context.Context, json.RawMessage) error
	Events() <-chan upstream.Event
	Close(context.Context) error
}

type terminalResult struct {
	err                error
	callerCancellation bool
}

func Run(ctx context.Context, input io.ReadCloser, output io.Writer, remote Remote) error {
	return runWithTimeouts(ctx, input, output, remote, cleanEOFDrainTimeout, cleanupTimeout)
}

func runWithCleanEOFDrainTimeout(ctx context.Context, input io.ReadCloser, output io.Writer, remote Remote, drainTimeout time.Duration) error {
	return runWithTimeouts(ctx, input, output, remote, drainTimeout, cleanupTimeout)
}

func runWithTimeouts(ctx context.Context, input io.ReadCloser, output io.Writer, remote Remote, drainTimeout, cleanupDuration time.Duration) error {
	sendCtx, cancelSends := context.WithCancel(context.WithoutCancel(ctx))
	defer cancelSends()

	terminal := make(chan terminalResult, 1)
	var terminalOnce sync.Once
	reportTerminal := func(result terminalResult) {
		terminalOnce.Do(func() { terminal <- result })
	}

	reader := stdio.NewReader(input)
	writer := stdio.NewWriter(output)
	cleanEOF := make(chan struct{}, 1)
	inputDone := make(chan struct{})
	sendSlots := make(chan struct{}, maxActiveSends)
	var sends sync.WaitGroup
	go func() {
		defer close(inputDone)
		for {
			message, err := reader.Read()
			if err == io.EOF {
				cleanEOF <- struct{}{}
				return
			}
			if err != nil {
				reportTerminal(terminalResult{err: err})
				return
			}

			select {
			case sendSlots <- struct{}{}:
			case <-sendCtx.Done():
				return
			}

			sends.Add(1)
			go func() {
				defer sends.Done()
				defer func() { <-sendSlots }()
				if err := remote.Send(sendCtx, message); err != nil && sendCtx.Err() == nil {
					reportTerminal(terminalResult{err: err})
				}
			}()
		}
	}()

	eventsDone := make(chan struct{})
	go func() {
		defer close(eventsDone)
		writeEvents := true
		for event := range remote.Events() {
			if event.Err != nil {
				reportTerminal(terminalResult{err: event.Err})
				writeEvents = false
				continue
			}
			if writeEvents {
				if err := writer.Write(event.Message); err != nil {
					reportTerminal(terminalResult{err: err})
					writeEvents = false
				}
			}
		}
	}()

	var first terminalResult
	hasTerminal := false
	if ctx.Err() != nil {
		first = terminalResult{callerCancellation: true}
		hasTerminal = true
	} else {
		select {
		case <-ctx.Done():
			first = terminalResult{callerCancellation: true}
			hasTerminal = true
		case first = <-terminal:
			hasTerminal = true
		case <-cleanEOF:
			<-inputDone
			if ctx.Err() != nil {
				first = terminalResult{callerCancellation: true}
				hasTerminal = true
			} else {
				select {
				case first = <-terminal:
					hasTerminal = true
				default:
				}
			}
		}
	}

	if !hasTerminal {
		sendsDone := make(chan struct{})
		go func() {
			sends.Wait()
			close(sendsDone)
		}()
		drainTimer := time.NewTimer(drainTimeout)
		select {
		case <-ctx.Done():
			first = terminalResult{callerCancellation: true}
			hasTerminal = true
			cancelSends()
			_ = input.Close()
			<-sendsDone
		case first = <-terminal:
			hasTerminal = true
			cancelSends()
			_ = input.Close()
			<-sendsDone
		case <-sendsDone:
			if ctx.Err() != nil {
				first = terminalResult{callerCancellation: true}
				hasTerminal = true
			} else {
				select {
				case first = <-terminal:
					hasTerminal = true
				default:
				}
			}
		case <-drainTimer.C:
			cancelSends()
			<-sendsDone
		}
		if !drainTimer.Stop() {
			select {
			case <-drainTimer.C:
			default:
			}
		}
	} else {
		cancelSends()
		_ = input.Close()
		<-inputDone
		sends.Wait()
	}

	cancelSends()
	cleanupCtx, cancelCleanup := context.WithTimeout(context.Background(), cleanupDuration)
	closeErr := remote.Close(cleanupCtx)
	cancelCleanup()

	// A slow or unreachable upstream can spend the whole close budget before
	// the events channel is closed, so the stdout drain gets a fresh one.
	eventsTimer := time.NewTimer(cleanupDuration)
	select {
	case <-eventsDone:
	case <-eventsTimer.C:
	}
	eventsTimer.Stop()

	if !hasTerminal {
		select {
		case first = <-terminal:
			hasTerminal = true
		default:
			if ctx.Err() != nil {
				first = terminalResult{callerCancellation: true}
				hasTerminal = true
			}
		}
	}
	if hasTerminal {
		if first.callerCancellation {
			return nil
		}
		return first.err
	}
	return closeErr
}
