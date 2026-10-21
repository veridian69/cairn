// Plugin tests for garden.ts. A fake `garden` on PATH speaks the core's JSON-RPC over stdio,
// so the plugin's child-process path runs for real. Run with:
//   node --test plugins/opencode/garden.test.ts
import { test } from "node:test"
import assert from "node:assert/strict"
import { createHash } from "node:crypto"
import { chmodSync, existsSync, mkdirSync, mkdtempSync, readFileSync, writeFileSync } from "node:fs"
import { tmpdir } from "node:os"
import { join } from "node:path"
import plugin from "./garden.ts"

// FAKE_CORE behaves like the core for the cases under test. Every frame it receives is logged to
// FAKE_LOG. Its first session report also replaces the instructions; every session report sends
// message m1; the poke tool sends message m0 without any session report.
const FAKE_CORE = `#!/usr/bin/env node
const fs = require("fs")
const log = (value) => fs.appendFileSync(process.env.FAKE_LOG, JSON.stringify(value) + "\\n")
const send = (value) => process.stdout.write(JSON.stringify(value) + "\\n")
const reply = (id, result) => send({ jsonrpc: "2.0", id, result })
let sessions = 0
const message = (id) => send({ jsonrpc: "2.0", method: "notifications/garden/message", params: { content: "hello from tester", meta: { source: "garden", message_id: id, sender: "tester", recipient: "oc" } } })
let buffer = ""
process.stdin.setEncoding("utf8")
process.stdin.on("data", (chunk) => {
  buffer += chunk
  let newline
  while ((newline = buffer.indexOf("\\n")) >= 0) {
    const line = buffer.slice(0, newline)
    buffer = buffer.slice(newline + 1)
    if (line.trim()) handle(JSON.parse(line))
  }
})
function handle(m) {
  log({ method: m.method, id: m.id ?? null, params: m.params ?? null })
  switch (m.method) {
    case "initialize":
      return reply(m.id, { protocolVersion: "2025-11-25", capabilities: { tools: {} }, instructions: "first instructions", serverInfo: { name: "fake", version: "1" } })
    case "tools/list":
      if (process.env.FAKE_CLOSE_STDIN) setTimeout(() => { process.stdin.removeAllListeners("data"); process.stdin.pause(); fs.closeSync(0); setTimeout(() => process.exit(0), 3000) }, 50)
      return reply(m.id, { tools: [
        { name: "status", description: "status", inputSchema: { type: "object", properties: {} } },
        { name: "poke", description: "poke", inputSchema: { type: "object", properties: {} } },
        { name: "acknowledge_delivery", description: "ack", inputSchema: { type: "object", properties: {} } },
      ] })
    case "tools/call":
      if (m.params && m.params.name === "poke") message("m0")
      return reply(m.id, { content: [{ type: "text", text: "ok" }], structuredContent: { acknowledged: true } })
    case "notifications/garden/session":
      sessions += 1
      if (sessions === 1) send({ jsonrpc: "2.0", method: "notifications/garden/instructions", params: { text: "replacement instructions" } })
      return message("m1")
    default:
      return
  }
}
`

type Frame = { method?: string; id?: number | null; params?: any }

function fakeCore(recipient = "oc"): { dir: string; log: string } {
  const dir = mkdtempSync(join(tmpdir(), "garden-plugin-test-"))
  const bin = join(dir, "bin")
  mkdirSync(bin)
  writeFileSync(join(bin, "garden"), FAKE_CORE.replace('recipient: "oc"', "recipient: " + JSON.stringify(recipient)))
  chmodSync(join(bin, "garden"), 0o755)
  process.env.PATH = bin + ":" + (process.env.PATH ?? "")
  const log = join(dir, "events.log")
  process.env.FAKE_LOG = log
  return { dir, log }
}

function frames(log: string): Frame[] {
  if (!existsSync(log)) return []
  return readFileSync(log, "utf8").split("\n").filter(Boolean).map((line) => JSON.parse(line))
}

async function waitFor(check: () => boolean, what: string): Promise<void> {
  const deadline = Date.now() + 5000
  while (!check()) {
    if (Date.now() > deadline) throw new Error("timed out waiting for " + what)
    await new Promise((resolve) => setTimeout(resolve, 20))
  }
}

// fakeContext records hooks, tools and prompts, and takes the prompt behaviour from the test.
// turns holds the admission id of each user turn OpenCode has made; context lists them.
function fakeContext(prompt: (args: any) => Promise<void>, turns: string[] = [], saved = new Map<string, any>()) {
  const hooks: Record<string, (event: any) => unknown> = {}
  const tools: any[] = []
  const prompts: any[] = []
  return {
    storage: {
      get: async (key: string) => saved.get(key),
      set: async (key: string, value: any) => { saved.set(key, value) },
      remove: async (key: string) => { saved.delete(key) },
    },
    hooks,
    tools,
    prompts,
    turns,
    session: {
      hook: async (name: string, handler: (event: any) => unknown) => {
        hooks[name] = handler
      },
      prompt: async (args: any) => {
        prompts.push(args)
        return prompt(args)
      },
      context: async () => ({ data: turns.map((id) => ({ id, type: "user" })) }),
    },
    tool: {
      transform: async (edit: (editor: { add: (tool: any) => void }) => void) => edit({ add: (tool) => tools.push(tool) }),
    },
  }
}

test("a failed injection is reported, not acknowledged, and redelivered after the next prompt", async () => {
  const { log } = fakeCore()
  process.argv = ["node", "opencode", "serve", "--stdio", "--port", "0"]
  let failNext = true
  const ctx = fakeContext(async () => {
    if (failNext) {
      failNext = false
      throw new Error("session busy")
    }
  })
  const stop = await plugin.setup(ctx)
  try {
    ctx.hooks.prompt({ sessionID: "ses_1", metadata: {} })
    await waitFor(() => frames(log).some((f) => f.method === "notifications/garden/delivery_failed"), "the delivery failure report")
    const failure = frames(log).find((f) => f.method === "notifications/garden/delivery_failed")
    assert.equal(failure?.params?.message_id, "m1")
    assert.match(failure?.params?.reason, /session busy/)
    assert.equal(frames(log).some((f) => f.method === "tools/call" && f.params?.name === "acknowledge_delivery"), false, "a failed injection must not be acknowledged")

    // The instructions replacement reached the plugin before the message did, so the context hook uses it.
    const system: any[] = []
    ctx.hooks.context({ system })
    assert.deepEqual(system, [{ type: "text", text: "replacement instructions" }])

    // The next human prompt re-reports the session; the core redelivers m1 and the plugin acknowledges it.
    ctx.hooks.prompt({ sessionID: "ses_1", metadata: {} })
    await waitFor(() => frames(log).some((f) => f.method === "tools/call" && f.params?.name === "acknowledge_delivery"), "the acknowledgement after redelivery")
    assert.equal(ctx.prompts.length, 2, "the message was injected once per attempt")
    const sessionReports = frames(log).filter((f) => f.method === "notifications/garden/session")
    assert.equal(sessionReports.length, 2, "the session is reported again after the failure")
  } finally {
    stop?.()
  }
})

test("a message with no tracked session is reported as a delivery failure and never injected", async () => {
  const { log } = fakeCore()
  process.argv = ["node", "opencode", "serve", "--stdio", "--port", "0"]
  const ctx = fakeContext(async () => {})
  const stop = await plugin.setup(ctx)
  try {
    const poke = ctx.tools.find((tool) => tool.name === "poke")
    assert.ok(poke, "the core's poke tool is exposed")
    await poke.execute({})
    await waitFor(() => frames(log).some((f) => f.method === "notifications/garden/delivery_failed"), "the no-session failure report")
    const failure = frames(log).find((f) => f.method === "notifications/garden/delivery_failed")
    assert.equal(failure?.params?.message_id, "m0")
    assert.match(failure?.params?.reason, /no session/)
    assert.equal(ctx.prompts.length, 0, "nothing is injected without a tracked session")
    assert.equal(frames(log).some((f) => f.method === "notifications/garden/session"), false, "no session is reported without a human prompt")
    assert.equal(ctx.tools.some((tool) => tool.name === "acknowledge_delivery"), false, "the acknowledgement tool stays hidden from the model")
  } finally {
    stop?.()
  }
})

test("an injection that errors after OpenCode admitted it is acknowledged once, without a second turn", async () => {
  const { log } = fakeCore()
  process.argv = ["node", "opencode", "serve", "--stdio", "--port", "0"]
  const turns: string[] = []
  const ctx = fakeContext(async (args) => {
    if (!turns.includes(args.id)) turns.push(args.id)
    throw new Error("connection reset after admission")
  }, turns)
  const stop = await plugin.setup(ctx)
  try {
    ctx.hooks.prompt({ sessionID: "ses_1", metadata: {} })
    await waitFor(() => frames(log).some((f) => f.method === "tools/call" && f.params?.name === "acknowledge_delivery"), "the acknowledgement")
    assert.equal(frames(log).some((f) => f.method === "notifications/garden/delivery_failed"), false, "an admitted message is delivered, not failed")
    assert.equal(turns.length, 1, "exactly one user turn")
    assert.equal(turns[0], ctx.prompts[0].id)
    assert.match(turns[0], /^msg_[0-9a-f]{12}[0-9A-Za-z]{14}$/)
  } finally {
    stop?.()
  }
})

test("an unknown outcome is not acknowledged, and redelivery with the same id adds no second turn", async () => {
  const { log } = fakeCore()
  process.argv = ["node", "opencode", "serve", "--stdio", "--port", "0"]
  const turns: string[] = []
  let failNext = true
  const ctx = fakeContext(async (args) => {
    if (!turns.includes(args.id)) turns.push(args.id)
    if (failNext) {
      failNext = false
      throw new Error("connection reset")
    }
  }, turns)
  ctx.session.context = async () => {
    throw new Error("lookup unavailable")
  }
  const stop = await plugin.setup(ctx)
  try {
    ctx.hooks.prompt({ sessionID: "ses_1", metadata: {} })
    await waitFor(() => frames(log).some((f) => f.method === "notifications/garden/delivery_failed"), "the uncertain failure report")
    const failure = frames(log).find((f) => f.method === "notifications/garden/delivery_failed")
    assert.match(failure?.params?.reason, /outcome is unknown/)
    assert.equal(frames(log).some((f) => f.method === "tools/call" && f.params?.name === "acknowledge_delivery"), false, "an unknown outcome must not be acknowledged")

    ctx.hooks.prompt({ sessionID: "ses_1", metadata: {} })
    await waitFor(() => frames(log).some((f) => f.method === "tools/call" && f.params?.name === "acknowledge_delivery"), "the acknowledgement after redelivery")
    assert.equal(ctx.prompts.length, 2)
    assert.equal(ctx.prompts[0].id, ctx.prompts[1].id, "redelivery reuses the admission id")
    assert.equal(turns.length, 1, "the same id yields one user turn")
  } finally {
    stop?.()
  }
})

test("an unknown outcome is redelivered to its original session after the human switches sessions", async () => {
  const { log } = fakeCore()
  process.argv = ["node", "opencode", "serve", "--stdio", "--port", "0"]
  const turns: string[] = []
  let failNext = true
  const ctx = fakeContext(async (args) => {
    if (!turns.includes(args.sessionID + "/" + args.id)) turns.push(args.sessionID + "/" + args.id)
    if (failNext) {
      failNext = false
      throw new Error("connection reset")
    }
  }, turns)
  ctx.session.context = async () => {
    throw new Error("lookup unavailable")
  }
  const stop = await plugin.setup(ctx)
  try {
    ctx.hooks.prompt({ sessionID: "ses_A", metadata: {} })
    await waitFor(() => frames(log).some((f) => f.method === "notifications/garden/delivery_failed"), "the uncertain failure report")
    ctx.hooks.prompt({ sessionID: "ses_B", metadata: {} })
    await waitFor(() => frames(log).some((f) => f.method === "tools/call" && f.params?.name === "acknowledge_delivery"), "the acknowledgement after redelivery")
    assert.deepEqual(ctx.prompts.map((p) => p.sessionID), ["ses_A", "ses_A"], "the retry keeps the original target session")
    assert.equal(turns.length, 1, "one user turn, in the original session")
  } finally {
    stop?.()
  }
})

test("a core that stops reading its input only disconnects Garden, without crashing OpenCode", async () => {
  const { log } = fakeCore()
  process.env.FAKE_CLOSE_STDIN = "1"
  process.argv = ["node", "opencode", "serve", "--stdio", "--port", "0"]
  const ctx = fakeContext(async () => {})
  const stop = await plugin.setup(ctx)
  try {
    await new Promise((resolve) => setTimeout(resolve, 300))
    // These writes reach a closed pipe and fail with EPIPE; the plugin must absorb that.
    ctx.hooks.prompt({ sessionID: "ses_1", metadata: {} })
    const status = ctx.tools.find((tool) => tool.name === "status")
    await assert.rejects(async () => {
      for (let i = 0; i < 5; i++) {
        await status.execute({})
        await new Promise((resolve) => setTimeout(resolve, 50))
      }
    })
    assert.ok(frames(log).some((f) => f.method === "tools/list"), "the core started normally first")
  } finally {
    delete process.env.FAKE_CLOSE_STDIN
    stop?.()
  }
})

test("admission ids persist per session and differ between sessions", async () => {
  const { admissionID } = await import("./garden.ts")
  const saved = new Map<string, string>()
  const storage = { get: async (key: string) => saved.get(key), set: async (key: string, id: string) => { saved.set(key, id) } }
  const first = await admissionID(storage, "ses_A", "m1")
  assert.equal(await admissionID(storage, "ses_A", "m1"), first)
  assert.notEqual(await admissionID(storage, "ses_B", "m1"), first)
  assert.match(first, /^msg_[0-9a-f]{12}[0-9A-Za-z]{14}$/)
})

test("a queued admission missing from the context keeps its session and id after the human switches sessions", async () => {
  const { log } = fakeCore()
  process.argv = ["node", "opencode", "serve", "--stdio", "--port", "0"]
  const queued: string[] = []
  let failNext = true
  const ctx = fakeContext(async (args) => {
    // OpenCode queues the admission (not yet in the context's history) and then the call errors.
    if (!queued.includes(args.sessionID + "/" + args.id)) queued.push(args.sessionID + "/" + args.id)
    if (failNext) {
      failNext = false
      throw new Error("connection reset after queueing")
    }
  })
  ;(ctx.session as any).get = async ({ sessionID }: any) => ({ data: { id: sessionID } })
  const stop = await plugin.setup(ctx)
  try {
    ctx.hooks.prompt({ sessionID: "ses_A", metadata: {} })
    await waitFor(() => frames(log).some((f) => f.method === "notifications/garden/delivery_failed"), "the uncertain failure report")
    ctx.hooks.prompt({ sessionID: "ses_B", metadata: {} })
    await waitFor(() => frames(log).some((f) => f.method === "tools/call" && f.params?.name === "acknowledge_delivery"), "the acknowledgement after redelivery")
    assert.deepEqual(ctx.prompts.map((p) => p.sessionID), ["ses_A", "ses_A"], "the retry stays in the session that may hold the queued copy")
    assert.equal(ctx.prompts[0].id, ctx.prompts[1].id, "and reuses its id")
    assert.equal(queued.length, 1, "one admission in total")
  } finally {
    stop?.()
  }
})

test("a delivery whose target session is gone may move to the current session", async () => {
  const { log } = fakeCore()
  process.argv = ["node", "opencode", "serve", "--stdio", "--port", "0"]
  let failNext = true
  const ctx = fakeContext(async () => {
    if (failNext) {
      failNext = false
      throw new Error("session deleted")
    }
  })
  const notFound = Object.assign(new Error(""), { _tag: "Session.NotFoundError", name: "Session.NotFoundError" })
  ctx.session.context = async () => {
    throw notFound
  }
  ;(ctx.session as any).get = async () => {
    throw notFound
  }
  const stop = await plugin.setup(ctx)
  try {
    ctx.hooks.prompt({ sessionID: "ses_A", metadata: {} })
    await waitFor(() => frames(log).some((f) => f.method === "notifications/garden/delivery_failed"), "the failure report")
    const failure = frames(log).find((f) => f.method === "notifications/garden/delivery_failed")
    assert.match(failure?.params?.reason, /no longer exists/)
    ctx.hooks.prompt({ sessionID: "ses_B", metadata: {} })
    await waitFor(() => frames(log).some((f) => f.method === "tools/call" && f.params?.name === "acknowledge_delivery"), "the acknowledgement in the new session")
    assert.deepEqual(ctx.prompts.map((p) => p.sessionID), ["ses_A", "ses_B"])
  } finally {
    stop?.()
  }
})

test("only Session.NotFoundError releases the pin; other lookup results keep the session and id", async () => {
  const { admittedForTest } = await import("./garden.ts")
  const notFound = Object.assign(new Error(""), { _tag: "Session.NotFoundError" })
  const schema = Object.assign(new Error('Expected a string starting with "ses"'), { name: "SchemaError" })
  const cases: Array<[string, any, any, string]> = [
    ["present in context", async () => [{ id: "msg_x" }], async () => ({ id: "ses_A" }), "yes"],
    ["absent, session exists", async () => [], async () => ({ id: "ses_A" }), "unknown"],
    ["get resolves undefined", async () => [], async () => undefined, "unknown"],
    ["get resolves {data: undefined}", async () => [], async () => ({ data: undefined }), "unknown"],
    ["get resolves {data: null, error}", async () => [], async () => ({ data: null, error: { message: "x" } }), "unknown"],
    ["get resolves another id", async () => [], async () => ({ id: "ses_other" }), "unknown"],
    ["schema error", async () => { throw schema }, async () => { throw schema }, "unknown"],
    ["transient error", async () => { throw new Error("ECONNRESET") }, async () => { throw new Error("ECONNRESET") }, "unknown"],
    ["context not found", async () => { throw notFound }, async () => ({ id: "ses_A" }), "gone"],
    ["get not found", async () => [], async () => { throw notFound }, "gone"],
  ]
  for (const [name, context, get, want] of cases) {
    const got = await admittedForTest({ session: { context, get } }, "ses_A", "msg_x")
    assert.equal(got, want, name)
  }
})

// Native ID timestamp/counter encoding from the shipped OpenCode 2.0.32 allocator.
function nativeID(now: number): string {
  return "msg_" + ((BigInt(now) * 0x1000n + 1n) & 0xffffffffffffn).toString(16).padStart(12, "0") + "00000000000000"
}

test("reverting native B keeps earlier Garden G and restart replay reuses G", async () => {
  const saved = new Map<string, string>()
  const turns = [nativeID(Date.now())]
  const nativeA = turns[0]
  let { log } = fakeCore()
  process.argv = ["node", "opencode", "serve", "--stdio", "--port", "0"]
  const ctx = fakeContext(async (args) => {
    if (turns.includes(args.id)) return
    await ctx.hooks.prompt({ sessionID: args.sessionID, messageID: args.id, metadata: args.metadata })
    turns.push(args.id)
  }, turns, saved)
  const stop = await plugin.setup(ctx)
  ctx.hooks.prompt({ sessionID: "ses_A", metadata: {} })
  await waitFor(() => frames(log).some((f) => f.params?.name === "acknowledge_delivery"), "first acknowledgement")
  stop?.()
  const gardenG = ctx.prompts[0].id
  const nativeB = nativeID(Date.now())
  turns.push(nativeB)
  assert.ok(nativeA < gardenG && gardenG < nativeB, "native A < Garden G < native B")
  // OpenCode's TUI preview and committed transcript truncate by lexical ID.
  assert.deepEqual(turns.filter((id) => id < nativeB), [nativeA, gardenG])
  const cut = turns.findIndex((id) => id >= nativeB)
  assert.deepEqual(turns.slice(0, cut), [nativeA, gardenG])

  // Recreate the plugin against the same durable storage, at a later time.
  ;({ log } = fakeCore())
  const replay = fakeContext(async (args) => {
    if (turns.includes(args.id)) return
    await replay.hooks.prompt({ sessionID: args.sessionID, messageID: args.id, metadata: args.metadata })
    turns.push(args.id)
  }, turns, saved)
  const stopReplay = await plugin.setup(replay)
  try {
    replay.hooks.prompt({ sessionID: "ses_A", metadata: {} })
    await waitFor(() => frames(log).some((f) => f.params?.name === "acknowledge_delivery"), "replay acknowledgement")
    assert.equal(replay.prompts[0].id, gardenG)
    assert.equal(turns.length, 3, "replay adds no new turn")
  } finally { stopReplay?.() }
})

test("a crash after persisting G but before submission reallocates G after native A", async () => {
  const saved = new Map<string, any>()
  let { log } = fakeCore()
  process.argv = ["node", "opencode", "serve", "--stdio", "--port", "0"]
  const first = fakeContext(async () => assert.fail("the crashed attempt must not submit"), [], saved)
  const persist = first.storage.set
  first.storage.set = async (key, value) => {
    await persist(key, value)
    if (key.startsWith("admission:")) throw new Error("crash after the storage commit")
  }
  const stop = await plugin.setup(first)
  try {
    first.hooks.prompt({ sessionID: "ses_A", metadata: {} })
    await waitFor(() => frames(log).some((f) => f.method === "notifications/garden/delivery_failed"), "the interrupted submission")
    assert.equal(first.prompts.length, 0)
  } finally { stop?.() }
  const reservedG = [...saved.entries()].find(([key]) => key.startsWith("admission:"))?.[1]
  assert.ok(reservedG)
  const nativeA = nativeID(Date.now())
  assert.ok(reservedG < nativeA, "the reserved ID predates native A")
  const turns = [nativeA]

  ;({ log } = fakeCore())
  const replay = fakeContext(async (args) => {
    // OpenCode 2.0.32 reconciles durable IDs before running the prompt hook.
    if (turns.includes(args.id)) return
    await replay.hooks.prompt({ sessionID: args.sessionID, messageID: args.id, metadata: args.metadata })
    turns.push(args.id)
  }, turns, saved)
  const stopReplay = await plugin.setup(replay)
  try {
    replay.hooks.prompt({ sessionID: "ses_A", metadata: {} })
    await waitFor(() => frames(log).some((f) => f.params?.name === "acknowledge_delivery"), "the replay acknowledgement")
    const gardenG = turns[1]
    assert.ok(nativeA < gardenG, "first admission must sort after native A")
    assert.notEqual(gardenG, reservedG)
    assert.ok([...saved.values()].includes(gardenG), "the replacement is durable")
    assert.deepEqual(turns.filter((id) => id < gardenG), [nativeA], "revert preview keeps A")
    assert.deepEqual(turns.slice(0, turns.findIndex((id) => id >= gardenG)), [nativeA], "committing the revert keeps A")
  } finally { stopReplay?.() }
})

test("mixed-era transcripts still truncate at old hash IDs and require fresh sessions", async () => {
  const { admissionID } = await import("./garden.ts")
  const nativeA = nativeID(Date.now() - 2)
  const legacyG = "msg_" + createHash("sha256").update("ses_A\x00legacy").digest("hex").slice(0, 28)
  const nativeB = nativeID(Date.now() - 1)
  const saved = new Map<string, string>()
  const storage = { get: async (key: string) => saved.get(key), set: async (key: string, id: string) => { saved.set(key, id) } }
  const correctedG = await admissionID(storage, "ses_A", "new")
  const turns = [nativeA, legacyG, nativeB, correctedG]
  assert.ok(nativeA < nativeB && nativeB < correctedG && nativeB < legacyG)
  const cut = turns.findIndex((id) => id >= nativeB)
  assert.deepEqual(turns.slice(0, cut), [nativeA], "reverting B also hides the subsequent corrected Garden turn")
  assert.ok(cut < turns.indexOf(nativeB), "the old hash ID starts the truncation too early")
  const readme = readFileSync(new URL("./README.md", import.meta.url), "utf8")
  assert.match(readme, /start fresh OpenCode sessions/i)
  assert.match(readme, /do not resume[^.]*hash-derived Garden IDs/i)
  assert.match(readme, /later native[^.]*Garden messages/i)
})

test("a native prompt during admission storage forces a fresh Garden ID", async () => {
  const { log } = fakeCore()
  process.argv = ["node", "opencode", "serve", "--stdio", "--port", "0"]
  const turns: string[] = []
  const ctx = fakeContext(async (args) => {
    if (turns.includes(args.id)) return
    await ctx.hooks.prompt({ sessionID: args.sessionID, messageID: args.id, metadata: args.metadata })
    turns.push(args.id)
  }, turns)
  const persist = ctx.storage.set
  let delayed = false
  ctx.storage.set = async (key, value) => {
    await persist(key, value)
    if (!key.startsWith("admission:") || delayed) return
    delayed = true
    await new Promise((resolve) => setTimeout(resolve, 2))
    const nativeA = nativeID(Date.now())
    turns.push(nativeA)
    ctx.hooks.prompt({ sessionID: "ses_A", messageID: nativeA, metadata: {} })
  }
  const stop = await plugin.setup(ctx)
  try {
    ctx.hooks.prompt({ sessionID: "ses_A", metadata: {} })
    await waitFor(() => frames(log).some((f) => f.params?.name === "acknowledge_delivery"), "the fresh admission")
    assert.equal(turns.length, 2)
    assert.ok(turns[0] < turns[1], "first admission must follow the native turn during storage")
    assert.deepEqual(turns.slice(0, turns.findIndex((id) => id >= turns[1])), [turns[0]])
  } finally { stop?.() }
})

test("replacement storage failure leaves the unadmitted replay pending", async () => {
  const { admissionID } = await import("./garden.ts")
  const saved = new Map<string, any>()
  const storage = { get: async (key: string) => saved.get(key), set: async (key: string, id: string) => { saved.set(key, id) } }
  const reservedG = await admissionID(storage, "ses_A", "m1")
  const turns = [nativeID(Date.now())]
  const { log } = fakeCore()
  process.argv = ["node", "opencode", "serve", "--stdio", "--port", "0"]
  const ctx = fakeContext(async (args) => {
    if (turns.includes(args.id)) return
    await ctx.hooks.prompt({ sessionID: args.sessionID, messageID: args.id, metadata: args.metadata })
    turns.push(args.id)
  }, turns, saved)
  const persist = ctx.storage.set
  ctx.storage.set = async (key, value) => {
    if (key.startsWith("admission:")) throw new Error("disk full during replacement")
    await persist(key, value)
  }
  const stop = await plugin.setup(ctx)
  try {
    ctx.hooks.prompt({ sessionID: "ses_A", metadata: {} })
    await waitFor(() => frames(log).some((f) => f.method === "notifications/garden/delivery_failed"), "the replacement storage failure")
    assert.deepEqual(ctx.prompts.map((p) => p.id), [reservedG], "only the aborted reconciliation probe was called")
    assert.equal(turns.length, 1, "no Garden turn was admitted")
    assert.equal(frames(log).some((f) => f.params?.name === "acknowledge_delivery"), false)
    assert.ok([...saved.values()].includes(reservedG), "the original mapping survives")
  } finally { stop?.() }
})

test("storage failure prevents prompt submission and acknowledgement", async () => {
  const { log } = fakeCore()
  process.argv = ["node", "opencode", "serve", "--stdio", "--port", "0"]
  const ctx = fakeContext(async () => {})
  ctx.storage.set = async () => { throw new Error("disk full") }
  const stop = await plugin.setup(ctx)
  try {
    ctx.hooks.prompt({ sessionID: "ses_A", metadata: {} })
    await waitFor(() => frames(log).some((f) => f.method === "notifications/garden/delivery_failed"), "storage failure")
    assert.equal(ctx.prompts.length, 0)
    assert.equal(frames(log).some((f) => f.params?.name === "acknowledge_delivery"), false)
  } finally { stop?.() }
})

test("restart and session switch keep an uncertain queued delivery in its original session", async () => {
  const saved = new Map<string, any>()
  const queued: string[] = []
  let { log } = fakeCore()
  process.argv = ["node", "opencode", "serve", "--stdio", "--port", "0"]
  const first = fakeContext(async ({ sessionID, id }) => {
    await first.hooks.prompt({ sessionID, messageID: id, metadata: { source: "garden" } })
    queued.push(sessionID + "/" + id)
    throw new Error("connection reset after queue admission")
  }, [], saved)
  ;(first.session as any).get = async ({ sessionID }: any) => ({ id: sessionID })
  const stop = await plugin.setup(first)
  first.hooks.prompt({ sessionID: "ses_A", metadata: {} })
  await waitFor(() => frames(log).some((f) => f.method === "notifications/garden/delivery_failed"), "uncertain delivery")
  stop?.()

  ;({ log } = fakeCore())
  const replay = fakeContext(async ({ sessionID, id }) => {
    const key = sessionID + "/" + id
    if (queued.includes(key)) return
    await replay.hooks.prompt({ sessionID, messageID: id, metadata: { source: "garden" } })
    queued.push(key)
  }, [], saved)
  const stopReplay = await plugin.setup(replay)
  try {
    replay.hooks.prompt({ sessionID: "ses_B", metadata: {} })
    await waitFor(() => frames(log).some((f) => f.params?.name === "acknowledge_delivery"), "replay acknowledgement")
    assert.equal(replay.prompts[0].sessionID, "ses_A")
    assert.equal(replay.prompts[0].id, first.prompts[0].id)
    assert.equal(queued.length, 1, "one queued admission survives restart")
  } finally { stopReplay?.() }
})

test("one Garden message to two recipients keeps separate durable targets", async () => {
  const saved = new Map<string, any>()
  const ids: string[] = []
  for (const [recipient, sessionID] of [["agentA", "ses_A"], ["agentB", "ses_B"]]) {
    const { log } = fakeCore(recipient)
    process.argv = ["node", "opencode", "serve", "--stdio", "--port", "0"]
    const ctx = fakeContext(async () => {}, [], saved)
    const stop = await plugin.setup(ctx)
    try {
      ctx.hooks.prompt({ sessionID, metadata: {} })
      await waitFor(() => frames(log).some((f) => f.params?.name === "acknowledge_delivery"), "recipient acknowledgement")
      assert.equal(ctx.prompts[0].sessionID, sessionID)
      ids.push(ctx.prompts[0].id)
    } finally { stop?.() }
  }
  assert.notEqual(ids[0], ids[1], "separate sessions cannot share an OpenCode admission ID")
})
