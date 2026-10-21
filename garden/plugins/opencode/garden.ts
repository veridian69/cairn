// Garden plugin for OpenCode 2.x (V2 plugin: {id, setup(ctx)}). One file, Node built-ins only.
//
// The plugin runs `garden connect --harness opencode` from PATH as a child process and is the MCP
// client over its stdio. It re-exposes the core's tools, pushes the core's instructions into the
// model's system prompt, reports the session a human prompts in, and injects Garden messages as
// visible user turns. It only connects under `opencode --standalone` (see standalonePrivateServer).
import { spawn, type ChildProcessWithoutNullStreams } from "node:child_process"
import { createHash, randomBytes } from "node:crypto"

const SOURCE = "garden"
const PROTOCOL = "2025-11-25"
const MESSAGE = "notifications/garden/message"
const SESSION = "notifications/garden/session"
const FAILED = "notifications/garden/delivery_failed"
const INSTRUCTIONS = "notifications/garden/instructions"
const STARTUP_TIMEOUT_MS = 15_000
const STOP_TIMEOUT_MS = 3_000
// The core's own confirmation tool; the plugin calls it, the model does not.
const ACKNOWLEDGE = "acknowledge_delivery"

type Pending = { resolve: (value: any) => void; reject: (error: Error) => void }

// OpenCode 2.0.32's native IDs start with six hex bytes of (milliseconds * 4096 +
// counter), followed by 14 base62 characters. Revert compares these IDs lexically.
// Reserve the last counter slot and cross its millisecond before submitting, so
// native turns submitted afterwards sort after this admission even in a fast TUI.
function chronologicalID(now: number): string {
  const prefix = ((BigInt(now) * 0x1000n + 0xfffn) & 0xffffffffffffn).toString(16).padStart(12, "0")
  const alphabet = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
  const suffix = Array.from(randomBytes(14), (value) => alphabet[value % 62]).join("")
  return "msg_" + prefix + suffix
}

function admissionKey(sessionID: string, messageID: string): string {
  return "admission:" + createHash("sha256").update(sessionID + "\x00" + messageID).digest("hex")
}

async function loadAdmission(storage: any, sessionID: string, messageID: string): Promise<{ id: string; reused: boolean }> {
  const key = admissionKey(sessionID, messageID)
  const stored = await storage.get(key)
  if (stored !== undefined) {
    if (typeof stored !== "string" || !/^msg_[0-9a-f]{12}[0-9A-Za-z]{14}$/.test(stored)) throw new Error("invalid stored Garden admission ID")
    return { id: stored, reused: true }
  }
  return { id: await persistAdmissionID(storage, key), reused: false }
}

async function persistAdmissionID(storage: any, key: string): Promise<string> {
  const now = Date.now()
  const id = chronologicalID(now)
  await storage.set(key, id)
  while (Date.now() <= now) await new Promise((resolve) => setTimeout(resolve, 1))
  return id
}

// Persist before submission. Reuse is reconciled at the prompt hook below, since
// a saved ID may never have reached OpenCode. Storage belongs to OpenCode.
export async function admissionID(storage: any, sessionID: string, messageID: string): Promise<string> {
  return (await loadAdmission(storage, sessionID, messageID)).id
}

// standalonePrivateServer reports whether this process is the private server that
// `opencode --standalone` starts for one TUI. Its argv is `serve --stdio --port 0`, and it
// inherits the TUI environment, including GARDEN_IDENTITY. The shared background service runs
// `serve --service`, where one plugin instance serves every TUI and cannot tell them apart
// (feasibility results, 19 October 2026). Anything else is treated as not standalone.
export function standalonePrivateServer(argv: string[] = process.argv): boolean {
  return argv.includes("--stdio") && !argv.includes("--service")
}

// Client is a JSON-RPC MCP client over the core's stdio. It is closed by ending the child's stdin.
class Client {
  private child: ChildProcessWithoutNullStreams
  private nextID = 1
  private pending = new Map<number, Pending>()
  private buffer = ""
  private closedReason: string | undefined
  // onNotify receives every notification the core sends the plugin.
  onNotify: (method: string, params: any) => void = () => {}

  constructor() {
    this.child = spawn("garden", ["connect", "--harness", "opencode"], { stdio: ["pipe", "pipe", "ignore"], env: process.env })
    this.child.stdout.setEncoding("utf8")
    this.child.stdout.on("data", (chunk: string) => this.read(chunk))
    this.child.on("error", (error) => this.close(`cannot start garden: ${error.message}`))
    // A core that closes its input before its exit is seen makes writes fail with EPIPE. Without a
    // listener that stream error is unhandled and would end the whole OpenCode process.
    this.child.stdin.on("error", (error) => this.close(`garden connect stopped reading: ${error.message}`))
    this.child.stdout.on("error", (error) => this.close(`garden connect output failed: ${error.message}`))
    this.child.on("exit", (code) => this.close(`garden connect exited with status ${code ?? "unknown"}`))
  }

  get open(): boolean {
    return this.closedReason === undefined
  }

  get closeReason(): string {
    return this.closedReason ?? "Garden is not running"
  }

  request(method: string, params: unknown = {}): Promise<any> {
    if (!this.open) return Promise.reject(new Error(this.closeReason))
    const id = this.nextID++
    return new Promise((resolve, reject) => {
      this.pending.set(id, { resolve, reject })
      this.child.stdin.write(JSON.stringify({ jsonrpc: "2.0", id, method, params }) + "\n")
    })
  }

  notify(method: string, params: unknown): void {
    if (this.open) this.child.stdin.write(JSON.stringify({ jsonrpc: "2.0", method, params }) + "\n")
  }

  // stop ends the core by closing its stdin, which releases the identity claim. The child is
  // killed only if it has not exited shortly after.
  stop(): void {
    if (!this.open) return
    this.child.stdin.end()
    const timer = setTimeout(() => this.child.kill(), STOP_TIMEOUT_MS)
    timer.unref?.()
    this.child.once("exit", () => clearTimeout(timer))
  }

  private read(chunk: string): void {
    this.buffer += chunk
    let newline = this.buffer.indexOf("\n")
    while (newline >= 0) {
      const line = this.buffer.slice(0, newline).trim()
      this.buffer = this.buffer.slice(newline + 1)
      if (line) this.frame(line)
      newline = this.buffer.indexOf("\n")
    }
  }

  private frame(line: string): void {
    let message: any
    try {
      message = JSON.parse(line)
    } catch {
      return
    }
    if (message.id !== undefined && (message.result !== undefined || message.error !== undefined)) {
      const waiter = this.pending.get(message.id)
      if (!waiter) return
      this.pending.delete(message.id)
      if (message.error) waiter.reject(new Error(message.error.message ?? "Garden request failed"))
      else waiter.resolve(message.result)
      return
    }
    if (typeof message.method === "string") this.onNotify(message.method, message.params)
  }

  private close(reason: string): void {
    if (!this.open) return
    this.closedReason = reason
    for (const waiter of this.pending.values()) waiter.reject(new Error(reason))
    this.pending.clear()
  }
}

// connect performs the MCP handshake and reads the core's tool list.
async function connect(client: Client): Promise<{ instructions: string; tools: any[] }> {
  const init = await client.request("initialize", { protocolVersion: PROTOCOL, capabilities: {}, clientInfo: { name: "opencode-garden", version: "1" } })
  client.notify("notifications/initialized", {})
  const listed = await client.request("tools/list", {})
  return { instructions: String(init?.instructions ?? ""), tools: Array.isArray(listed?.tools) ? listed.tools : [] }
}

function withTimeout<T>(promise: Promise<T>, ms: number): Promise<T> {
  let timer: ReturnType<typeof setTimeout> | undefined
  const expired = new Promise<never>((_, reject) => {
    timer = setTimeout(() => reject(new Error("Garden did not answer in time")), ms)
  })
  return Promise.race([promise, expired]).finally(() => clearTimeout(timer))
}

// sessionNotFound recognises OpenCode's "no such session" rejection. Probed on 2.0.32: get and
// context reject a deleted or never-existing session with _tag (and name) "Session.NotFoundError";
// a malformed ID rejects with SchemaError; constructor names are minified and messages are empty.
function sessionNotFound(error: any): boolean {
  return error?._tag === "Session.NotFoundError" || error?.name === "Session.NotFoundError"
}

// admitted reports whether OpenCode holds the injection. Absence from the session's context proves
// nothing: a queued admission stays in the session inbox, outside the projected history, until it is
// delivered. Only OpenCode's explicit Session.NotFoundError counts as "gone"; every other outcome,
// including unrecognised responses and other errors, is "unknown" and is retried in the same session
// under the same id.
async function admitted(ctx: any, sessionID: string, id: string): Promise<"yes" | "gone" | "unknown"> {
  try {
    const found = await ctx.session.context({ sessionID })
    const messages: any[] = Array.isArray(found) ? found : Array.isArray(found?.data) ? found.data : []
    if (messages.some((message) => message?.id === id)) return "yes"
  } catch (error) {
    if (sessionNotFound(error)) return "gone"
  }
  try {
    await ctx.session.get({ sessionID })
  } catch (error) {
    if (sessionNotFound(error)) return "gone"
  }
  return "unknown"
}

// admittedForTest exposes admitted to the plugin tests.
export const admittedForTest = admitted

// statusOnly serves the status tool alone, for a session that has no core to ask.
function statusOnly(ctx: any, reason: string): Promise<void> {
  return ctx.tool.transform((editor: any) => {
    editor.add({
      name: "status",
      description: "Explain why Garden is not connected in this session.",
      input: { type: "object", properties: {}, additionalProperties: false },
      async execute() {
        return { content: JSON.stringify({ connected: false, reason }) }
      },
    })
  })
}

export default {
  id: "garden",
  async setup(ctx: any) {
    if (!standalonePrivateServer()) {
      // Shared background service: no core and no identity claim, so no delivery.
      await statusOnly(ctx, "this OpenCode runs in the shared background service, which cannot tell sessions apart; relaunch with opencode --standalone")
      return
    }

    const client = new Client()
    const connection = withTimeout(connect(client), STARTUP_TIMEOUT_MS)
    // Sessions reported before the core answers wait here; a failed start leaves them unreported.
    const ready = connection.then(() => undefined)
    ready.catch(() => {})

    let instructions = ""
    let tracked: string | undefined
    let reported: string | undefined
    const latestNative = new Map<string, string>()
    const reconciling = new Map<string, { refresh: boolean; unadmitted: boolean }>()

    // Session tracking: the session the human last prompted. The plugin's own injections carry
    // metadata.source "garden" and are ignored.
    await ctx.session.hook("prompt", (event: any) => {
      const own = event?.metadata?.source === SOURCE || event?.prompt?.metadata?.source === SOURCE
      if (typeof event?.sessionID !== "string") return
      if (own) {
        // 2.0.32 reconciles durable inbox IDs BEFORE this hook. Reaching it proves
        // this ID was never admitted, even if it was saved before a process crash.
        const attempt = reconciling.get(event.sessionID + "/" + event.messageID)
        const native = latestNative.get(event.sessionID)
        if (attempt && (attempt.refresh || (native !== undefined && native >= event.messageID))) {
          attempt.unadmitted = true
          throw new Error("Garden admission needs a fresh chronological ID")
        }
        return
      }
      if (typeof event.messageID === "string") latestNative.set(event.sessionID, event.messageID)
      tracked = event.sessionID
      if (reported === tracked) return
      void ready
        .then(() => {
          if (tracked === event.sessionID && client.open) {
            reported = tracked
            client.notify(SESSION, { session_id: tracked })
          }
        })
        .catch(() => {})
    })

    // A failed injection is reported, never acknowledged. The core then holds the message
    // pending and waits for a new session report, which the plugin sends at the next human prompt.
    const fail = (messageID: string, reason: string) => {
      reported = undefined
      client.notify(FAILED, { message_id: messageID, reason })
    }

    // Delivery: inject the core's text as a visible user turn, then confirm it to the core.
    client.onNotify = (method: string, params: any) => {
      if (method === INSTRUCTIONS) {
        if (typeof params?.text === "string") instructions = params.text
        return
      }
      if (method !== MESSAGE) return
      const messageID = params?.meta?.message_id
      if (typeof messageID !== "string" || typeof params?.content !== "string") return
      void (async () => {
        // Retain the target even after acknowledgement: the core can end before
        // its gateway acknowledgement commits. Recipient scopes multi-principal
        // messages sharing OpenCode's plugin storage to separate deliveries.
        const recipient = params?.meta?.recipient
        if (typeof recipient !== "string" || !recipient) return fail(messageID, "missing Garden recipient")
        const targetKey = "target:" + createHash("sha256").update(recipient + "\x00" + messageID).digest("hex")
        let sessionID: string
        let id: string
        let reused: boolean
        try {
          const stored = await ctx.storage.get(targetKey)
          const target = stored === undefined ? tracked : stored
          if (target === undefined) return fail(messageID, "no session has been prompted yet in this OpenCode instance")
          if (typeof target !== "string" || !target.startsWith("ses")) throw new Error("invalid stored Garden target")
          sessionID = target
          await ctx.storage.set(targetKey, sessionID)
          ;({ id, reused } = await loadAdmission(ctx.storage, sessionID, messageID))
        } catch {
          return fail(messageID, "cannot persist Garden admission ID; nothing was submitted")
        }
        const submit = async (refresh: boolean): Promise<boolean> => {
          const key = sessionID + "/" + id
          const attempt = { refresh, unadmitted: false }
          reconciling.set(key, attempt)
          try {
            await ctx.session.prompt({ id, sessionID, text: params.content, delivery: "queue", metadata: { source: SOURCE } })
            return true
          } catch (error) {
            if (attempt.unadmitted) return false
            throw error
          } finally {
            reconciling.delete(key)
          }
        }
        try {
          if (!await submit(reused)) {
            id = await persistAdmissionID(ctx.storage, admissionKey(sessionID, messageID))
            if (!await submit(false)) throw new Error("a native prompt overtook Garden admission; retry after the next prompt")
          }
        } catch (error) {
          const reason = "prompt injection failed: " + (error instanceof Error ? error.message : String(error))
          // OpenCode may have admitted the message before the error. Found counts as delivered and is
          // acknowledged below; unknown is not acknowledged and is redelivered with the same id.
          const state = await admitted(ctx, sessionID, id)
          if (state === "gone") {
            // The target session no longer exists, so its queued copy cannot run; a new target is safe.
            try {
              await ctx.storage.remove(targetKey)
            } catch {
              return fail(messageID, reason + "; cannot release the stored target")
            }
            return fail(messageID, reason + "; the target session no longer exists")
          }
          if (state === "unknown") {
            return fail(messageID, reason + "; the outcome is unknown, so the message is redelivered with the same id to the same session")
          }
        }
        await client.request("tools/call", { name: ACKNOWLEDGE, arguments: { message_id: messageID } })
      })().catch(() => {})
    }

    await ctx.session.hook("context", (event: any) => {
      if (instructions) event.system.push({ type: "text", text: instructions })
    })

    let core: { instructions: string; tools: any[] }
    try {
      core = await connection
    } catch (error) {
      client.stop()
      await statusOnly(ctx, error instanceof Error ? error.message : String(error))
      return
    }

    // The core answers even when it is disconnected, with status only and its own reason.
    instructions = core.instructions
    const tools = core.tools.filter((tool) => tool.name !== ACKNOWLEDGE)
    await ctx.tool.transform((editor: any) => {
      for (const tool of tools) {
        editor.add({
          name: tool.name,
          description: tool.description ?? "",
          input: tool.inputSchema ?? { type: "object", properties: {} },
          async execute(input: unknown) {
            const result = await client.request("tools/call", { name: tool.name, arguments: input ?? {} })
            const text = Array.isArray(result?.content) ? result.content.map((part: any) => part?.text ?? "").join("\n") : JSON.stringify(result)
            if (result?.isError) throw new Error(text)
            return { content: text }
          },
        })
      }
    })

    return () => client.stop()
  },
}
