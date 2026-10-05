"""Duration estimation for ajq.

A task is described by the signature `kind|tool|dirs|bucket` where `dirs` are
the top-level directories touched by the working copy and `bucket` the rough
changed-file count. Each signature carries an incremental Welford accumulator
(mean/m2) in the store, so a warm signature is estimated as
`mean + sigma_weight*sigma` while a cold one falls back to
`COLD_DEFAULTS_S[kind]` scaled by the filecount bucket.
"""

from __future__ import annotations

import math
import re
import shlex
import subprocess
from dataclasses import dataclass, field
from typing import Mapping, Sequence

COLD_DEFAULTS_S: dict[str, float] = {
    "build": 300.0,
    "test": 120.0,
    "typecheck": 90.0,
    "lint": 20.0,
    "format": 10.0,
    "install": 240.0,
    "docker": 600.0,
    "check": 30.0,
    "unknown": 60.0,
}

FILECOUNT_BUCKETS: tuple[tuple[int, str], ...] = (
    (1, "1"),
    (5, "2-5"),
    (20, "6-20"),
    (100, "21-100"),
    (1_000_000_000, "100+"),
)

SIGMA_WEIGHT = 0.5
MAX_CHANGED_FILES = 200
MAX_DIRS = 6
COLD_MIN_SCALE = 0.5
COLD_MAX_SCALE = 2.0
_RUN_WORDS = ("run", "run-script", "x", "task")


# --------------------------------------------------------------------------
# command -> kind/tool classification (one tunable table, first match wins)
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class _Rule:
    names: tuple[str, ...]
    kind: str = "unknown"
    subs: Mapping[str, str] = field(default_factory=dict)
    tool: str | None = None
    script: bool = False  # sub-command is a script name after run/run-script
    sub_tool: bool = False  # report "tool:sub" when a sub-command matched


_RULES: tuple[_Rule, ...] = (
    _Rule(
        ("npm", "yarn", "pnpm", "bun", "deno"),
        "unknown",
        {
            "build": "build",
            "bundle": "build",
            "compile": "build",
            "pack": "build",
            "ci": "build",
            "test": "test",
            "t": "test",
            "jest": "test",
            "vitest": "test",
            "typecheck": "typecheck",
            "type-check": "typecheck",
            "tsc": "typecheck",
            "lint": "lint",
            "stylelint": "lint",
            "format": "format",
            "fmt": "format",
            "prettier": "format",
            "check": "check",
            "check-types": "typecheck",
            "audit": "check",
            "outdated": "check",
            "why": "check",
            "start": "check",
            "dev": "check",
            "watch": "check",
            "serve": "check",
            "preview": "check",
            "install": "install",
            "i": "install",
            "add": "install",
            "ci-install": "install",
            "sync": "install",
            "update": "install",
            "upgrade": "install",
            "prune": "install",
            "dedupe": "install",
            "prepare": "install",
            "publish": "install",
            "postinstall": "install",
        },
        script=True,
        sub_tool=True,
    ),
    _Rule(
        ("next",),
        "unknown",
        {
            "build": "build",
            "export": "build",
            "lint": "lint",
            "test": "test",
            "dev": "check",
            "start": "check",
            "info": "check",
        },
        sub_tool=True,
    ),
    _Rule(("vite", "webpack", "rollup", "esbuild", "parcel", "turbo"), "build",
          {"dev": "check", "serve": "check", "preview": "check", "watch": "check"}),
    _Rule(("tsc", "mypy", "pyright", "pyre", "flow", "svelte-check", "vue-tsc",
           "astro-check"), "typecheck"),
    _Rule(("jest", "vitest", "mocha", "ava", "jasmine", "karma", "tap", "node-tap",
           "phpunit", "rspec", "cucumber"), "test"),
    _Rule(("playwright", "cypress", "selenium"), "test",
          {"install": "install", "codegen": "check", "open": "check", "screenshot": "check"}),
    _Rule(("pytest", "py.test", "tox", "nox", "trial", "unittest"), "test"),
    _Rule(("make", "gmake", "cmake", "ninja", "meson"), "build",
          {"test": "test", "check": "test", "install": "install", "clean": "check",
           "format": "format", "lint": "lint"}),
    _Rule(("bazel", "bazelisk", "buck", "buck2", "pants"), "build",
          {"test": "test", "query": "check", "cquery": "check", "run": "unknown",
           "info": "check", "clean": "check", "fetch": "install"}),
    _Rule(("cargo",), "unknown",
          {"build": "build", "b": "build", "test": "test", "bench": "test",
           "check": "build", "clippy": "typecheck", "fmt": "format", "format": "format",
           "install": "install", "doc": "check", "tree": "check", "audit": "check",
           "run": "unknown", "publish": "install"}),
    _Rule(("rustc",), "build"),
    _Rule(("rustup",), "install",
          {"update": "install", "toolchain": "install", "component": "install",
           "default": "install", "show": "check", "which": "check"}),
    _Rule(("go",), "build",
          {"build": "build", "test": "test", "vet": "lint", "fmt": "format",
           "install": "install", "generate": "build", "mod": "check", "work": "check",
           "run": "unknown", "list": "check", "env": "check"}),
    _Rule(("gofmt", "gofumpt", "golines"), "format"),
    _Rule(("gradle", "gradlew", "mvn", "mvnw"), "build",
          {"test": "test", "check": "test", "verify": "test", "build": "build",
           "assemble": "build", "compile": "build", "package": "build", "install": "install",
           "clean": "check", "tasks": "check", "dependencies": "check", "run": "unknown",
           "bootrun": "unknown", "publish": "install"}),
    _Rule(("dotnet",), "build",
          {"build": "build", "test": "test", "publish": "install", "restore": "install",
           "pack": "install", "clean": "check", "format": "format", "tool": "check",
           "run": "unknown"}),
    _Rule(("sbt",), "build",
          {"test": "test", "compile": "build", "doc": "check", "clean": "check",
           "run": "unknown", "update": "install", "publish": "install"}),
    _Rule(("mix",), "build",
          {"test": "test", "compile": "build", "deps": "install", "deps.get": "install",
           "format": "format", "run": "unknown", "clean": "check"}),
    _Rule(("swift",), "build",
          {"build": "build", "test": "test", "package": "install", "format": "format",
           "run": "unknown", "clean": "check"}),
    _Rule(("flutter",), "build",
          {"build": "build", "test": "test", "pub": "install", "analyze": "lint",
           "format": "format", "doctor": "check", "clean": "check", "run": "unknown"}),
    _Rule(("dart",), "unknown",
          {"analyze": "lint", "format": "format", "test": "test", "compile": "build",
           "pub": "install", "run": "unknown"}),
    _Rule(("docker", "podman", "nerdctl", "docker-compose", "podman-compose"), "docker",
          {"build": "docker", "buildx": "docker", "compose": "docker", "run": "docker",
           "push": "docker", "pull": "install", "image": "docker", "exec": "check",
           "logs": "check", "inspect": "check", "ps": "check"},
          sub_tool=True),
    _Rule(("pip", "pip3", "uv", "pipenv", "poetry"), "install",
          {"install": "install", "download": "install", "wheel": "build", "build": "build",
           "add": "install", "sync": "install", "lock": "install", "update": "install",
           "upgrade": "install", "publish": "install", "remove": "install",
           "list": "check", "show": "check", "freeze": "check", "outdated": "check",
           "env": "check", "venv": "install"}),
    _Rule(("eslint", "tslint", "stylelint", "shellcheck", "ruff", "flake8", "pylint",
           "phpcs", "phpstan", "rubocop", "credo", "golangci-lint",
           "staticcheck", "cppcheck", "checkstyle", "biome", "oxlint", "vet"), "lint",
          {"fix": "format", "autocorrect": "format"}),
    _Rule(("prettier", "black", "rustfmt", "clang-format", "isort", "shfmt", "taplo",
           "gci", "blackish", "sort-imports"), "format"),
    _Rule(
        ("git",),
        "unknown",
        {
            "status": "check", "diff": "check", "log": "check", "show": "check",
            "blame": "check", "branch": "check", "describe": "check", "rev-parse": "check",
            "ls-files": "check", "ls-tree": "check", "cat-file": "check", "grep": "check",
            "shortlog": "check", "remote": "check", "config": "check", "rev-list": "check",
            "reflog": "check", "stash": "check", "worktree": "check", "tag": "check",
            "clean": "check", "fetch": "install", "clone": "install", "pull": "install",
            "push": "install", "submodule": "install", "lfs": "install",
        },
    ),
    _Rule(
        (
            "rg", "ripgrep", "ag", "ack", "jq", "yq", "ls", "cat", "head", "tail", "less",
            "wc", "find", "fd", "grep", "egrep", "fgrep", "awk", "gawk", "sed", "cut",
            "sort", "uniq", "tr", "tee", "pwd", "which", "whereis", "basename", "dirname",
            "realpath", "readlink", "tree", "du", "df", "stat", "file", "echo", "printf",
            "date", "uname", "env", "printenv", "id", "whoami", "hostname", "seq",
            "diff", "cmp", "md5sum", "sha1sum", "sha256sum", "xxd", "hexdump", "strings",
            "nm", "objdump", "readelf", "ldd", "otool", "lipo", "true", "false",
        ),
        "check",
    ),
)

_RULE_BY_NAME: dict[str, _Rule] = {
    name: rule for rule in _RULES for name in rule.names
}

_TOOL_ALIASES: dict[str, str] = {
    "gradlew": "gradle",
    "mvnw": "mvn",
    "py.test": "pytest",
}

_RUNNERS = frozenset(
    {
        "npx", "bunx", "pnpx", "uvx", "pipx", "sudo", "env", "time", "timeout", "nice",
        "nohup", "stdbuf", "watchexec", "command", "exec", "xargs", "parallel",
    }
)
_SHELLS = frozenset({"sh", "bash", "zsh", "dash", "ksh", "fish"})

_STATUS_RE = re.compile(r"^[ MADRCU?!][ MADRCU?!][ MADRCU?!]")


def _basename(token: str) -> str:
    name = str(token).replace("\\", "/").rstrip("/").split("/")[-1]
    return name.lower()


def _subcommand(args: Sequence[str]) -> str:
    if len(args) > 1 and not args[1].startswith("-"):
        return args[1].lower()
    return ""


def _script_name(args: Sequence[str], sub: str) -> str:
    tail = args[1:]
    for index, token in enumerate(tail):
        if token.lower() in _RUN_WORDS:
            for candidate in tail[index + 1 :]:
                if not candidate.startswith("-"):
                    return _tool_name(candidate.lower())
    return ""


MAX_TOOL_LEN = 32


def _tool_name(value: str) -> str:
    """Bound a tool name so one odd command cannot blow up the cache keys."""
    text = _basename(str(value)) or "unknown"
    return text[:MAX_TOOL_LEN]


def _rule_for(argv: Sequence[str], depth: int) -> tuple[str, str]:
    args = [str(item) for item in argv]
    if not args or depth > 4:
        return ("unknown", "")
    name = _basename(args[0])

    if name in _SHELLS and "-c" in args:
        index = args.index("-c")
        if index + 1 < len(args):
            try:
                parts = shlex.split(args[index + 1])
            except ValueError:
                return ("unknown", name)
            return _rule_for(parts, depth + 1)

    if name.startswith("python") and "-c" in args:
        # `python -c "<arbitrary code>"` has no meaningful script name: using the
        # code as the tool would give every ad-hoc snippet its own cache key and
        # the estimate cache would never hit again.
        return ("unknown", name)

    if name.startswith("python") and "-m" in args:
        index = args.index("-m")
        if index + 1 < len(args):
            return _rule_for(args[index + 1 :], depth + 1)

    if name in _RUNNERS:
        rest = [arg for arg in args[1:] if not arg.startswith("-")]
        if name == "env":
            rest = [arg for arg in rest if "=" not in arg.split("/")[-1]]
        if rest:
            return _rule_for(rest, depth + 1)

    rule = _RULE_BY_NAME.get(name)
    if rule is None:
        return ("unknown", _tool_name(name))
    tool = rule.tool or _TOOL_ALIASES.get(name, _tool_name(name))
    sub = _subcommand(args)
    if rule.script and sub in _RUN_WORDS:
        sub = _script_name(args, sub)
    if not sub:
        return (rule.kind, tool)
    return (rule.subs.get(sub, rule.kind), f"{tool}:{sub}" if rule.sub_tool else tool)


def kind_from_command(argv: Sequence[str]) -> str:
    return _rule_for(list(argv), 0)[0]


def tool_from_command(argv: Sequence[str]) -> str:
    return _rule_for(list(argv), 0)[1]


# --------------------------------------------------------------------------
# working-copy fingerprint
# --------------------------------------------------------------------------


def changed_files(cwd: str, timeout: float = 2.0) -> list[str]:
    """Paths reported by `git status --porcelain -z`; [] on any failure."""
    try:
        result = subprocess.run(
            ["git", "status", "--porcelain", "-z"],
            cwd=cwd or ".",
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, ValueError, subprocess.SubprocessError):
        return []
    if result.returncode != 0:
        return []
    files: list[str] = []
    for chunk in (result.stdout or "").split("\0"):
        if not chunk or not _STATUS_RE.match(chunk):
            continue  # rename/copy source path; git already lists the destination
        path = chunk[3:].strip()
        if path:
            files.append(path)
        if len(files) >= MAX_CHANGED_FILES:
            break
    return files


def _clean_path(path: str) -> str:
    cleaned = str(path).replace("\\", "/").strip()
    while cleaned.startswith("./"):
        cleaned = cleaned[2:]
    return cleaned.strip("/")


def _top_dir(path: str) -> str:
    cleaned = _clean_path(path)
    head, sep, _rest = cleaned.partition("/")
    if not sep or head in ("", ".", ".."):
        return ""  # a file at the repository root contributes no directory
    return head


def bucket_label(count: int) -> str:
    for limit, label in FILECOUNT_BUCKETS:
        if count <= limit:
            return label
    return FILECOUNT_BUCKETS[-1][1]


def bucket_scale(bucket: str) -> float:
    """Cold-default multiplier: 1 file -> 0.5, 100+ files -> 2.0, linear between."""
    labels = [label for _limit, label in FILECOUNT_BUCKETS]
    index = labels.index(bucket) if bucket in labels else 0
    span = len(labels) - 1
    return COLD_MIN_SCALE + (COLD_MAX_SCALE - COLD_MIN_SCALE) * index / span


def file_signature(files: Sequence[str]) -> str:
    """`"src,tests/21-100"`: touched top-level dirs plus the filecount bucket."""
    touched = list(files)
    dirs = sorted({top for top in (_top_dir(path) for path in touched) if top})
    dirs = [item for item in dirs if item not in (".", "..")][:MAX_DIRS]
    return f"{','.join(dirs) or 'root'}/{bucket_label(len(touched))}"


def signature_for(
    kind: str, tool: str, cwd: str, files: Sequence[str] | None = None
) -> str:
    """-> `kind|tool|dirs|bucket`, e.g. `build|npm:build|src,tests|21-100`."""
    touched = changed_files(cwd) if files is None else list(files)
    dirs, _sep, bucket = file_signature(touched).rpartition("/")
    return f"{kind or 'unknown'}|{tool or ''}|{dirs or 'root'}|{bucket}"


def parse_signature(signature: str) -> tuple[str, str, str, str]:
    """-> (kind, tool, dirs, bucket).

    Accepts the canonical `kind|tool|dirs|bucket` and the short form
    `kind|tool|dirs/bucket` that `file_signature` returns.
    """
    parts = (str(signature).split("|") + ["", "", "", ""])[:4]
    kind, tool, dirs, bucket = parts
    if not bucket and "/" in dirs:
        dirs, _sep, bucket = dirs.rpartition("/")
    return (kind, tool, dirs, bucket)


# --------------------------------------------------------------------------
# estimation
# --------------------------------------------------------------------------


def _sigma(n: int, m2: float) -> float:
    if n < 2:
        return 0.0
    return math.sqrt(max(float(m2), 0.0) / (n - 1))


def estimate_seconds(
    store,
    kind: str,
    tool: str,
    cwd: str,
    files: Sequence[str] | None = None,
) -> tuple[float, str]:
    """-> (seconds, est_source) where est_source is "cache:n=6" or "cold:build"."""
    signature = signature_for(kind, tool, cwd, files)
    row = store.get_estimate(signature) if store is not None else None
    n = int(row["n"]) if row else 0
    if row is not None and n >= 2:
        mean = max(float(row["mean"]), 0.001)
        est = mean + SIGMA_WEIGHT * _sigma(n, float(row["m2"]))
        return (min(max(est, mean / 2.0), mean * 3.0), f"cache:n={n}")
    kind_key = kind if kind in COLD_DEFAULTS_S else "unknown"
    _kind, _tool, _dirs, bucket = parse_signature(signature)
    return (COLD_DEFAULTS_S[kind_key] * bucket_scale(bucket), f"cold:{kind_key}")


def record(
    store,
    kind: str,
    tool: str,
    cwd: str,
    seconds: float,
    files: Sequence[str] | None = None,
) -> str:
    signature = signature_for(kind, tool, cwd, files)
    store.record_duration(signature, kind or "", tool or "", float(seconds))
    return signature


def mape_pct(estimates: Sequence[float], actuals: Sequence[float]) -> float:
    """Mean absolute percentage error of estimates against actuals, in percent."""
    errors = [
        abs(float(est) - float(actual)) / float(actual) * 100.0
        for est, actual in zip(estimates, actuals)
        if float(actual) > 0.0
    ]
    return sum(errors) / len(errors) if errors else 0.0


def accuracy(store, limit: int = 50) -> list[dict]:
    """Per-signature accuracy rows computed from finished jobs with an estimate."""
    rows = store.estimate_rows(limit=max(1, int(limit)) * 10)
    grouped: dict[str, list[dict]] = {}
    for row in rows:
        grouped.setdefault(str(row["signature"]), []).append(row)
    report: list[dict] = []
    for signature, group in grouped.items():
        actuals = [float(item["actual_s"]) for item in group]
        estimates = [float(item["est_seconds"]) for item in group]
        kind, tool, _dirs, _bucket = parse_signature(signature)
        n = len(actuals)
        mean = sum(actuals) / n
        report.append(
            {
                "signature": signature,
                "kind": kind,
                "tool": tool,
                "n": n,
                "samples": n,
                "mean_seconds": round(mean, 3),
                "sigma_seconds": round(_sigma(n, sum((a - mean) ** 2 for a in actuals)), 3),
                "est_mean_seconds": round(sum(estimates) / n, 3),
                "mape_pct": round(mape_pct(estimates, actuals), 2),
            }
        )
    report.sort(key=lambda item: (-item["samples"], item["signature"]))
    return report[: max(0, int(limit))]