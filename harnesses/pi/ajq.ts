// ajq — jobs-queue integration for pi.
//
// Loaded from ~/.pi/agent/extensions/ajq.ts.
//
// What it does: on session_start it runs `ajq daemon ensure` and reports the
// queue, so the daemon is already up before the model asks for anything heavy.
//
// What it deliberately does NOT do: intercept tool calls. pi exposes no
// verified tool-interception hook in its extension API, so a `bash` build/test
// cannot be denied or rewritten from here. The ajq skill is what makes the model
// submit through `ajq submit` on this harness; that is a prompt-level guarantee,
// not an enforced one. Do not add a tool guard here without a verified hook.

import { execFile } from "node:child_process";

const ENSURE_MS = 4000;
const LIST_MS = 5000;

function ajqBin(): string {
  const fromEnv = process.env.AJQ_BIN;
  if (typeof fromEnv === "string" && fromEnv.trim() !== "") return fromEnv.trim();
  return "ajq";
}

function run(args: string[], ms: number): Promise<string> {
  return new Promise((resolve) => {
    let settled = false;
    const done = (value: string) => {
      if (settled) return;
      settled = true;
      resolve(value);
    };
    try {
      execFile(
        ajqBin(),
        args,
        { timeout: ms, maxBuffer: 4 * 1024 * 1024, encoding: "utf8" },
        (err: unknown, stdout: string) => done(err ? "" : String(stdout ?? "")),
      );
    } catch {
      done("");
    }
  });
}

function countStates(listing: string, wanted: string): number {
  const needle = `"state": "${wanted}"`;
  let count = 0;
  let at = listing.indexOf(needle);
  while (at !== -1) {
    count += 1;
    at = listing.indexOf(needle, at + needle.length);
  }
  return count;
}

// pi has no verified notification surface, so try the plausible ones in order and
// fall back to stderr. A session_start hook must never throw into the session.
function say(pi: any, ctx: any, line: string): void {
  const attempts: Array<() => unknown> = [];
  if (ctx?.ui && typeof ctx.ui.notify === "function") attempts.push(() => ctx.ui.notify(line));
  if (ctx && typeof ctx.notify === "function") attempts.push(() => ctx.notify(line));
  if (pi && typeof pi.notify === "function") attempts.push(() => pi.notify(line));
  for (const attempt of attempts) {
    try {
      attempt();
      return;
    } catch {
      /* try the next one */
    }
  }
  try {
    process.stderr.write(`ajq: ${line}\n`);
  } catch {
    /* nothing left to try */
  }
}

export default function (pi) {
  if (!pi || typeof pi.on !== "function") return;

  pi.on("session_start", async (event, ctx) => {
    try {
      await run(["daemon", "ensure"], ENSURE_MS);
      const listing = await run(["list", "--json"], LIST_MS);
      const running = countStates(listing, "running");
      const queued = countStates(listing, "queued");
      if (running === 0 && queued === 0) return;
      say(
        pi,
        ctx,
        `jobs queue up: ${running} running, ${queued} queued — submit builds, tests, ` +
          `typechecks and anything over ~30s with \`ajq submit -- <command>\`, then read it with ` +
          `\`ajq status <id> --json\` / \`ajq wait <id>\` / \`ajq output <id>\`.`,
      );
    } catch {
      /* never wedge the session */
    }
  });
}
