# astern.timeledger

Time ledger: where the owner’s time and the agents’ time went, per bucket, over a window.

Three steps, each usable alone:

- [`activity()`](#astern.timeledger.activity) reads transcripts (any homes, so several machines) and returns, per
  top-level session, the agent’s **active seconds** as runs of `[cwd, day, seconds]`, and
  every **owner prompt** (time, cwd, length). Active time is the sum of gaps between
  consecutive events, each gap counted only up to `idle` seconds. An owner prompt is a
  `user` line the transcript marks `origin.kind == "human"` (typed by a person, locally
  or by remote control), minus briefs another agent sent that way (`dispatch_markers`).
  Events are deduplicated by `(sessionId, uuid)` across files: a session whose repository
  moved leaves a second transcript copy holding the same events.
- [`commits()`](#astern.timeledger.commits) reads `git log` of the repositories under a root and classes each
  commit `bot`, `others` (not the owner’s identity: collaborators in shared repos),
  `agent` (a Claude trailer or session link in the message), else `unmarked` (agents
  commit under the owner’s identity too, so unmarked is not proof of a hand commit).
  Dated by author date. `git log --all` overstates distinct changes: a branch commit later
  repeated by a squash merge counts twice.
- [`rollup()`](#astern.timeledger.rollup) groups both by an injected `bucket(cwd) -> str | None` (see
  [`prefix_bucket()`](#astern.timeledger.prefix_bucket)). `None` means “outside every bucket”: that time counts for the
  > session’s previous bucket (a scratchpad in `/tmp` belongs to the project that used it).

  Owner time is credited per prompt as `min(W, minutes since the previous owner prompt on
  any machine)`, so parallel sessions never double-count one person; `W` is reported for
  each of `windows` (5 minutes, with 2 and 10 as the sensitivity range).

Agent hours are session-hours, not wall-clock: parallel sessions add up. Nothing here reads
prompt text beyond its length and the dispatch markers, and nothing is written anywhere.

```pycon
>>> bucket = prefix_bucket([("t/astern", "astern"), ("t/", "tools")], root="/p")
>>> bucket("/p/t/astern/docs"), bucket("/p/t/ir"), bucket("/p/misc/x"), bucket("/tmp/s")
('astern', 'tools', 'unmapped', None)
```

### Module Attributes

| [`DISPATCH_MARKERS`](#astern.timeledger.DISPATCH_MARKERS)   | Text that marks a prompt as a brief another agent wrote (sent through a human-kind prompt, as a spawn does): the fleet's reply contract.   |
|---------------------------------------------------------------------|--------------------------------------------------------------------------------------------------------------------------------------------|
| [`UNMAPPED`](#astern.timeledger.UNMAPPED)           | The bucket of a cwd that is inside the root but matches no rule.                                                                           |
| [`OTHER`](#astern.timeledger.OTHER)              | The bucket of time no rule and no previous bucket claims.                                                                                  |
| [`DFLT_EXCLUDE`](#astern.timeledger.DFLT_EXCLUDE)       | other people's histories are not the owner's work.                                                                                         |

### Functions

| [`activity`](#astern.timeledger.activity)([home, until, idle, ...])              | Agent active time and owner prompts from the transcripts of `home` (see [`astern.sources.homes()`](astern.sources.html.md#astern.sources.homes); `"all"` covers every account and mirrored machine).                               |
|--------------------------------------------------------------------------------------------------|---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| [`commit_kind`](#astern.timeledger.commit_kind)(email, name, body, \*, owner)       | `bot`, `others`, `agent` or `unmarked`, in that order of precedence.                                                                                                                                                                                    |
| [`commits`](#astern.timeledger.commits)(root, \*, since[, until, exclude, ...]) | Commits by author date in `[since, until)` across the repositories under `root`.                                                                                                                                                                        |
| [`is_owner_prompt`](#astern.timeledger.is_owner_prompt)(record)                         | A prompt a person typed: `origin.kind == "human"` (not a peer message or a notification).                                                                                                                                                               |
| [`merge`](#astern.timeledger.merge)(\*found)                                  | Several [`activity()`](#astern.timeledger.activity) results (one per machine, e.g. run over ssh) as one.                                                                                                                                |
| [`prefix_bucket`](#astern.timeledger.prefix_bucket)(rules, \*, root[, root_bucket])   | A bucket map from `(prefix, bucket)` rules, prefixes relative to `root`; first match wins.                                                                                                                                                              |
| [`project_bucket`](#astern.timeledger.project_bucket)(cwd)                             | The default bucket: the cwd's own folder name (one bucket per project folder).                                                                                                                                                                          |
| [`rollup`](#astern.timeledger.rollup)(found[, found_commits, bucket, ...])     | One row per bucket from [`activity()`](#astern.timeledger.activity)'s result (`found`, or several merged with [`merge()`](#astern.timeledger.merge)) and [`commits()`](#astern.timeledger.commits). |

### astern.timeledger.DFLT_EXCLUDE *= ('forks/',)*

other people’s histories are not the owner’s work.

* **Type:**
  Repositories skipped by default

### astern.timeledger.DISPATCH_MARKERS *= ('Reply in at most five lines',)*

Text that marks a prompt as a brief another agent wrote (sent through a human-kind prompt,
as a spawn does): the fleet’s reply contract. Pass your own with `dispatch_markers=`.

### astern.timeledger.OTHER *= 'other'*

The bucket of time no rule and no previous bucket claims.

### astern.timeledger.UNMAPPED *= 'unmapped'*

The bucket of a cwd that is inside the root but matches no rule.

### astern.timeledger.activity(home=None, , since, until=None, idle=300, dispatch_markers=('Reply in at most five lines',), files=None)

Agent active time and owner prompts from the transcripts of `home` (see
[`astern.sources.homes()`](astern.sources.html.md#astern.sources.homes); `"all"` covers every account and mirrored machine).

`since` / `until` are ISO dates or times (UTC; `until` exclusive). `files` overrides
the transcripts read (tests pass fixtures). Returns `{"since", "until", "sessions",
"prompts"}`: a session is `{"session", "home", "active": [[cwd, day, seconds], ...]}`
with consecutive events in the same cwd and day merged into one run, in order; a prompt is
`{"ts", "cwd", "chars", "session", "dispatched"}`.

* **Return type:**
  [`dict`](https://docs.python.org/3/builtins/stdtypes.html#dict)

### astern.timeledger.commit_kind(email, name, body, , owner)

`bot`, `others`, `agent` or `unmarked`, in that order of precedence.

```pycon
>>> me = re.compile("ada", re.IGNORECASE)
>>> commit_kind("ci@x", "github-actions[bot]", "", owner=me)
'bot'
>>> commit_kind("bob@x", "Bob", "Claude-Session: x", owner=me)
'others'
>>> commit_kind("ada@x", "Ada", "fix\n\nCo-Authored-By: Claude <noreply@anthropic.com>", owner=me)
'agent'
```

* **Return type:**
  [`str`](https://docs.python.org/3/builtins/stdtypes.html#str)

### astern.timeledger.commits(root, \*, since, until=None, owner, exclude=('forks/', ), depth=3, timeout=60, run=<function run>)

Commits by author date in `[since, until)` across the repositories under `root`.

`owner` is a regex (or a pattern) matched against the author’s email and name.
Repositories whose path relative to `root` starts with one of `exclude` are skipped.
Each commit is `{"hash", "date", "repo", "path", "kind"}`, once per hash.

* **Return type:**
  [`list`](https://docs.python.org/3/builtins/stdtypes.html#list)[[`dict`](https://docs.python.org/3/builtins/stdtypes.html#dict)]

### astern.timeledger.is_owner_prompt(record)

A prompt a person typed: `origin.kind == "human"` (not a peer message or a notification).

```pycon
>>> is_owner_prompt({"type": "user", "origin": {"kind": "human"}, "message": {"content": "hi"}})
True
>>> is_owner_prompt({"type": "user", "origin": {"kind": "peer"}, "message": {"content": "hi"}})
False
```

* **Return type:**
  [`bool`](https://docs.python.org/3/builtins/functions.html#bool)

### astern.timeledger.merge(\*found)

Several [`activity()`](#astern.timeledger.activity) results (one per machine, e.g. run over ssh) as one.

Prompts seen twice (same session and time) are kept once.

* **Return type:**
  [`dict`](https://docs.python.org/3/builtins/stdtypes.html#dict)

### astern.timeledger.prefix_bucket(rules, , root, root_bucket='unmapped')

A bucket map from `(prefix, bucket)` rules, prefixes relative to `root`; first match wins.

A cwd outside `root` (or none) maps to `None`; `root` itself to `root_bucket`; a
cwd inside it that no rule matches to [`UNMAPPED`](#astern.timeledger.UNMAPPED). Put longer prefixes first.
A rule matches whole path parts: `t/ast` does not match `t/astern`.

```pycon
>>> b = prefix_bucket([["a/b", "B"]], root="/r", root_bucket="lookout")
>>> b("/r"), b("/r/a/b/c"), b("/r/a/bc"), b(None)
('lookout', 'B', 'unmapped', None)
```

* **Return type:**
  [`Callable`](https://docs.python.org/3/library/collections.abc.html#collections.abc.Callable)[[[`str`](https://docs.python.org/3/builtins/stdtypes.html#str) | [`None`](https://docs.python.org/3/builtins/constants.html#None)], [`str`](https://docs.python.org/3/builtins/stdtypes.html#str) | [`None`](https://docs.python.org/3/builtins/constants.html#None)]

### astern.timeledger.project_bucket(cwd)

The default bucket: the cwd’s own folder name (one bucket per project folder).

```pycon
>>> project_bucket("/home/me/proj/astern"), project_bucket(None)
('astern', None)
```

* **Return type:**
  [`str`](https://docs.python.org/3/builtins/stdtypes.html#str) | [`None`](https://docs.python.org/3/builtins/constants.html#None)

### astern.timeledger.rollup(found, found_commits=(), \*, bucket=<function project_bucket>, windows=(2, 5, 10), weeks=None)

One row per bucket from [`activity()`](#astern.timeledger.activity)’s result (`found`, or several merged with
[`merge()`](#astern.timeledger.merge)) and [`commits()`](#astern.timeledger.commits).

`weeks` divides the per-week columns (default: the window’s length, from `since` to
`until` or the last event).

* **Return type:**
  [`dict`](https://docs.python.org/3/builtins/stdtypes.html#dict)
