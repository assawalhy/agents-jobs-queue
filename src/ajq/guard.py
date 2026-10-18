"""Command classification: argv -> {kind, tool, pool, heavy, reason}.

The whole verdict comes from `RULES`, one ordered table of (regex, kind) pairs
matched against the joined command, most specific first. Tuning is a one-place
edit: add a row, move a row, change a row — nothing else in ajq knows the tool
vocabulary.

    HEAVY_KINDS  build test typecheck install docker  -> pool "heavy"
    LIGHT_KINDS  lint format check                   -> pool "light"
    everything else                                 -> kind "unknown", pool "normal"
"""

from __future__ import annotations

import os
import re
import shlex
from typing import Sequence

HEAVY_KINDS: frozenset[str] = frozenset({"build", "test", "typecheck", "install", "docker"})
LIGHT_KINDS: frozenset[str] = frozenset({"lint", "format", "check"})

_PKG = r"(?:npm|yarn|pnpm|bun|deno)"
_SUB = r"\s+(?:(?:run|run-script|task)\s+)?"
_JVM = r"(?:mvn|mvnw|gradle|gradlew|sbt|mill)"

# (regex, kind) — first match wins, so the specific rows come first.
RULES: tuple[tuple[str, str], ...] = (
    # ---- install -------------------------------------------------------
    (rf"\b{_PKG}\s+(?:i|ci|add|install)\b", "install"),
    (r"\b(?:pip3?|python3?\s+-m\s+pip|pipx|uv|uvx|poetry|pdm|rye|conda)\s+(?:install|add|sync)\b", "install"),
    (r"\b(?:gem|cargo|go|nix|goinstall|npm|yarn|pnpm|bun|pip|brew)\s+install\b", "install"),
    (r"\b(?:apt|apt-get|aptitude|dnf|yum|zypper|pacman|apk|brew|port|emerge|snap|nix-env)\s+(?:install|add)\b", "install"),
    (r"\bpacman\s+-S\w*\b", "install"),
    (r"\bflutter\s+pub\s+(?:get|add|upgrade)\b", "install"),
    (r"\bnix(?:-shell)?\s+(?:develop|shell|profile)\b", "install"),
    (r"\bbundle\s+install\b", "install"),
    # ---- docker --------------------------------------------------------
    (r"\b(?:docker|docker-compose|podman|nerdctl)\s+(?:compose|build|buildx|run|push|pull)\b", "docker"),
    (r"\b(?:docker-compose|docker\s+compose)\b", "docker"),
    # ---- typecheck -----------------------------------------------------
    (r"\bcargo\s+clippy\b", "typecheck"),
    (r"\b(?:tsc|vue-tsc|svelte-check|mypy|pyright|dmypy|tsgo|basedpyright)\b", "typecheck"),
    (rf"\b{_PKG}{_SUB}(?:typecheck|type-check|check-types|lint:types|tsc)\b", "typecheck"),
    (r"\bflow\s+(?:check|focus-check)\b", "typecheck"),
    (r"\bflutter\s+analyze\b", "typecheck"),
    (r"\bdotnet\s+(?:build|msbuild)\b", "typecheck"),
    # ---- build ---------------------------------------------------------
    (r"\bcargo\s+(?:build|b|check|bench|doc|rustdoc)\b", "build"),
    (rf"\b{_PKG}{_SUB}(?:build|rebuild|compile|bundle|dist|prepare|prepack|assemble|package|pack)\b", "build"),
    (r"\b(?:next|nuxt|ng|astro|gatsby|remix|svelte-kit)\s+build\b", "build"),
    (r"\b(?:vite|webpack|rollup|esbuild|parcel|swc|rspack|turbo|nx)\s+(?:build|bundle)\b", "build"),
    (rf"\b{_JVM}\s+(?:[\w.+-]+:)?(?:build|assemble|compile|classes|jar|installDist|bundle|shadowJar)\b", "build"),
    (r"\bcmake\b[\w\s.+-]*--build\b", "build"),
    (r"\b(?:g?make|cmake|meson|ninja|nix|bazel|bazelisk)(?![\w.+-])", "build"),
    (r"\bgo\s+(?:build|install|generate)\b", "build"),
    (r"\bmix\s+compile\b", "build"),
    (r"\bswift\s+build\b", "build"),
    (r"\bflutter\s+(?:build|bundle)\b", "build"),
    (r"\btsdown\b", "build"),
    # ---- test ----------------------------------------------------------
    (rf"\b{_PKG}{_SUB}(?:test|tests|test:\S+|e2e|it|vitest|jest)\b", "test"),
    (r"\b(?:pytest|py\.test|tox|nox|unittest|hypothesis|nextest)\b", "test"),
    (r"\bpython3?\s+-m\s+(?:pytest|unittest|tox|coverage)\b", "test"),
    (r"\b(?:jest|vitest|mocha|jasmine|karma|ava|tape|playwright|cypress|phpunit|rspec|behat|cucumber|junit|testcafe|checkstyle)\b", "test"),
    (r"\bcargo\s+(?:test|t|nextest|clippy)\b", "test"),
    (r"\bgo\s+test\b", "test"),
    (rf"\b{_JVM}\s+(?:[\w.+-]+:)?(?:test|check|verify|integrationTest|failsafe)\b", "test"),
    (r"\bdotnet\s+test\b", "test"),
    (r"\b(?:sbt|mix|swift|flutter|nx)\s+test\b", "test"),
    (r"\bnode\s+(?:--test|test)\b", "test"),
    (rf"\b{_JVM}(?![\w.+-])", "build"),
    (r"\bnix\s+build\b", "build"),
    # ---- lint / format (light) -----------------------------------------
    (r"\b(?:npm|yarn|pnpm|bun|deno)\s+(?:run\s+)?(?:format|fmt|prettier|format:check)\b", "format"),
    (r"\bprettier\b", "format"),
    (r"\b(?:black|isort|autopep8|yapf|ruff\s+format|rubyfmt|gofmt|gofumpt|goimports|rustfmt|clang-format|csharpier|dprint|biome\s+format|rome)\b", "format"),
    (r"\b(?:npm|yarn|pnpm|bun|deno)\s+(?:run\s+)?(?:lint|lint:\S*|stylecheck|eslint)\b", "lint"),
    (r"\b(?:eslint|stylelint|oxlint|tslint|biome\s+(?:lint|check)|shellcheck|flake8|pylint|ruff|checkstyle|hadolint|markdownlint|rubocop|luacheck|detekt|credo|phpmd|phpstan|psalm)\b", "lint"),
    (r"\bgo\s+(?:vet|fmt)\b", "lint"),
    (r"\bclang-tidy\b", "lint"),
    # ---- read-only (light) ----------------------------------------------
    (r"\bgit\s+(?:status|diff|log|show|blame|shortlog|describe|branch|ls-files|ls-tree|rev-parse|rev-list|tag|config|remote|diff-tree|whatchanged|annotate)\b", "check"),
    (r"\b(?:rg|ripgrep|ag)\b", "check"),
    (r"\b(?:jq|yq)\b", "check"),
    (r"\b(?:ls|cat|head|tail|wc|stat|file|du|df|echo|pwd|which|printenv|realpath|basename|dirname|tree|watch)\b", "check"),
)

_COMPILED: tuple[tuple[re.Pattern[str], str], ...] = tuple(
    (re.compile(pattern, re.IGNORECASE), kind) for pattern, kind in RULES
)

_SCRIPTS = {"run", "run-script", "task", "dlx", "exec", "x", "dlxp"}
_MAX_TOOL = 32
_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_WRAPPERS = {
    # interpreters / launchers that take the real tool as a later argument
    "python", "python3", "py", "env", "sudo", "nice", "time", "command", "exec",
    "stdbuf", "xargs", "uv", "poetry", "pipenv", "pdm", "rye", "pixi", "mise",
    "asdf", "direnv", "timeout", "watchexec",
}


def _is_assignment(token: str) -> bool:
    """True for `FOO=bar` / `FOO=bar=baz` environment assignments."""
    head = token.split("=", 1)[0]
    return bool(head) and "=" in token and not token.startswith("=") and _IDENT.fullmatch(head) is not None


_RUNNER_FAMILIES = {"npm", "yarn", "pnpm", "bun", "deno", "npx", "bunx", "dlx"}
# flags that consume the next token as their value
_VALUE_FLAGS = {
    "-n", "-j", "-p", "-C", "-o", "-d", "-w", "-c", "-m", "-t", "--max-workers",
    "--jobs", "--parallel", "--cpus", "--memory", "--stack", "--release",
}


def _flag_takes_value(token: str) -> bool:
    return token in _VALUE_FLAGS or token.split("=", 1)[0] in _VALUE_FLAGS


def _tool_for(argv: Sequence[str]) -> str:
    """Tool label used by estimates and stats: `npm:build`, `cargo`, `pytest`.

    Unwraps launchers (`uv run`, `env FOO=1`, `nice -n 10`) so the same real tool
    lands on the same estimate-cache key no matter how it was invoked.
    """
    args = [str(item) for item in argv]
    while args and _is_assignment(args[0]):
        args.pop(0)                       # FOO=1 cmd
    if not args:
        return "unknown"
    head = os.path.basename(args[0].strip())
    if head in {"python", "python3", "py"}:
        if "-m" in args:
            index = args.index("-m")
            if index + 1 < len(args):
                return (os.path.basename(args[index + 1]) or head)[:_MAX_TOOL]
        if "-c" in args:
            # inline code is not a tool name: it would give every ad-hoc snippet
            # its own cache key and the estimate cache would never hit again
            return head[:_MAX_TOOL]

    index = 0
    head = ""
    while index < len(args):
        token = args[index]
        base = os.path.basename(token.strip())
        if base in _WRAPPERS or base in _SCRIPTS or _is_assignment(token):
            index += 1
            continue
        if base.isdigit():
            # `timeout 300 pytest`, `xargs -n 1 pytest`: the wrapper's duration or
            # count, not the tool
            index += 1
            continue
        if token.startswith("-") and token != "-":
            index += 2 if _flag_takes_value(token) and index + 1 < len(args) else 1
            continue
        head = base
        break
    if not head:
        return (os.path.basename(args[0].strip()) or "unknown")[:_MAX_TOOL]

    rest = [item for item in args[index + 1:] if not item.startswith("-") and not _is_assignment(item)]
    while rest and os.path.basename(rest[0].strip()) in _SCRIPTS:
        rest.pop(0)
    if head in _RUNNER_FAMILIES and rest:
        return f"{head}:{os.path.basename(rest[0].strip())}"[:_MAX_TOOL]
    return head[:_MAX_TOOL]


SHELL_NAMES: frozenset[str] = frozenset({"sh", "bash", "zsh", "dash", "ksh", "ash", "fish"})

# `cd <dir> && ` at the start of a shell script, quoted or bare.
_CD_PREFIX = re.compile(r"""^\s*cd\s+(?P<dir>'[^']*'|"[^"]*"|\S+)\s*&&\s*""")

# Anything that only a shell can run: these need `ajq submit --shell`.
_SHELLISH = re.compile(r"[|&;<>()`$*?]")


def shell_script(argv: Sequence[str]) -> str | None:
    """The script of a `sh -c <script>` argv, or None for a plain command."""
    args = [str(item) for item in (argv or [])]
    if len(args) == 3 and args[1] == "-c" and os.path.basename(args[0].strip()) in SHELL_NAMES:
        return args[2]
    return None


def split_cd_prefix(command: str, limit: int = 4) -> tuple[str, str]:
    """Split a leading `cd <dir> &&` chain off a shell command string.

    Returns the last directory the chain enters and the rest of the command.
    Agents and harnesses submit `sh -c 'cd <worktree> && <command>'`, and the
    daemon already runs a job in its own cwd, so the prefix carries no
    information worth displaying. Everything after it is left verbatim.
    """
    directory = ""
    rest = str(command or "")
    for _ in range(max(0, limit)):
        match = _CD_PREFIX.match(rest)
        if match is None:
            break
        raw = match.group("dir")
        if len(raw) > 1 and raw[0] in "'\"" and raw[-1] == raw[0]:
            raw = raw[1:-1]
        directory = raw
        rest = rest[match.end() :]
    return directory, rest


def strip_cd_args(argv: Sequence[str]) -> tuple[str, list[str]]:
    """`["cd", dir, "&&", ...rest]` -> `(dir, rest)`; no chain, no shell needed."""
    items = [str(item) for item in (argv or [])]
    directory = ""
    while len(items) >= 4 and items[0] == "cd" and items[2] == "&&":
        directory = items[1]
        items = items[3:]
    return directory, items


# Shell operator words that must stay bare when a command line is rebuilt.
_OPERATORS = frozenset(
    {"|", "||", "&&", ";", "&", ">", ">>", "<", "<<", "|&", "2>", "2>>", "2>&1", "&>", "&>>"}
)


def _shell_line(words: Sequence[str]) -> str:
    """Render an argv as a shell command line: operators bare, every word quoted."""
    return " ".join(word if word in _OPERATORS else shlex.quote(word) for word in words)


def submit_line(argv: Sequence[str], pool: str | None = None) -> str:
    """The `ajq submit` line that runs this command, with a cd prefix turned into --cwd.

    A `cd <dir> && <command>` prefix becomes `--cwd <dir>`: the daemon runs the
    job in one directory, so a shell dance around it buys nothing and hides the
    command. `--shell` is added when the command needs a shell (pipes, `&&`,
    redirects, globs), because the agent's own shell would otherwise split it.
    """
    items = [str(item) for item in (argv or [])]
    script = shell_script(items)
    if script is not None:
        directory, body = split_cd_prefix(script)
        needs_shell = True
    else:
        directory, rest = strip_cd_args(items)
        words = rest if directory else items
        plain = shlex.join(words)
        needs_shell = bool(_SHELLISH.search(plain))
        # with --shell the argument is a command line, not an argv, so the words
        # are quoted for a shell instead of for the daemon's argv parser
        body = _shell_line(words) if needs_shell else plain
    parts = ["ajq", "submit"]
    if pool:
        parts += ["--pool", pool]
    if directory:
        parts += ["--cwd", directory]
    if needs_shell:
        parts += ["--shell"]
    return f"{shlex.join(parts)} -- {body}"


def classify(argv: Sequence[str]) -> dict:
    """Verdict for one command. Never raises, whatever `argv` holds."""
    try:
        args = [str(item) for item in (argv or [])]
    except TypeError:
        args = [str(argv)]
    tool = _tool_for(args)
    if not args:
        return {"kind": "unknown", "tool": tool, "pool": "normal", "heavy": False, "reason": "empty command"}
    command = " ".join(args)
    for pattern, kind in _COMPILED:
        match = pattern.search(command)
        if match is None:
            continue
        pool = "heavy" if kind in HEAVY_KINDS else "light" if kind in LIGHT_KINDS else "normal"
        return {
            "kind": kind,
            "tool": tool,
            "pool": pool,
            "heavy": kind in HEAVY_KINDS,
            "reason": f"{kind}: matched {match.group(0).strip()}",
        }
    return {
        "kind": "unknown",
        "tool": tool,
        "pool": "normal",
        "heavy": False,
        "reason": "unknown: no rule matched",
    }


def explain(argv: Sequence[str]) -> str:
    """Multi-line human explanation of a verdict, for `ajq guard --explain`."""
    args = [str(item) for item in (argv or [])]
    verdict = classify(args)
    command = " ".join(args)
    if verdict["heavy"]:
        headline = "HEAVY - worth queueing with ajq"
        suggestion = submit_line(args)
    elif verdict["kind"] in LIGHT_KINDS:
        headline = "LIGHT - cheap, run it directly"
        suggestion = command
    else:
        headline = "UNKNOWN - ajq cannot place it in a pool"
        suggestion = submit_line(args, pool="normal")
    return "\n".join(
        [
            f"command: {command}",
            f"verdict: {headline}",
            f"kind:    {verdict['kind']}",
            f"tool:    {verdict['tool']}",
            f"pool:    {verdict['pool']}",
            f"heavy:   {str(verdict['heavy']).lower()}",
            f"rule:    {verdict['reason']}",
            f"suggest: {suggestion}",
        ]
    )


def is_heavy_command(command: str) -> bool:
    """True when a shell command string looks heavy (PreToolUse guard input)."""
    try:
        argv = shlex.split(str(command))
    except ValueError:
        argv = str(command).split()
    if not argv:
        return False
    return bool(classify(argv)["heavy"])