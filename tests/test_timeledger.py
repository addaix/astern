"""The time ledger: owner vs agent time and commits per bucket (addaix/astern#16)."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from astern import timeledger as tl
from astern import tools

ROOT = "/p"


def _ev(i, ts, cwd, *, kind="assistant", origin=None, text="x", sid="s1"):
    d = {"type": kind, "timestamp": ts, "cwd": cwd, "sessionId": sid, "uuid": f"{sid}-{i}",
         "message": {"content": text}}  # fmt: skip
    if origin:
        d["origin"] = {"kind": origin}
    return d


def _write(home: Path, slug: str, sid: str, records) -> None:
    folder = home / "projects" / slug
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{sid}.jsonl").write_text("\n".join(json.dumps(r) for r in records) + "\n")


def _home(tmp_path):
    home = tmp_path / ".claude"
    s1 = [
        _ev(
            0,
            "2026-11-10T10:00:00Z",
            "/p/t/astern",
            kind="user",
            origin="human",
            text="fix the parser",
        ),
        _ev(1, "2026-11-10T10:01:00Z", "/p/t/astern"),
        _ev(2, "2026-11-10T10:03:00Z", "/tmp/scratch"),  # outside the root: astern's time
        _ev(3, "2026-11-10T11:00:00Z", "/p/t/astern"),  # an hour later: idle, not counted
        _ev(
            4,
            "2026-11-10T11:00:30Z",
            "/p/t/astern",
            kind="user",
            origin="peer",
            text="status?",
        ),
    ]
    s2 = [
        _ev(0, "2026-11-10T10:02:00Z", "/p/i/dol", kind="user", origin="human", text="go", sid="s2"),
        _ev(1, "2026-11-10T10:04:00Z", "/p/i/dol", sid="s2"),
        _ev(2, "2026-11-10T10:20:00Z", "/p/i/dol", kind="user", origin="human",
            text="You are w. Reply in at most five lines.", sid="s2"),
    ]  # fmt: skip
    _write(home, "-p-t-astern", "s1", s1)
    _write(home, "-p-i-dol", "s2", s2)
    _write(home, "-p-t-astern-moved", "s1", s1)  # a moved repo's second copy: same events
    return home


def test_activity_dedupes_caps_idle_and_marks_owner_prompts(tmp_path):
    found = tl.activity(_home(tmp_path), since="2026-11-10")
    by = {s["session"]: s for s in found["sessions"]}
    assert set(by) == {"s1", "s2"}
    assert by["s1"]["active"] == [["/p/t/astern", "2026-11-10", 60.0], ["/tmp/scratch", "2026-11-10", 120.0],
                                  ["/p/t/astern", "2026-11-10", 30.0]]  # fmt: skip
    owner = [(p["session"], p["dispatched"]) for p in found["prompts"]]
    assert owner == [
        ("s1", False),
        ("s2", False),
        ("s2", True),
    ]  # the peer message is not the owner's


def test_rollup_buckets_by_the_callers_map_and_never_double_counts_the_owner(tmp_path):
    found = tl.activity(_home(tmp_path), since="2026-11-10")
    bucket = tl.prefix_bucket([["t/astern", "astern"], ["i/", "i2mint"]], root=ROOT)
    made = [
        {
            "hash": "h1",
            "date": "2026-11-10T09:00:00Z",
            "repo": "t/astern",
            "path": "/p/t/astern",
            "kind": "agent",
        }
    ]
    out = tl.rollup(found, made, bucket=bucket, windows=(2, 5), weeks=1)
    rows = {r["bucket"]: r for r in out["rows"]}
    assert rows["astern"]["agent_hours"] == round(
        210 / 3600, 2
    )  # the scratchpad counts for astern
    assert (
        rows["i2mint"]["dispatched_briefs"] == 1 and rows["i2mint"]["owner_prompts"] == 1
    )
    # the owner typed at 10:00 (astern) and 10:02 (dol): the second is credited 2 minutes, not 5
    assert rows["i2mint"]["owner_hours"] == {"2": round(2 / 60, 2), "5": round(2 / 60, 2)}
    assert rows["astern"]["owner_hours"]["5"] == round(5 / 60, 2)
    assert (
        rows["astern"]["commits"] == {"agent": 1} and out["totals"]["owner_prompts"] == 2
    )


def test_commits_class_and_dedupe_by_hash(tmp_path):
    for name in ("t/a", "forks/b", "t/c"):
        (tmp_path / name / ".git").mkdir(parents=True)
    log = "\x1e".join([
        "h1\x1f2026-11-10T10:00:00+00:00\x1fada@x\x1fAda\x1ffix\n\nClaude-Session: https://claude.ai/code/session_x",
        "h2\x1f2026-11-11T10:00:00+00:00\x1fbob@x\x1fBob\x1fmine",
        "h3\x1f2026-10-01T10:00:00+00:00\x1fada@x\x1fAda\x1fold",
    ]) + "\x1e"  # fmt: skip
    calls = []

    def run(argv, **kw):
        calls.append(argv[2])
        return subprocess.CompletedProcess(argv, 0, stdout=log, stderr="")

    got = tl.commits(tmp_path, since="2026-11-01", owner="ada", run=run)
    assert sorted(Path(c).name for c in calls) == ["a", "c"]  # forks skipped
    assert {c["hash"]: c["kind"] for c in got} == {"h1": "agent", "h2": "others"}


def test_the_time_ledger_tool(tmp_path):
    rules = tmp_path / "map.json"
    rules.write_text(json.dumps({"rules": [["t/", "tools"]]}))
    out = tools.time_ledger(
        "2026-11-10",
        home=str(_home(tmp_path)),
        root=ROOT,
        buckets=str(rules),
        with_commits=False,
    )
    assert {r["bucket"] for r in out["rows"]} == {"tools", "unmapped"}
    assert "time_ledger" in {f.__name__ for f in tools._dispatch_funcs}
