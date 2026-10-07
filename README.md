# pr-watch

[![Tests](https://github.com/sebmellen/pr-watch/actions/workflows/tests.yml/badge.svg)](https://github.com/sebmellen/pr-watch/actions/workflows/tests.yml)

A shared PR watcher for any shell or MCP client, including Claude Code, Codex,
Grok, OpenCode, and Cursor. Both interfaces share an engine, database, and workers.
Waiting makes no model API calls. GitHub access uses `gh` and is read-only.

Start a watch in one agent and consume its result in another. A local worker handles
polling, backoff, revision checks, and persistence; the agent handles any fixes.

## Install

Requires Python 3.10+, GitHub CLI, and Linux/macOS (POSIX process/file locking).
The CLI has no Python dependencies. MCP is an optional pinned extra.

```sh
uv tool install 'pr-watch[mcp] @ git+https://github.com/sebmellen/pr-watch.git'
gh auth login
pr-watch --help
```

Install [uv](https://docs.astral.sh/uv/getting-started/installation/) and
[GitHub CLI](https://cli.github.com/) first if needed. Omit `[mcp]` for CLI-only use.
The installation command uses this repository directly; it does not depend on a
PyPI release. `uv tool update-shell` adds the executable directory to your PATH.
Use `--host HOSTNAME` to watch a GitHub Enterprise host authenticated in `gh`.

## Use

```sh
# Start once: prints JSON containing the durable watch ID.
pr-watch watch --repo OWNER/REPO --pr 123

# Wait once: no progress output or model-driven polling.
pr-watch wait WATCH_ID

# Watch an explicit set when branch protection does not declare required checks.
pr-watch watch --repo OWNER/REPO --pr 123 --checks named \
  --check 'test (api)' --check 'integration testing in docker compose'

# Review cycle: use the timestamp recorded BEFORE requesting that review.
# This tool does not request reviews, post comments, or authorize those actions.
pr-watch watch --repo OWNER/REPO --pr 123 --checks none \
  --reviewer copilot --review-after 2026-10-07T10:00:00Z

pr-watch status WATCH_ID
pr-watch list --active
pr-watch cancel WATCH_ID
pr-watch resume WATCH_ID
```

Combine checks and reviewers to require both; check failures return immediately.
Review receipts include COMMENTED and CHANGES_REQUESTED, not just approvals.
The consuming agent must inspect review bodies and inline comments.

`--head FULL_SHA` pins a supplied head; otherwise creation pins the current head.
A new head or base ends the watch; the consumer decides whether to start again.
`--timeout` bounds the subscription (default one hour, maximum seven days).
`--interval` sets GitHub polling (default 30 seconds, minimum five).
`wait --timeout N` bounds only the caller's wait: a `waiting` result does not
cancel the subscription. Add `--wait` to `watch` to create and wait in one call.
CLI exit 0 means valid JSON, **not** CI success: inspect `state` and `terminal`.
Invalid requests exit nonzero; interrupted wait exits 130 and leaves the worker running.

## Contract

Results include `id`, `state`, `terminal`, `request`, `observed_at`, and `last_error`.
Observations identify the PR revision, check sources/results, and fresh reviews.

| State | Meaning |
|---|---|
| `waiting` | At least one condition is pending, absent, or temporarily unreadable |
| `ready` | Selected checks and any selected review receipts meet the criteria |
| `review_received` | Every selected reviewer submitted a matching fresh review |
| `checks_failed` | A selected check failed or had a disallowed conclusion |
| `head_changed`, `base_changed` | The pinned revision is no longer current |
| `merged`, `closed` | The PR left the open state |
| `timed_out` | The subscription deadline elapsed; last evidence/error is retained |
| `error` | Permanent transport/permission/configuration error, or five consecutive transient errors |
| `cancelled` | Local subscription cancelled; no GitHub action was cancelled |

`ready` refers only to these criteria. It is not a merge authorization or a claim
that every GitHub merge gate (approvals, deployments, merge queue, etc.) is met.

- Required checks are the union of classic branch protection and active rulesets.
  Empty policy is an explicit error; select named checks if that is intended.
- Missing checks stay pending. Names are matched exactly, including whitespace.
  GitHub App source requirements are checked. A same-named check and commit status
  must both pass. Legacy statuses do not expose an App ID in this API; a policy
  requiring a specific App stays missing unless that source can be verified by a
  matching check run. It never silently falls back to an unverified status.
- Test-merge checks take precedence over head checks when present. Merge parents
  and PR identity are checked to avoid attributing stale results to a new push.
- Required GitHub Actions checks must come from a PR-eligible workflow event;
  an unrelated manual workflow run cannot satisfy the policy. Named watches may
  intentionally watch a manually triggered job.
- Success is strict by default. `--allow-skipped` explicitly accepts GitHub's
  skipped/neutral conclusions. Unknown conclusions stay pending.
- Review lists and check/status/rule lists are paginated. Reviews match author,
  pinned commit, and `review_after`. Pending, dismissed, and older-cycle reviews
  do not satisfy the predicate. `copilot` is an exact alias for the review bot.
- API errors are never converted into empty-success responses. Transient errors
  back off up to five minutes; the error remains visible during retries.
- Cancellation and terminal outcomes cannot be overwritten by a late worker.
  File locks prevent duplicate workers for the same watch ID. Share that ID
  across clients; separate `watch` calls intentionally create separate subscriptions.

These rules follow the relevant [GitHub check semantics](https://docs.github.com/en/pull-requests/how-tos/merge-and-close-pull-requests/troubleshooting-required-status-checks)
and [active branch rules API](https://docs.github.com/en/rest/repos/rules#get-rules-for-a-branch).

## Persistence and notifications

The default store is `$XDG_STATE_HOME/pr-watch` or `~/.local/state/pr-watch`.
`PR_WATCH_STATE_DIR` or `--state-dir` selects an isolated store. Keep this on a
local disk, not a shared network filesystem. Credentials and review bodies are
not stored. `PR_WATCH_GH` optionally identifies a stable absolute `gh` executable
for desktop hosts whose PATH differs from the shell.

`watch` starts a detached worker. Closing the CLI/MCP connection does not stop it.
`wait` and `resume` recover a lost worker. `status` reports `worker_active` and
`needs_resume`. For unattended recovery after login/reboot, run `pr-watch serve`
under a supervisor; a Linux user-service template is in `examples/pr-watch.service`.
It resumes existing pending subscriptions without resetting their deadlines.
This service runs while the user service manager is running; it does not enable
system-wide lingering or create a remote service.

On Linux, from a checkout of this repository:

```sh
mkdir -p ~/.config/systemd/user
cp examples/pr-watch.service ~/.config/systemd/user/
# Edit ExecStart if `command -v pr-watch` is not ~/.local/bin/pr-watch.
systemctl --user daemon-reload
systemctl --user enable --now pr-watch.service
```

Call CLI/MCP wait once. Hosts with a background-command/Monitor facility can run
`pr-watch wait ID` there. Configure a sufficiently long MCP tool timeout;
do not repeatedly poll status or the subprocess with model calls.

**A closed or idle conversation is not automatically restarted by MCP.** The
host owns delivery/resumption and tool-call timeouts. The worker survives a host
disconnect, and another provider can consume its saved result using the same ID.

## MCP configuration

Run `pr-watch mcp`. The server exposes `watch_pr`, `get_pr_watch`,
`wait_for_pr_watch`, `cancel_pr_watch`, and `list_pr_watches`. All results are
structured JSON.

Use `command -v pr-watch` to find the absolute executable path. For Claude Code:

```sh
claude mcp add --scope user --transport stdio pr-watch -- /absolute/path/to/pr-watch mcp
```

For Cursor, merge this entry into `~/.cursor/mcp.json`:

```json
{"mcpServers":{"pr-watch":{"type":"stdio","command":"/absolute/path/to/pr-watch","args":["mcp"]}}}
```

For Codex (`~/.codex/config.toml`) or Grok (`~/.grok/config.toml`):

```toml
[mcp_servers.pr-watch]
command = "/absolute/path/to/pr-watch"
args = ["mcp"]
tool_timeout_sec = 3700
```

For OpenCode, merge into `~/.config/opencode/opencode.jsonc`:

```json
{"mcp":{"pr-watch":{"type":"local","command":["/absolute/path/to/pr-watch","mcp"],"enabled":true,"timeout":3700000}}}
```

Restart/reconnect an existing client to discover newly registered tools.
Codex's [MCP timeout setting](https://learn.chatgpt.com/docs/config-file/config-reference),
[Cursor stdio configuration](https://cursor.com/docs/mcp), and
[OpenCode configuration](https://opencode.ai/docs/mcp-servers/) are host settings;
changing one does not change other providers' timeouts.

An agent should call `watch_pr` once, retain the returned `id`, then call
`wait_for_pr_watch` with that ID. Set its wait timeout below the host's tool timeout.
If the host disconnects, use `get_pr_watch` or `list_pr_watches` after reconnecting.
Every client on the same machine and user account shares the default store.

## Development

```sh
git clone https://github.com/sebmellen/pr-watch.git
cd pr-watch
uv venv .venv
uv pip install --python .venv/bin/python -e '.[mcp]'
.venv/bin/python -m unittest discover -s tests -v
```

Tests cover predicates, API failures, revision races, pagination, cancellation,
deadlines, persistence, and worker ownership. The subprocess test uses a fake `gh`:
one MCP client starts a watch, the CLI reads it, and another client receives completion.
Tests need no GitHub credentials. CI runs the same suite on Linux and macOS.

The implementation separates GitHub reads (`github.py`), pure completion predicates
(`model.py`), SQLite state and process locks (`store.py`), and worker lifecycle
(`engine.py`). CLI and MCP are thin adapters over that engine. Changes to completion
semantics should include a regression test, especially for missing or stale data.

Report bugs or propose changes through [issues](https://github.com/sebmellen/pr-watch/issues)
and pull requests. Include the relevant JSON state with private repository details
removed; never include credentials or authentication configuration.
