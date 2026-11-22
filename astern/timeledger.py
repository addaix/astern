"""Time ledger: where the owner's time and the agents' time went, per bucket, over a window.

Three steps, each usable alone:

- :func:`activity` reads transcripts (any homes, so several machines) and returns, per
  top-level session, the agent's **active seconds** as runs of ``[cwd, day, seconds]``, and
  every **owner prompt** (time, cwd, length). Active time is the sum of gaps between
  consecutive events, each gap counted only up to ``idle`` seconds. An owner prompt is a
  ``user`` line the transcript marks ``origin.kind == "human"`` (typed by a person, locally
  or by remote control), minus briefs another agent sent that way (``dispatch_markers``).
  Events are deduplicated by ``(sessionId, uuid)`` across files: a session whose repository
  moved leaves a second transcript copy holding the same events.
- :func:`commits` reads ``git log`` of the repositories under a root and classes each
  commit ``bot``, ``others`` (not the owner's identity: collaborators in shared repos),
  ``agent`` (a Claude trailer or session link in the message), else ``unmarked`` (agents
  commit under the owner's identity too, so unmarked is not proof of a hand commit).
  Dated by author date. ``git log --all`` overstates distinct changes: a branch commit later
  repeated by a squash merge counts twice.
- :func:`rollup` groups both by an injected ``bucket(cwd) -> str | None`` (see
  :func:`prefix_bucket`). ``None`` means "outside every bucket": that time counts for the
  session's previous bucket (a scratchpad in ``/tmp`` belongs to the project that used it).
  Owner time is credited per prompt as ``min(W, minutes since the previous owner prompt on
  any machine)``, so parallel sessions never double-count one person; ``W`` is reported for
  each of ``windows`` (5 minutes, with 2 and 10 as the sensitivity range).

Agent hours are session-hours, not wall-clock: parallel sessions add up. Nothing here reads
prompt text beyond its length and the dispatch markers, and nothing is written anywhere.

>>> bucket = prefix_bucket([("t/astern", "astern"), ("t/", "tools")], root="/p")
>>> bucket("/p/t/astern/docs"), bucket("/p/t/ir"), bucket("/p/misc/x"), bucket("/tmp/s")
('astern', 'tools', 'unmapped', None)
"""

from __future__ import annotations

import os
import re
import subprocess
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable, Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path, PurePath

from astern.sources import iter_session_files, load_records

IDLE_SECONDS = 300
OWNER_WINDOWS_MIN = (2, 5, 10)
DFLT_OWNER_WINDOW = 5
#: Text that marks a prompt as a brief another agent wrote (sent through a human-kind prompt,
#: as a spawn does): the fleet's reply contract. Pass your own with ``dispatch_markers=``.
DISPATCH_MARKERS = ("Reply in at most five lines",)
AGENT_TRAILER = re.compile(
    r"Claude-Session:|claude\.ai/code/session_|Co-Authored-By:\s*Claude|"
    r"Generated with \[Claude Code\]|noreply@anthropic\.com",
    re.IGNORECASE,
)
BOT_AUTHOR = re.compile(r"\[bot\]|github-actions", re.IGNORECASE)
#: The bucket of a cwd that is inside the root but matches no rule.
UNMAPPED = "unmapped"
#: The bucket of time no rule and no previous bucket claims.
OTHER = "other"
#: Repositories skipped by default: other people's histories are not the owner's work.
DFLT_EXCLUDE = ("forks/",)
_GIT_FORMAT = "%H%x1f%aI%x1f%ae%x1f%an%x1f%B%x1e"
COMMIT_KINDS = ("bot", "others", "agent", "unmarked")

Bucket = Callable[[str | None], "str | None"]


def _ts(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def _when(iso: str) -> datetime:
    """An ISO date or time as an aware UTC datetime (a date is its midnight).

    >>> _when("2026-11-01").isoformat()
    '2026-11-01T00:00:00+00:00'
    """
    t = _ts(iso if "T" in iso else iso + "T00:00:00+00:00")
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


def _day(iso: str) -> str:
    return iso[:10]


def prefix_bucket(
    rules: Iterable[Sequence[str]], *, root: str | os.PathLike, root_bucket: str = UNMAPPED
) -> Bucket:
    """A bucket map from ``(prefix, bucket)`` rules, prefixes relative to ``root``; first match wins.

    A cwd outside ``root`` (or none) maps to ``None``; ``root`` itself to ``root_bucket``; a
    cwd inside it that no rule matches to :data:`UNMAPPED`. Put longer prefixes first.
    A rule matches whole path parts: ``t/ast`` does not match ``t/astern``.

    >>> b = prefix_bucket([["a/b", "B"]], root="/r", root_bucket="lookout")
    >>> b("/r"), b("/r/a/b/c"), b("/r/a/bc"), b(None)
    ('lookout', 'B', 'unmapped', None)
    """
    base = PurePath(root).as_posix().rstrip("/") + "/"
    pairs = [(str(p).strip("/") + "/", str(b)) for p, b in rules if str(p).strip("/")]

    def bucket(cwd: str | None) -> str | None:
        if not cwd:
            return None
        path = PurePath(str(cwd)).as_posix().rstrip("/") + "/"
        if path == base:
            return root_bucket
        if not path.startswith(base):
            return None
        rel = path[len(base) :]
        return next((b for p, b in pairs if rel.startswith(p)), UNMAPPED)

    return bucket


def project_bucket(cwd: str | None) -> str | None:
    """The default bucket: the cwd's own folder name (one bucket per project folder).

    >>> project_bucket("/home/me/proj/astern"), project_bucket(None)
    ('astern', None)
    """
    return Path(str(cwd)).name or None if cwd else None


def _prompt_text(record: Mapping) -> str | None:
    """A user line's text, or ``None`` for a tool result."""
    content = (record.get("message") or {}).get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        if any(isinstance(x, dict) and x.get("type") == "tool_result" for x in content):
            return None
        return " ".join(str(x.get("text", "")) for x in content if isinstance(x, dict))
    return None


def is_owner_prompt(record: Mapping) -> bool:
    """A prompt a person typed: ``origin.kind == "human"`` (not a peer message or a notification).

    >>> is_owner_prompt({"type": "user", "origin": {"kind": "human"}, "message": {"content": "hi"}})
    True
    >>> is_owner_prompt({"type": "user", "origin": {"kind": "peer"}, "message": {"content": "hi"}})
    False
    """
    if record.get("type") != "user" or record.get("isMeta"):
        return False
    if (record.get("origin") or {}).get("kind") != "human":
        return False
    return _prompt_text(record) is not None


def _in_window(iso: str, since: str, until: str | None) -> bool:
    return iso[: len(since)] >= since and (until is None or iso[: len(until)] < until)


def activity(
    home=None,
    *,
    since: str,
    until: str | None = None,
    idle: float = IDLE_SECONDS,
    dispatch_markers: Sequence[str] = DISPATCH_MARKERS,
    files: Iterable | None = None,
) -> dict:
    """Agent active time and owner prompts from the transcripts of ``home`` (see
    :func:`astern.sources.homes`; ``"all"`` covers every account and mirrored machine).

    ``since`` / ``until`` are ISO dates or times (UTC; ``until`` exclusive). ``files`` overrides
    the transcripts read (tests pass fixtures). Returns ``{"since", "until", "sessions",
    "prompts"}``: a session is ``{"session", "home", "active": [[cwd, day, seconds], ...]}``
    with consecutive events in the same cwd and day merged into one run, in order; a prompt is
    ``{"ts", "cwd", "chars", "session", "dispatched"}``.
    """
    if files is None:
        days = (datetime.now(timezone.utc) - _when(since)).total_seconds() / 86400 + 1
        files = iter_session_files(home, since_days=max(days, 1))
    seen: set[tuple] = set()
    sessions, prompts = [], []
    for sf in files:
        events = []
        for d in load_records(sf.path):
            ts = d.get("timestamp")
            if not isinstance(ts, str) or d.get("type") not in ("user", "assistant", "system"):
                continue
            if not _in_window(ts, since, until):
                continue
            key = (d.get("sessionId"), d.get("uuid"))
            if key[1]:
                if key in seen:
                    continue
                seen.add(key)
            events.append((ts, d))
        if not events:
            continue
        events.sort(key=lambda e: e[0])
        runs: list[list] = []
        prev = None
        for ts, d in events:
            t, cwd = _ts(ts), d.get("cwd")
            gap = (t - prev).total_seconds() if prev is not None else None
            prev = t
            if gap is not None and 0 <= gap <= idle:
                if runs and runs[-1][0] == cwd and runs[-1][1] == _day(ts):
                    runs[-1][2] += gap
                else:
                    runs.append([cwd, _day(ts), gap])
            if is_owner_prompt(d):
                text = _prompt_text(d) or ""
                prompts.append({
                    "ts": ts, "cwd": cwd, "chars": len(text), "session": sf.session_id,
                    "dispatched": any(m in text for m in dispatch_markers),
                })  # fmt: skip
        sessions.append({
            "session": sf.session_id, "home": sf.home,
            "active": [[c, day, round(s, 1)] for c, day, s in runs],
        })  # fmt: skip
    return {"since": since, "until": until, "sessions": sessions, "prompts": prompts}


def _repos(root: Path, *, depth: int) -> Iterable[Path]:
    """Git repositories under ``root``, at most ``depth`` levels down (not inside one another)."""
    stack = [(root, 0)]
    while stack:
        d, level = stack.pop()
        if (d / ".git").exists():
            yield d
            continue
        if level >= depth:
            continue
        try:
            subs = sorted(p for p in d.iterdir() if p.is_dir() and not p.name.startswith("."))
        except OSError:
            continue
        stack.extend((p, level + 1) for p in reversed(subs))


def commit_kind(email: str, name: str, body: str, *, owner: re.Pattern) -> str:
    """``bot``, ``others``, ``agent`` or ``unmarked``, in that order of precedence.

    >>> me = re.compile("ada", re.IGNORECASE)
    >>> commit_kind("ci@x", "github-actions[bot]", "", owner=me)
    'bot'
    >>> commit_kind("bob@x", "Bob", "Claude-Session: x", owner=me)
    'others'
    >>> commit_kind("ada@x", "Ada", "fix\\n\\nCo-Authored-By: Claude <noreply@anthropic.com>", owner=me)
    'agent'
    """
    if BOT_AUTHOR.search(email) or BOT_AUTHOR.search(name):
        return "bot"
    if not (owner.search(email) or owner.search(name)):
        return "others"
    return "agent" if AGENT_TRAILER.search(body) else "unmarked"


def commits(
    root: str | os.PathLike,
    *,
    since: str,
    until: str | None = None,
    owner: str | re.Pattern,
    exclude: Sequence[str] = DFLT_EXCLUDE,
    depth: int = 3,
    timeout: float = 60,
    run: Callable[..., subprocess.CompletedProcess] = subprocess.run,
) -> list[dict]:
    """Commits by author date in ``[since, until)`` across the repositories under ``root``.

    ``owner`` is a regex (or a pattern) matched against the author's email and name.
    Repositories whose path relative to ``root`` starts with one of ``exclude`` are skipped.
    Each commit is ``{"hash", "date", "repo", "path", "kind"}``, once per hash.
    """
    root = Path(root).expanduser()
    owner = re.compile(owner, re.IGNORECASE) if isinstance(owner, str) else owner
    out: dict[str, dict] = {}
    for repo in _repos(root, depth=depth):
        rel = repo.relative_to(root).as_posix()
        if any(rel.startswith(x) for x in exclude):
            continue
        try:
            done = run(
                ["git", "-C", str(repo), "log", "--all", f"--since={since}", f"--format={_GIT_FORMAT}"],
                capture_output=True, text=True, timeout=timeout, check=False,
            )  # fmt: skip
        except subprocess.TimeoutExpired:
            continue
        for rec in (done.stdout or "").split("\x1e"):
            parts = rec.strip("\n").split("\x1f")
            if len(parts) < 5 or not _in_window(parts[1], since, until):
                continue
            h, date, email, name, body = parts[:5]
            out.setdefault(h, {"hash": h, "date": date, "repo": rel, "path": str(repo),
                               "kind": commit_kind(email, name, body, owner=owner)})  # fmt: skip
    return list(out.values())


def rollup(
    found: Mapping,
    found_commits: Iterable[Mapping] = (),
    *,
    bucket: Bucket = project_bucket,
    windows: Sequence[float] = OWNER_WINDOWS_MIN,
    weeks: float | None = None,
) -> dict:
    """One row per bucket from :func:`activity`'s result (``found``, or several merged with
    :func:`merge`) and :func:`commits`.

    ``weeks`` divides the per-week columns (default: the window's length, from ``since`` to
    ``until`` or the last event).
    """
    agent_s: Counter = Counter()
    agent_sessions, agent_days = defaultdict(set), defaultdict(set)
    for s in found["sessions"]:
        last = None
        for cwd, day, sec in s["active"]:
            b = bucket(cwd)
            b, last = (b, b) if b is not None else (last or OTHER, last)
            agent_s[b] += sec
            if sec >= 60:
                agent_sessions[b].add(s["session"])
                agent_days[b].add(day)
    prompts = sorted(found["prompts"], key=lambda p: p["ts"])
    owner = [p for p in prompts if not p["dispatched"]]
    dispatched = Counter(bucket(p["cwd"]) or OTHER for p in prompts if p["dispatched"])
    owner_min = {w: Counter() for w in windows}
    owner_n, owner_days = Counter(), defaultdict(set)
    prev = None
    for p in owner:
        t = _ts(p["ts"])
        gap = (t - prev).total_seconds() / 60 if prev is not None else float("inf")
        b = bucket(p["cwd"]) or OTHER
        for w in windows:
            owner_min[w][b] += min(w, gap)
        owner_n[b] += 1
        owner_days[b].add(_day(p["ts"]))
        prev = t
    ckind: dict[str, Counter] = defaultdict(Counter)
    found_commits = list(found_commits)
    for c in found_commits:
        ckind[bucket(c["path"]) or OTHER][c["kind"]] += 1
    if weeks is None:
        end = found.get("until") or (prompts[-1]["ts"] if prompts else None)
        end = _when(end) if end else datetime.now(timezone.utc)
        weeks = max((end - _when(found["since"])).total_seconds() / (7 * 86400), 1 / 7)
    main_w = DFLT_OWNER_WINDOW if DFLT_OWNER_WINDOW in owner_min else windows[0]
    total_agent = sum(agent_s.values())
    total_owner = sum(owner_min[main_w].values())
    rows = []
    for b in sorted(set(agent_s) | set(owner_n) | set(ckind), key=lambda b: (-agent_s[b], b)):
        rows.append({
            "bucket": b,
            "agent_hours": round(agent_s[b] / 3600, 2),
            "agent_hours_per_week": round(agent_s[b] / 3600 / weeks, 2),
            "agent_share": round(agent_s[b] / total_agent, 3) if total_agent else None,
            "agent_sessions": len(agent_sessions[b]),
            "agent_days": len(agent_days[b]),
            "owner_prompts": owner_n[b],
            "owner_days": len(owner_days[b]),
            "owner_hours": {str(w): round(owner_min[w][b] / 60, 2) for w in windows},
            "owner_share": round(owner_min[main_w][b] / total_owner, 3) if total_owner else None,
            "dispatched_briefs": dispatched[b],
            "commits": {k: ckind[b][k] for k in COMMIT_KINDS if ckind[b][k]},
        })  # fmt: skip
    return {
        "since": found["since"], "until": found.get("until"), "weeks": round(weeks, 2),
        "owner_window_min": main_w,
        "totals": {
            "agent_hours": round(total_agent / 3600, 2),
            "owner_hours": {str(w): round(sum(owner_min[w].values()) / 60, 2) for w in windows},
            "owner_prompts": len(owner), "dispatched_briefs": sum(dispatched.values()),
            "commits": dict(Counter(c["kind"] for c in found_commits)),
        },
        "rows": rows,
    }  # fmt: skip


def merge(*found: Mapping) -> dict:
    """Several :func:`activity` results (one per machine, e.g. run over ssh) as one.

    Prompts seen twice (same session and time) are kept once.
    """
    if not found:
        raise ValueError("nothing to merge")
    seen, prompts = set(), []
    for f in found:
        for p in f["prompts"]:
            if (p["session"], p["ts"]) not in seen:
                seen.add((p["session"], p["ts"]))
                prompts.append(p)
    return {
        "since": min(f["since"] for f in found),
        "until": found[0].get("until"),
        "sessions": [s for f in found for s in f["sessions"]],
        "prompts": prompts,
    }
