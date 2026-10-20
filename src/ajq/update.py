"""Update check and install for ajq.

The CLI ships as a zipapp with no dependencies, so everything here is stdlib:
`urllib` for the network, `xml` for the release feed, `subprocess` for the
installer.

Two sources give the available refs:

  * `https://github.com/<repo>/releases.atom` is the primary one. It needs no
    token, it has no rate limit, and it lists the releases and the tags.
  * `https://api.github.com/repos/<repo>/releases/latest` is the fallback and
    the addition. It gives the published release, its note and its assets. It
    is unauthenticated and limited to 60 requests an hour per address, so a
    failure there must not fail the command while the feed worked.

The install runs the same installer the one line install runs, pinned to a ref:
the script comes from the raw URL at the ref and `AJQ_VERSION` pins the payload
fetch to the same ref.
"""

from __future__ import annotations

import html
import json
import os
import re
import ssl
import subprocess
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from typing import Any, Optional, Sequence

from . import paths

# The repo the update checks. It must match REPO_SLUG in install.sh.
REPO = "assawalhy/agents-jobs-queue"
USER_AGENT = "ajq-update"
ATOM_NS = "{http://www.w3.org/2005/Atom}"
TIMEOUT = 15.0
# The registry keys that are not a harness.
RESERVED_KEYS = ("version", "shared")

_VERSION_RE = re.compile(r"^v?(\d+(?:\.\d+)*)([-+].*)?$", re.IGNORECASE)
_TAG_RE = re.compile(r"<[^>]+>")


class UpdateError(Exception):
    """A check or an install that could not be done."""


# -- network ----------------------------------------------------------------


def fetch(url: str) -> str:
    """The one network call. Every request goes through here."""
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            return response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        raise UpdateError(f"{url} returned HTTP {exc.code}") from exc
    except urllib.error.URLError as exc:
        reason = exc.reason
        if isinstance(reason, ssl.SSLCertVerificationError):
            raise UpdateError(
                f"{url} could not be verified: the local certificate store did not"
                " verify GitHub (set SSL_CERT_FILE to a CA bundle)"
            ) from exc
        raise UpdateError(f"{url} is unreachable: {reason}") from exc
    except OSError as exc:
        raise UpdateError(f"{url} failed: {exc}") from exc


def feed_url(repo: str = REPO) -> str:
    return f"https://github.com/{repo}/releases.atom"


def api_url(repo: str = REPO) -> str:
    return f"https://api.github.com/repos/{repo}/releases/latest"


def script_url(repo: str, ref: str) -> str:
    return f"https://raw.githubusercontent.com/{repo}/{ref}/install.sh"


# -- versions ---------------------------------------------------------------


def _split_version(ref: str) -> tuple[Optional[tuple[int, ...]], str]:
    """The numbers and the suffix of a version ref, or (None, "")."""
    if not ref:
        return None, ""
    match = _VERSION_RE.match(ref.strip())
    if not match:
        return None, ""
    numbers = tuple(int(part) for part in match.group(1).split("."))
    return numbers, match.group(2) or ""


def parse_version(ref: str) -> Optional[tuple[int, ...]]:
    """The numbers of a version ref, or None when the ref is not a version.

    `v0.1.2` is `(0, 1, 2)`, `0.2` is `(0, 2)`, `v1.2.3-rc1` is `(1, 2, 3)`
    and `main` is None.
    """
    numbers, _ = _split_version(ref)
    return numbers


def is_prerelease(ref: str) -> bool:
    """True when the ref carries a pre-release suffix, as in `v1.2.3-rc1`."""
    numbers, suffix = _split_version(ref)
    return numbers is not None and suffix.startswith("-")


def version_key(ref: str) -> Optional[tuple]:
    """A sort key for a version ref, or None when the ref is not a version.

    The numbers are padded so `0.2` and `0.2.0` are the same version, and a
    pre-release sorts below the same version without one.
    """
    numbers, suffix = _split_version(ref)
    if numbers is None:
        return None
    padded = numbers + (0,) * (3 - len(numbers)) if len(numbers) < 3 else numbers
    return (padded, 0 if is_prerelease(ref) else 1, suffix)


def version_of(ref: str) -> Optional[str]:
    """The version a ref names: `v0.1.2` is `0.1.2`."""
    if not ref:
        return None
    text = ref.strip()
    if parse_version(text) is None:
        return None
    if text[:1] in ("v", "V") and text[1:2].isdigit():
        text = text[1:]
    return text


# -- the release feed -------------------------------------------------------


def clean_note(raw: str) -> str:
    """The note of a release as plain text."""
    text = html.unescape(raw or "")
    text = _TAG_RE.sub(" ", text)
    text = html.unescape(text)
    return " ".join(text.split())


def _entry_text(entry: ET.Element, tag: str) -> str:
    node = entry.find(f"{ATOM_NS}{tag}")
    if node is None:
        return ""
    return node.text or ""


def _entry_ref(entry: ET.Element) -> str:
    """The ref of an entry: its title, or the tag in its link."""
    title = _entry_text(entry, "title").strip()
    if title:
        return title
    for link in entry.findall(f"{ATOM_NS}link"):
        href = link.get("href") or ""
        if "/releases/tag/" in href:
            return href.rsplit("/", 1)[-1]
    return ""


def _is_release(entry: ET.Element) -> bool:
    """True when the entry points at a published release, not at a plain tag."""
    for link in entry.findall(f"{ATOM_NS}link"):
        if "/releases/" in (link.get("href") or ""):
            return True
    return "/releases/" in _entry_text(entry, "id")


def parse_atom(text: str) -> list[dict[str, Any]]:
    """The entries of a GitHub release feed.

    Each entry is a ref, a note and whether it is a published release.
    """
    try:
        root = ET.fromstring(text)
    except ET.ParseError as exc:
        raise UpdateError(f"the release feed is not valid XML: {exc}") from exc
    entries: list[dict[str, Any]] = []
    for entry in root.findall(f"{ATOM_NS}entry"):
        ref = _entry_ref(entry)
        if not ref:
            continue
        content = entry.find(f"{ATOM_NS}content")
        raw = (content.text or "") if content is not None else ""
        entries.append(
            {
                "ref": ref,
                "note": clean_note(raw),
                "release": _is_release(entry),
                "source": "feed",
            }
        )
    return entries


def parse_api_release(payload: Any) -> dict[str, Any]:
    """The published release from the GitHub API.

    The payload is the body of `/releases/latest`, as text or as a dict.
    """
    if isinstance(payload, (str, bytes)):
        try:
            payload = json.loads(payload)
        except (TypeError, ValueError) as exc:
            raise UpdateError(f"the release API did not return JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise UpdateError("the release API did not return an object")
    ref = str(payload.get("tag_name") or "").strip()
    assets = []
    for asset in payload.get("assets") or []:
        if not isinstance(asset, dict):
            continue
        assets.append(
            {
                "name": str(asset.get("name") or ""),
                "size": int(asset.get("size") or 0),
                "url": str(asset.get("browser_download_url") or asset.get("url") or ""),
            }
        )
    return {
        "ref": ref,
        "note": clean_note(str(payload.get("body") or "")),
        "release": not payload.get("draft"),
        "source": "api",
        "assets": assets,
    }


def fetch_candidates(repo: str = REPO) -> list[dict[str, Any]]:
    """Every candidate ref, from the feed first and the API second.

    The API is rate limited, so a failure there is not fatal while the feed
    worked, and a failure in the feed is not fatal while the API worked.
    """
    candidates: list[dict[str, Any]] = []
    problems: list[str] = []
    try:
        candidates.extend(parse_atom(fetch(feed_url(repo))))
    except UpdateError as exc:
        problems.append(str(exc))
    try:
        candidates.append(parse_api_release(fetch(api_url(repo))))
    except UpdateError as exc:
        problems.append(str(exc))
    if not candidates:
        raise UpdateError("; ".join(problems) or "no release found")
    return candidates


def latest(candidates: Sequence[dict[str, Any]]) -> Optional[dict[str, Any]]:
    """The best candidate: the highest version, a release over a plain tag."""
    best: Optional[dict[str, Any]] = None
    best_key: Optional[tuple] = None
    for candidate in candidates:
        key = version_key(str(candidate.get("ref") or ""))
        if key is None:
            continue
        full = key + (1 if candidate.get("release") else 0,)
        if best_key is None or full > best_key:
            best, best_key = candidate, full
    return best


# -- the installer registry -------------------------------------------------


def _registry_lines(path: Optional[str] = None) -> list[str]:
    target = path or paths.installed_registry()
    try:
        with open(target, "r", encoding="utf-8", errors="replace") as handle:
            return handle.read().splitlines()
    except OSError:
        return []


def _fields(line: str) -> list[str]:
    """The fields of a registry line, without the whitespace around them."""
    return [part.strip() for part in line.split("|")]


def read_installed(path: Optional[str] = None) -> dict[str, Any]:
    """What the installer recorded: the ref it installed and its version.

    The version comes from the ref, not from the code, because the code
    version is the same at every published tag. The third field of the line is
    the fallback, for an install from a ref that is not a version.

    A missing file or a missing line is normal: the install is untracked.
    """
    for line in _registry_lines(path):
        parts = _fields(line)
        if not parts or parts[0] != "version":
            continue
        ref = parts[1] if len(parts) > 1 else ""
        code = parts[2] if len(parts) > 2 else ""
        version = version_of(ref)
        if version is None and code and code != "unknown":
            version = code
        return {"ref": ref or None, "version": version, "tracked": True}
    return {"ref": None, "version": None, "tracked": False}


def read_harnesses(path: Optional[str] = None) -> list[str]:
    """The harness keys the installer recorded, in registry order."""
    names: list[str] = []
    for line in _registry_lines(path):
        parts = _fields(line)
        if not parts or not parts[0]:
            continue
        key = parts[0]
        if key in RESERVED_KEYS or key in names:
            continue
        names.append(key)
    return names


# -- the report -------------------------------------------------------------


def build_report(
    installed: dict[str, Any], newest: Optional[dict[str, Any]]
) -> dict[str, Any]:
    """The report the command prints: what is installed and what is out there."""
    latest_ref = str(newest.get("ref") or "") if newest else ""
    installed_version = installed.get("version")
    available = False
    if newest:
        if installed_version is None:
            # The install is untracked, so it cannot be told apart from an
            # older one: offer the newest.
            available = True
        else:
            here = version_key(str(installed_version))
            there = version_key(latest_ref)
            available = there is not None and (here is None or here < there)
    return {
        "installed_version": installed_version,
        "installed_ref": installed.get("ref"),
        "latest_version": version_of(latest_ref) if latest_ref else None,
        "latest_ref": latest_ref or None,
        "update_available": available,
        "note": str(newest.get("note") or "") if newest else "",
    }


# -- the install ------------------------------------------------------------


def target_args(harnesses: Sequence[str]) -> list[str]:
    """The installer arguments for the harnesses the registry names."""
    if not harnesses:
        return []
    return ["--target", ",".join(harnesses)]


def install_command(repo: str, ref: str, harnesses: Sequence[str]) -> str:
    """The install as the user can copy and paste it."""
    args = " ".join(target_args(harnesses))
    command = f"curl -fsSL {script_url(repo, ref)} | AJQ_VERSION={ref} bash -s --"
    return f"{command} {args}" if args else command


def install(repo: str, ref: str, harnesses: Sequence[str]) -> int:
    """Run the installer from the ref and return its exit code.

    The script comes from the raw URL at the ref and `AJQ_VERSION` pins the
    payload fetch to the same ref, so the script and the payload are one ref.
    """
    script = fetch(script_url(repo, ref))
    env = dict(os.environ)
    env["AJQ_VERSION"] = ref
    argv = ["bash", "-s", "--"] + target_args(harnesses)
    try:
        result = subprocess.run(argv, input=script, env=env, text=True)
    except FileNotFoundError as exc:
        raise UpdateError("bash is not on PATH") from exc
    except OSError as exc:
        raise UpdateError(f"cannot run the installer: {exc}") from exc
    code = int(getattr(result, "returncode", 0) or 0)
    if code != 0:
        raise UpdateError(f"the installer failed with exit code {code}")
    return code
