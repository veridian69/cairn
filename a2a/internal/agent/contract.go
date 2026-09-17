package agent

const contractHeader = `You can include a control block by replying with JSON instead of plain text:
{"message": "<what you say to the others>", "control": { ... }}

  "responsiveness": 0.05-1.0 — how often you choose to speak.
`

const contractMemory = `
  "remember": "<text>" — keep something in the memory you share with the
  other participants here.

Recalled memories arrive in a [memory] block at the start of your context.
They are the past, not the present — do not answer them as if they were
just said.
`

const contractFooter = `
Plain text replies are always fine. Use the JSON form only when you want a
control field.`

func ControlContract(memoryEnabled bool) string {
	if memoryEnabled {
		return contractHeader + contractMemory + contractFooter
	}
	return contractHeader + contractFooter
}
