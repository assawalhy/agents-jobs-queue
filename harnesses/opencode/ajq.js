// ajq — jobs-queue integration for opencode v2.0.18.
//
// Loaded from ~/.config/opencode/plugins/ajq.js. Three jobs:
//   1. make sure ajqd is up, once per process, on the first event or chat message
//   2. deny `bash` calls that are heavy when hooks.guard_mode is "block"
//   3. offer ajq_submit / ajq_status so the model can queue work without a shell
//
// Every entry point is wrapped: a plugin that throws wedges the host session, so
// failures degrade to "" and never propagate.

import { execFile } from "node:child_process";

const ENSURE_MS = 4000;
const CLI_MS = 15000;
const MAX_BUFFER = 4 * 1024 * 1024;

// $AJQ_BIN, else `ajq` from PATH. The @AJQ_BIN@ placeholder is a hook-script
// convention that the installer never substitutes here, so it is not used.
function ajqBin() {
  const fromEnv = process.env.AJQ_BIN;
  if (typeof fromEnv === "string" && fromEnv.trim() !== "") return fromEnv.trim();
  return "ajq";
}

function text(value) {
  return typeof value === "string" ? value.trim() : "";
}

// opencode injects Bun's `$`. It is absent on a non-Bun host, so keep a plain
// child_process path as the fallback rather than losing the plugin.
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

async function cli($, args, ms) {
  try {
    if ($ && typeof $ === "function") {
      const bin = ajqBin();
      return await Promise.race([
        Promise.resolve($`${[bin, ...args]}`.quiet().text()),
        spawn(args, ms),
      ]);
    }
    return await spawn(args, ms);
  } catch {
    return "";
  }
}

// `ajq list --json` is a flat array of job objects, one per job, each carrying
// exactly one "state": "..." pair.
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

async function queueLine($) {
  const listing = await cli($, ["list", "--json"], CLI_MS);
  if (!listing.includes('"state"')) return "";
  const running = countStates(listing, "running");
  const queued = countStates(listing, "queued");
  if (running === 0 && queued === 0) return "";
  return `ajq: ${running} running, ${queued} queued`;
}

let ensured = false;

async function ensureDaemon($, client) {
  if (ensured) return;
  ensured = true;
  await cli($, ["daemon", "ensure"], ENSURE_MS);
  const line = await queueLine($);
  if (line === "") return;
  // tui.toast.show is the documented surface, but v2.0.18's Hooks type does not
  // list it, so nothing dispatches the handler registered below. The client log
  // is the always-available mirror of the same one-liner.
  try {
    await client.app.log({ body: { service: "ajq", level: "info", message: line } });
  } catch {
    /* logging is best effort */
  }
}

// `ajq config` prints `hooks.guard_mode   <mode>`; `ajq config --json` prints the
// merged config. Prefer the labelled line, fall back to a JSON scan.
async function guardMode($) {
  const human = await cli($, ["config"], CLI_MS);
  const mode = text(field(human, "hooks.guard_mode")).split(/\s+/)[0];
  if (mode === "off" || mode === "warn" || mode === "block") return mode;
  const json = await cli($, ["config", "--json"], CLI_MS);
  const hit = json.match(/"guard_mode"\s*:\s*"(off|warn|block)"/);
  return hit ? hit[1] : "warn";
}

async function classify($, command) {
  const verdict = await cli($, ["guard", "--explain", command], CLI_MS);
  if (field(verdict, "heavy") !== "true") return null;
  return { kind: field(verdict, "kind") || "unknown" };
}

function blockMessage(command, kind) {
  return (
    `Blocked by ajq (hooks.guard_mode=block): '${command}' is classified ${kind} (heavy). ` +
    `Submit it instead: ajq submit -- ${command} --label <name> [--timeout <seconds>] ` +
    `[--max-output-bytes <n>], then inspect it with ajq status <id> --json and ajq output <id>.`
  );
}

const SUBMIT_DESCRIPTION =
  "Queue a heavy command (build, test, typecheck, lint, install, docker build, or anything " +
  "expected to take more than ~30s) on the ajq jobs daemon instead of running it in the shell. " +
  "Call this whenever the next step would otherwise be a slow bash command. Returns the job id; " +
  "follow it with ajq_status.";

const STATUS_DESCRIPTION =
  "Read an ajq job: state (queued/running/done/failed/timeout/canceled/lost), queue position, " +
  "ETAs and the tail of its captured output. Call this after ajq_submit instead of sleeping or " +
  "polling by hand.";

// opencode's `tool()` is an identity helper around {description, args, execute}.
// With @opencode-ai/plugin importable we use its Zod builders (real optional
// arguments); otherwise plain JSON-Schema arg entries, which opencode's tool
// registry also accepts through its legacy path.
async function loadSchema() {
  try {
    const mod = await import("@opencode-ai/plugin");
    if (mod && mod.tool && mod.tool.schema) return mod.tool.schema;
  } catch {
    /* not installed as a dependency of this config dir */
  }
  return null;
}

function submitArgs(schema) {
  if (schema) {
    return {
      command: schema.string().describe("Command to run, as a single shell-style string."),
      cwd: schema.string().optional().describe("Working directory; defaults to the session directory."),
      kind: schema
        .string()
        .optional()
        .describe("build | test | typecheck | lint | format | install | docker | check. Omit for auto."),
      timeout: schema.number().optional().describe("Seconds before the job is killed. Omit for the config default."),
      label: schema.string().optional().describe("Short name shown by `ajq list`."),
      priority: schema.number().optional().describe("Higher runs first inside a pool."),
    };
  }
  return {
    command: { type: "string", description: "Command to run, as a single shell-style string." },
    cwd: { type: "string", description: "Working directory; defaults to the session directory." },
    kind: {
      type: "string",
      description: "build | test | typecheck | lint | format | install | docker | check. Omit for auto.",
    },
    timeout: { type: "number", description: "Seconds before the job is killed. Omit for the config default." },
    label: { type: "string", description: "Short name shown by `ajq list`." },
    priority: { type: "number", description: "Higher runs first inside a pool." },
  };
}

function statusArgs(schema) {
  if (schema) {
    return {
      id: schema.string().describe("Job id, e.g. j-1a2b3c4d5e6f."),
      tail: schema.number().optional().describe("Trailing log lines to return; omit for 40."),
    };
  }
  return {
    id: { type: "string", description: "Job id, e.g. j-1a2b3c4d5e6f." },
    tail: { type: "number", description: "Trailing log lines to return; omit for 40." },
  };
}

async function runSubmit($, args, context) {
  try {
    const command = text(args.command);
    if (command === "") return "ajq_submit: command is required";
    const cliArgs = ["submit"];
    if (text(args.label) !== "") cliArgs.push("--label", text(args.label));
    if (text(args.kind) !== "") cliArgs.push("--kind", text(args.kind));
    if (Number(args.timeout) > 0) cliArgs.push("--timeout", String(Math.trunc(Number(args.timeout))));
    if (Number.isFinite(Number(args.priority)) && Number(args.priority) !== 0) {
      cliArgs.push("--priority", String(Math.trunc(Number(args.priority))));
    }
    const cwd = text(args.cwd) || text(context && context.directory);
    if (cwd !== "") cliArgs.push("--cwd", cwd);
    // --shell hands the single string to $SHELL in the daemon, so pipes, &&,
    // globs and env prefixes behave the way the model wrote them.
    cliArgs.push("--shell", "--", command);
    const out = text(await cli($, cliArgs, CLI_MS));
    return out === "" ? `ajq_submit: no output from ajq for ${command}` : out;
  } catch (err) {
    return `ajq_submit failed: ${err && err.message ? err.message : String(err)}`;
  }
}

async function runStatus($, args) {
  try {
    const id = text(args.id);
    if (id === "") return "ajq_status: id is required";
    const meta = await cli($, ["status", id, "--json"], CLI_MS);
    let state = "";
    try {
      state = text(JSON.parse(meta).state);
    } catch {
      state = "";
    }
    const tailArgs = Number(args.tail) > 0 ? ["--tail", String(Math.trunc(Number(args.tail)))] : [];
    const log = text(await cli($, ["output", id, ...tailArgs], CLI_MS));
    return [state === "" ? meta.trim() : `state ${state}`, log === "" ? "(no output yet)" : log].join("\n");
  } catch (err) {
    return `ajq_status failed: ${err && err.message ? err.message : String(err)}`;
  }
}

async function defineTools($) {
  const schema = await loadSchema();
  return {
    ajq_submit: {
      description: SUBMIT_DESCRIPTION,
      args: submitArgs(schema),
      execute: (args, context) => runSubmit($, args, context),
    },
    ajq_status: {
      description: STATUS_DESCRIPTION,
      args: statusArgs(schema),
      execute: (args) => runStatus($, args),
    },
  };
}

export const AjqPlugin = async (ctx) => {
  const $ = ctx && ctx.$;
  const client = (ctx && ctx.client) || null;

  try {
    const once = async () => {
      try {
        await ensureDaemon($, client);
      } catch {
        /* the daemon is optional for everything this plugin does */
      }
    };

    return {
      event: once,
      "chat.message": once,

      // Registered for hosts that do dispatch it; inert on the ones that do not.
      "tui.toast.show": async (input, output) => {
        try {
          if (!output || typeof output !== "object") return;
          if (text(output.message) !== "" || text(input && input.message) !== "") return;
          const line = await queueLine($);
          if (line !== "") output.message = line;
        } catch {
          /* ignore */
        }
      },

      "tool.execute.before": async (input, output) => {
        let reason = "";
        try {
          if (!input || input.tool !== "bash") return;
          const command = text(output && output.args && output.args.command);
          if (command === "" || command.includes("ajq")) return;
          if ((await guardMode($)) !== "block") return;
          const verdict = await classify($, command);
          if (!verdict) return;
          reason = blockMessage(command, verdict.kind);
        } catch {
          return;
        }
        throw new Error(reason);
      },

      tool: await defineTools($),
    };
  } catch {
    return {};
  }
};

// opencode v2 calls server(); v1 called the default export. Export both shapes.
export default {
  id: "ajq",
  server: AjqPlugin,
  setup() {},
};
