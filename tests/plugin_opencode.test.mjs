// Smoke test for the opencode V2 ajq plugin.
//
// Drives the plugin through a fake `ctx` that mimics the V2 plugin API
// (tool.hook / tool.transform / session.hook / session.get) and a stub `ajq`
// binary, so no real daemon or harness is involved.
//
//   node --test tests/plugin_opencode.test.mjs

import { test } from "node:test";
import assert from "node:assert/strict";
import { mkdtempSync, writeFileSync, chmodSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { pathToFileURL } from "node:url";

const PLUGIN = new URL("../harnesses/opencode/ajq.js", import.meta.url);

// A stub `ajq` whose behaviour is steered by env vars:
//   STUB_MODE=warn|block   -> `ajq config`
//   STUB_DAEMON=up|down    -> `ajq daemon status`
//   STUB_LIST=<json>       -> `ajq list --json`
function makeStub() {
  const dir = mkdtempSync(join(tmpdir(), "ajq-stub-"));
  const bin = join(dir, "ajq");
  writeFileSync(
    bin,
    `#!/usr/bin/env bash
cmd="\${1:-}"; shift || true
case "$cmd" in
  config)
    if [ "\${1:-}" = "--json" ]; then
      printf '{"hooks":{"guard_mode":"%s"}}\\n' "\${STUB_MODE:-warn}"
    else
      printf 'hooks.guard_mode   %s\\n' "\${STUB_MODE:-warn}"
    fi ;;
  guard)
    shift || true
    if printf '%s' "$*" | grep -qE 'npm test|pytest|heavy'; then
      printf 'heavy: true\\nkind: test\\n'
    else
      printf 'heavy: false\\nkind: unknown\\n'
    fi ;;
  daemon)
    if [ "\${1:-}" = "status" ]; then
      if [ "\${STUB_DAEMON:-up}" = "up" ]; then printf 'running\\n'; exit 0; else exit 1; fi
    fi ;;
  list)
    [ -n "\${STUB_LIST:-}" ] && printf '%s\\n' "\${STUB_LIST}" ;;
  submit) printf 'j-test123\\n' ;;
  status) printf '{"state":"running"}\\n' ;;
  output) printf 'some log output\\n' ;;
esac
exit 0
`,
  );
  chmodSync(bin, 0o755);
  return bin;
}

const STUB = makeStub();

function makeCtx() {
  const hooks = {};
  const tools = [];
  return {
    hooks,
    tools,
    ctx: {
      location: { directory: "/plugin/location" },
      tool: {
        hook: async (name, cb) => {
          hooks[name] = cb;
          return { dispose() {} };
        },
        transform: async (cb) => {
          cb({
            add: (t) => tools.push(t),
            list: () => tools,
            get: () => undefined,
            namespace() {},
            update() {},
            remove() {},
          });
          return { dispose() {} };
        },
      },
      session: {
        hook: async (name, cb) => {
          hooks[`session:${name}`] = cb;
          return { dispose() {} };
        },
        get: async ({ sessionID }) => ({ id: sessionID, location: { directory: "/session/dir" } }),
      },
    },
  };
}

// Each import re-runs setup(), which reads the guard mode.
async function load(mode, extraEnv = {}) {
  process.env.AJQ_BIN = STUB;
  process.env.STUB_MODE = mode;
  delete process.env.STUB_DAEMON;
  delete process.env.STUB_LIST;
  Object.assign(process.env, extraEnv);
  const url = `${pathToFileURL(PLUGIN.pathname).href}?t=${Math.random()}`;
  const mod = await import(url);
  const fake = makeCtx();
  await mod.default.setup(fake.ctx);
  return fake;
}

const heavyResult = () => ({ content: [{ type: "text", text: "boom" }], metadata: {} });
const heavyEvent = () => ({ tool: "shell", input: { command: "npm test" }, status: "completed", result: heavyResult() });

test("setup registers the native tools", async () => {
  const { tools } = await load("warn");
  assert.deepEqual(
    tools.map((t) => t.name).sort(),
    ["ajq_status", "ajq_submit"],
  );
});

test("warn appends a nudge to a heavy shell result", async () => {
  const { hooks } = await load("warn");
  const event = heavyEvent();
  await hooks["execute.after"](event);
  const texts = event.result.content.map((c) => c.text).join("\n");
  assert.match(texts, /classified test \(heavy\)/);
  assert.match(texts, /ajq submit/);
});

test("warn leaves light commands untouched", async () => {
  const { hooks } = await load("warn");
  const event = { tool: "shell", input: { command: "git status" }, status: "completed", result: heavyResult() };
  const before = JSON.stringify(event.result);
  await hooks["execute.after"](event);
  assert.equal(JSON.stringify(event.result), before);
});

test("warn ignores commands that already use ajq", async () => {
  const { hooks } = await load("warn");
  const event = { tool: "shell", input: { command: "ajq status j-1" }, status: "completed", result: heavyResult() };
  const before = JSON.stringify(event.result);
  await hooks["execute.after"](event);
  assert.equal(JSON.stringify(event.result), before);
});

test("block denies a heavy shell command", async () => {
  const { hooks } = await load("block");
  await assert.rejects(
    () => Promise.resolve(hooks["execute.before"]({ tool: "shell", input: { command: "npm test" } })),
    /Blocked by ajq .*classified test \(heavy\)/,
  );
});

test("block allows light commands", async () => {
  const { hooks } = await load("block");
  await hooks["execute.before"]({ tool: "shell", input: { command: "git status" } });
});

test("block never denies when the daemon is down", async () => {
  const { hooks } = await load("block", { STUB_DAEMON: "down" });
  await hooks["execute.before"]({ tool: "shell", input: { command: "npm test" } });
});

test("the V1 tool name 'bash' is still accepted", async () => {
  const { hooks } = await load("warn");
  const event = { tool: "bash", input: { command: "npm test" }, status: "completed", result: heavyResult() };
  await hooks["execute.after"](event);
  assert.match(event.result.content.map((c) => c.text).join("\n"), /ajq submit/);
});

test("ajq_submit runs submit with the session directory", async () => {
  const { tools } = await load("warn");
  const submit = tools.find((t) => t.name === "ajq_submit");
  const out = await submit.execute({ command: "npm test" }, { sessionID: "ses_x" });
  assert.match(out.content, /j-test123/);
});

test("ajq_status reads state and output", async () => {
  const { tools } = await load("warn");
  const status = tools.find((t) => t.name === "ajq_status");
  const out = await status.execute({ id: "j-test123" }, {});
  assert.match(out.content, /state running/);
  assert.match(out.content, /some log output/);
});

test("the queue snapshot is injected once", async () => {
  const { hooks } = await load("warn", { STUB_LIST: '{"count": 1, "jobs": [{"state": "running"}]}' });
  const contextHook = hooks["session:context"];
  const first = { system: [] };
  await contextHook(first);
  assert.equal(first.system.length, 1);
  assert.match(first.system[0].text, /1 running, 0 queued/);
  const second = { system: [] };
  await contextHook(second);
  assert.equal(second.system.length, 0);
});
