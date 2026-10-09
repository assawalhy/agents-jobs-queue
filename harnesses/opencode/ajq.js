// ajq — jobs-queue integration for opencode v2 (verified on v2.0.26).
//
// Loaded from ~/.config/opencode/plugins/ajq.js. V2 calls the default export's
// `setup(ctx)` and ignores a V1 `server()` export, so all logic lives in setup.
// Four jobs:
//   1. make sure ajqd is up, once per process, and inject the queue snapshot
//   2. on `execute.after`, when a heavy shell command ran and hooks.guard_mode is
//      "warn", append a note pointing the model at `ajq submit`
//   3. on `execute.before`, when hooks.guard_mode is "block", deny a heavy shell
//      command with a reason naming `ajq submit`
//   4. register `ajq_submit` / `ajq_status` / `ajq_output` / `ajq_wait` so the
//      model can queue, check, read and wait for work without a shell
//
// The V2 shell tool is named "shell" (V1 called it "bash"); both are accepted.
// Classification stays in Python (`ajq guard --explain`) — one source of truth.
//
// Every registration and callback is wrapped: a plugin that throws wedges the
// host session, so failures degrade quietly. The one deliberate throw is the
// `block` deny, which is the hook's whole purpose.
//
// $AJQ_BIN overrides the binary; otherwise `ajq` from PATH.

import { execFile } from "node:child_process";

const CLI_MS = 15000;
const ENSURE_MS = 4000;
const MAX_BUFFER = 4 * 1024 * 1024;

// V2 renamed the shell tool from "bash" to "shell"; accept both so a version
// bump in either direction keeps the guard alive.
const SHELL_TOOLS = new Set(["shell", "bash"]);

function ajqBin() {
  const fromEnv = process.env.AJQ_BIN;
  if (typeof fromEnv === "string" && fromEnv.trim() !== "") return fromEnv.trim();
  return "ajq";
}

function text(value) {
  return typeof value === "string" ? value.trim() : "";
}

// Always resolves ("" on any failure); never rejects, so callers need no catch.
function spawn(args, ms) {
  return new Promise((resolve) => {
    let settled = false;
    const done = (value) => {
      if (settled) return;
      settled = true;
      resolve(value);
    };
    const timer = setTimeout(() => done(""), ms);
    if (typeof timer === "object" && timer && typeof timer.unref === "function") timer.unref();
    try {
      execFile(ajqBin(), args, { timeout: ms, maxBuffer: MAX_BUFFER, encoding: "utf8" }, (err, stdout) => {
        done(err ? "" : String(stdout));
      });
    } catch {
      done("");
    }
  });
}

// Like spawn(), but reports only whether the command exited 0. `ajq daemon
// status` logs its line to stderr and exits 0 running / 1 down, so the exit
// code — not stdout — is the reachability signal.
function spawnOk(args, ms) {
  return new Promise((resolve) => {
    let settled = false;
    const done = (value) => {
      if (settled) return;
      settled = true;
      resolve(value);
    };
    const timer = setTimeout(() => done(false), ms);
    if (typeof timer === "object" && timer && typeof timer.unref === "function") timer.unref();
    try {
      execFile(ajqBin(), args, { timeout: ms, maxBuffer: MAX_BUFFER, encoding: "utf8" }, (err) => done(!err));
    } catch {
      done(false);
    }
  });
}

// Like spawn(), but keeps stdout even when the command exits non-zero — `ajq
// wait` exits 2 for any non-`done` state and still prints the state and log.
function spawnText(args, ms) {
  return new Promise((resolve) => {
    let settled = false;
    const done = (value) => {
      if (settled) return;
      settled = true;
      resolve(value);
    };
    const timer = setTimeout(() => done(""), ms);
    if (typeof timer === "object" && timer && typeof timer.unref === "function") timer.unref();
    try {
      execFile(ajqBin(), args, { timeout: ms, maxBuffer: MAX_BUFFER, encoding: "utf8" }, (_err, stdout) => {
        done(typeof stdout === "string" ? stdout : "");
      });
    } catch {
      done("");
    }
  });
}

// `ajq list --json` is `{"count": N, "jobs": [ ... ]}`; each job carries exactly
// one `"state": "..."` pair, so a substring count is enough.
function countStates(listing, wanted) {
  const needle = `"state": "${wanted}"`;
  let count = 0;
  let at = listing.indexOf(needle);
  while (at !== -1) {
    count += 1;
    at = listing.indexOf(needle, at + needle.length);
  }
  return count;
}

function field(block, name) {
  const prefix = `${name}:`;
  const row = block.split("\n").find((line) => line.startsWith(prefix));
  return row ? row.slice(prefix.length).trim() : "";
}

// `ajq config` prints `hooks.guard_mode   <mode>`; `ajq config --json` prints the
// merged config. Prefer the labelled line, fall back to a JSON scan.
async function readGuardMode() {
  const human = await spawn(["config"], CLI_MS);
  const mode = text(field(human, "hooks.guard_mode")).split(/\s+/)[0];
  if (mode === "off" || mode === "warn" || mode === "block") return mode;
  const json = await spawn(["config", "--json"], CLI_MS);
  const hit = json.match(/"guard_mode"\s*:\s*"(off|warn|block)"/);
  return hit ? hit[1] : "warn";
}

// guard_mode is read lazily and memoized briefly, not captured at setup: an
// opencode reload does not re-run setup for an unchanged plugin file, so a
// setup-time read would ignore a config change until a full restart.
const MODE_TTL_MS = 10000;
let modeCache = { value: "", at: 0 };

// The queue snapshot is injected once per process, not once per setup, since
// setup re-runs on reload.
let snapshotDone = false;

async function guardMode() {
  const now = Date.now();
  if (modeCache.value !== "" && now - modeCache.at < MODE_TTL_MS) return modeCache.value;
  const value = await readGuardMode();
  modeCache = { value, at: now };
  return value;
}

async function classify(command) {
  const verdict = await spawn(["guard", "--explain", command], CLI_MS);
  if (field(verdict, "heavy") !== "true") return null;
  return { kind: field(verdict, "kind") || "unknown" };
}

function isShellTool(tool) {
  return SHELL_TOOLS.has(text(tool));
}

function commandOf(event) {
  return text(event && event.input && event.input.command);
}

// A shell-ish command keeps its metacharacters, so the suggestion needs
// `--shell` and a quoted argument; otherwise the agent's own shell splits it.
function submitSuggestion(command) {
  if (/[|&;<>`]|\$\(/.test(command)) {
    return `ajq submit --shell -- "${command.replace(/"/g, '\\"')}"`;
  }
  return `ajq submit -- ${command}`;
}

function blockMessage(command, kind) {
  return (
    `Blocked by ajq (hooks.guard_mode=block): '${command}' is classified ${kind} (heavy). ` +
    `Submit it instead with the ajq_submit tool, or: ${submitSuggestion(command)} — then wait for it ` +
    `with the ajq_wait tool (or a background 'ajq wait <id> --tail 80').`
  );
}

function warnNote(command, kind) {
  return (
    `ajq: '${command}' is classified ${kind} (heavy) and is expected to be slow. ` +
    `Next time queue it instead: ${submitSuggestion(command)} — then wait for it with the ajq_wait tool ` +
    `(or a background 'ajq wait <id> --tail 80'). Do not sleep or poll.`
  );
}

// Append a note to a completed tool result. Reassigns `content` rather than
// mutating in place, matching the documented hook contract.
function withNote(result, note) {
  if (!result || typeof result !== "object") return result;
  const content = Array.isArray(result.content)
    ? [...result.content, { type: "text", text: `\n${note}` }]
    : typeof result.content === "string"
      ? [{ type: "text", text: `${result.content}\n${note}` }]
      : [{ type: "text", text: note }];
  return { ...result, content };
}

const SUBMIT_DESCRIPTION =
  "Queue a heavy command (build, test, typecheck, lint, install, docker build, or anything " +
  "expected to take more than ~30s) on the ajq jobs daemon instead of running it in the shell. " +
  "Call this whenever the next step would otherwise be a slow bash command. A cheap job is waited " +
  "out and returns its state and log in this same call; anything longer returns the job id to " +
  "follow with ajq_wait (or a background `ajq wait`).";

const STATUS_DESCRIPTION =
  "Read an ajq job's state only (queued/running/done/failed/timeout/canceled/lost). It does not " +
  "return the log: use ajq_output to read the log, or ajq_wait to block until the job finishes " +
  "and get the log in one call. Never poll it in a loop.";

const OUTPUT_DESCRIPTION =
  "Read the tail of an ajq job's captured output. Use it once ajq_status shows a terminal state; " +
  "to wait for that and get the log in one call, use ajq_wait instead.";

const WAIT_DESCRIPTION =
  "Wait for an ajq job to finish and return its final state plus the tail of its output in one " +
  "call. Runs in-process, so it is not the shell's 120s timeout. Call this instead of polling " +
  "ajq_status or sleeping. For a job that may outlast a turn, run `ajq wait <id> --tail N` as a " +
  "background shell task instead, so the session stays interactive.";

// A runaway `tail` (a model can pass one while trying to wait) must never read
// the whole log; clamp to a sane number of lines.
const MAX_TAIL = 1000;

function clampTail(value, fallback) {
  const n = Number(value);
  if (!Number.isFinite(n)) return fallback;
  return Math.min(MAX_TAIL, Math.max(0, Math.trunc(n)));
}

// The session directory is what the job must run in; the tool executor context
// carries no cwd on V2, so read it from Session.Info.location, then ctx.location.
async function sessionDirectory(ctx, context) {
  const sessionID = text(context && context.sessionID);
  if (sessionID !== "" && ctx && ctx.session && typeof ctx.session.get === "function") {
    try {
      const info = await ctx.session.get({ sessionID });
      const dir = text(info && info.location && info.location.directory);
      if (dir !== "") return dir;
    } catch {
      /* fall through to the plugin location */
    }
  }
  return text(ctx && ctx.location && ctx.location.directory);
}

// Seconds `ajq_submit` will block for a cheap job before handing the id back.
// Long enough to cover a lint/format/check, short enough that a real build never
// sits inside a blocking tool call. `wait_s: 0` opts out.
const SUBMIT_WAIT_S = 20;
const SUBMIT_WAIT_MAX_S = 120;

function clampWaitS(value) {
  const raw = Number(value);
  if (!Number.isFinite(raw) || raw <= 0) return value === 0 || raw === 0 ? 0 : SUBMIT_WAIT_S;
  return Math.min(Math.trunc(raw), SUBMIT_WAIT_MAX_S);
}

async function runSubmit(ctx, args, context) {
  try {
    const command = text(args.command);
    if (command === "") return { content: "ajq_submit: command is required" };
    const cliArgs = ["--json", "submit"];
    if (text(args.label) !== "") cliArgs.push("--label", text(args.label));
    if (text(args.kind) !== "") cliArgs.push("--kind", text(args.kind));
    if (Number(args.timeout) > 0) cliArgs.push("--timeout", String(Math.trunc(Number(args.timeout))));
    if (Number.isFinite(Number(args.priority)) && Number(args.priority) !== 0) {
      cliArgs.push("--priority", String(Math.trunc(Number(args.priority))));
    }
    const cwd = text(args.cwd) || (await sessionDirectory(ctx, context));
    if (cwd !== "") cliArgs.push("--cwd", cwd);
    // --shell hands the single string to $SHELL in the daemon, so pipes, &&,
    // globs and env prefixes behave the way the model wrote them.
    cliArgs.push("--shell", "--", command);
    const waitS = clampWaitS(args.wait_s);
    // JSON carries the id and the daemon's estimate, so the id never has to be
    // scraped back out of the human line and the wait can be gated on it.
    const raw = text(await spawn(cliArgs, CLI_MS));
    let job = null;
    try {
      job = JSON.parse(raw);
    } catch {
      job = null;
    }
    if (!job || !job.id) {
      // The job is already queued; never submit twice. Hand back whatever the
      // CLI printed so the id can still be read out of it.
      return { content: raw === "" ? `ajq_submit: no output from ajq for ${command}` : raw };
    }
    const etaTotal = Number(job.eta_start_s) + Number(job.eta_run_s);
    if (waitS > 0 && Number.isFinite(etaTotal) && etaTotal <= waitS) {
      const waited = text(
        await spawnText(["wait", String(job.id), "--tail", "40", "--timeout", String(waitS)], (waitS + 15) * 1000),
      );
      // `ajq wait` already leads with the id; do not print it twice.
      if (waited !== "") {
        return { content: waited.startsWith(String(job.id)) ? waited : `${job.id}  ${waited}` };
      }
    }
    return { content: `${job.id}  state ${text(job.state)}  pool ${text(job.pool)}  eta_run ${Math.round(Number(job.eta_run_s))}s` };
  } catch (err) {
    return { content: `ajq_submit failed: ${err && err.message ? err.message : String(err)}` };
  }
}

async function runStatus(args) {
  try {
    const id = text(args.id);
    if (id === "") return { content: "ajq_status: id is required" };
    const meta = await spawn(["status", id, "--json"], CLI_MS);
    let job = null;
    try {
      job = JSON.parse(meta);
    } catch {
      job = null;
    }
    if (!job || !job.state) return { content: `ajq_status: no such job ${id}` };
    const parts = [`state ${text(job.state)}`];
    if (job.queue_position) parts.push(`position ${job.queue_position}`);
    if (job.exit_code !== null && job.exit_code !== undefined) parts.push(`exit ${job.exit_code}`);
    if (text(job.kill_reason) !== "") parts.push(`killed ${text(job.kill_reason)}`);
    return { content: parts.join("  ") };
  } catch (err) {
    return { content: `ajq_status failed: ${err && err.message ? err.message : String(err)}` };
  }
}

async function runOutput(args) {
  try {
    const id = text(args.id);
    if (id === "") return { content: "ajq_output: id is required" };
    const cliArgs = ["output", id, "--tail", String(clampTail(args.tail, 40))];
    if (args.from_start === true) cliArgs.push("--from-start");
    const out = text(await spawnText(cliArgs, CLI_MS));
    return { content: out === "" ? `ajq_output: ${id} has no output` : out };
  } catch (err) {
    return { content: `ajq_output failed: ${err && err.message ? err.message : String(err)}` };
  }
}

async function runWait(args) {
  try {
    const id = text(args.id);
    if (id === "") return { content: "ajq_wait: id is required" };
    const tail = clampTail(args.tail, 40);
    const timeout = Number(args.timeout) > 0 ? Math.trunc(Number(args.timeout)) : 120;
    const out = text(
      await spawnText(["wait", id, "--tail", String(tail), "--timeout", String(timeout)], (timeout + 15) * 1000),
    );
    return { content: out === "" ? `ajq_wait: no output for ${id}` : out };
  } catch (err) {
    return { content: `ajq_wait failed: ${err && err.message ? err.message : String(err)}` };
  }
}

function submitInput() {
  return {
    type: "object",
    properties: {
      command: { type: "string", description: "Command to run, as a single shell-style string." },
      cwd: { type: "string", description: "Working directory; defaults to the session directory." },
      kind: {
        type: "string",
        description: "build | test | typecheck | lint | format | install | docker | check. Omit for auto.",
      },
      timeout: { type: "number", description: "Seconds before the job is killed. Omit for the config default." },
      wait_s: {
        type: "number",
        description:
          "Seconds to wait for a cheap job and return its state and log in this same call " +
          `(default ${SUBMIT_WAIT_S}, max ${SUBMIT_WAIT_MAX_S}, 0 to return the id immediately).`,
      },
      label: { type: "string", description: "Short name shown by `ajq list`." },
      priority: { type: "number", description: "Higher runs first inside a pool." },
    },
    required: ["command"],
    additionalProperties: false,
  };
}

function statusInput() {
  return {
    type: "object",
    properties: {
      id: { type: "string", description: "Job id, e.g. j-1a2b3c4d5e6f." },
    },
    required: ["id"],
    additionalProperties: false,
  };
}

function outputInput() {
  return {
    type: "object",
    properties: {
      id: { type: "string", description: "Job id, e.g. j-1a2b3c4d5e6f." },
      tail: { type: "number", description: "Trailing log lines to return; omit for 40." },
      from_start: { type: "boolean", description: "Return the whole log, not just the tail." },
    },
    required: ["id"],
    additionalProperties: false,
  };
}

function waitInput() {
  return {
    type: "object",
    properties: {
      id: { type: "string", description: "Job id, e.g. j-1a2b3c4d5e6f." },
      tail: { type: "number", description: "Trailing log lines to return; omit for 40." },
      timeout: { type: "number", description: "Seconds to wait before returning the current state; omit for 120." },
    },
    required: ["id"],
    additionalProperties: false,
  };
}

export default {
  id: "ajq",
  async setup(ctx) {
    // Native tools are the good path the guard points at — register them first
    // so a later failure cannot take them down.
    try {
      await ctx.tool.transform((editor) => {
        editor.add({
          name: "ajq_submit",
          description: SUBMIT_DESCRIPTION,
          input: submitInput(),
          execute: (args, context) => runSubmit(ctx, args, context),
        });
        editor.add({
          name: "ajq_status",
          description: STATUS_DESCRIPTION,
          input: statusInput(),
          execute: (args) => runStatus(args),
        });
        editor.add({
          name: "ajq_output",
          description: OUTPUT_DESCRIPTION,
          input: outputInput(),
          execute: (args) => runOutput(args),
        });
        editor.add({
          name: "ajq_wait",
          description: WAIT_DESCRIPTION,
          input: waitInput(),
          execute: (args) => runWait(args),
        });
      });
    } catch {
      /* the host rejects the tool set; the guard still works */
    }

    // warn — let the command run, then nudge. V2 has no per-tool context channel,
    // so the note rides the tool result back to the model.
    try {
      await ctx.tool.hook("execute.after", async (event) => {
        try {
          if ((await guardMode()) !== "warn" || !isShellTool(event.tool)) return;
          const command = commandOf(event);
          if (command === "" || command.includes("ajq")) return;
          const verdict = await classify(command);
          if (!verdict) return;
          event.result = withNote(event.result, warnNote(command, verdict.kind));
        } catch {
          /* never wedge the host */
        }
      });
    } catch {
      /* hook unsupported on this build */
    }

    // block — deny a heavy shell command, but never on a machine whose queue is
    // not answering.
    try {
      await ctx.tool.hook("execute.before", async (event) => {
        if ((await guardMode()) !== "block" || !isShellTool(event.tool)) return;
        const command = commandOf(event);
        if (command === "" || command.includes("ajq")) return;
        const verdict = await classify(command);
        if (!verdict) return;
        if (!(await spawnOk(["daemon", "status"], CLI_MS))) return;
        throw new Error(blockMessage(command, verdict.kind));
      });
    } catch {
      /* hook unsupported on this build */
    }

    // daemon ensure, best effort, off the critical path.
    void spawn(["daemon", "ensure"], ENSURE_MS);

    // queue snapshot, once per process, as model-visible context.
    try {
      await ctx.session.hook("context", async (event) => {
        try {
          if (snapshotDone) return;
          snapshotDone = true;
          const listing = await spawn(["list", "--json"], CLI_MS);
          if (!listing.includes('"state"')) return;
          const running = countStates(listing, "running");
          const queued = countStates(listing, "queued");
          if (running === 0 && queued === 0) return;
          if (event.system && typeof event.system.push === "function") {
            event.system.push({ type: "text", text: `ajq: ${running} running, ${queued} queued` });
          }
        } catch {
          /* ignore */
        }
      });
    } catch {
      /* hook unsupported on this build */
    }
  },
};
