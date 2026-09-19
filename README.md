# IssueOps MCP

An MCP server plus a human-approval layer for GitHub issue triage. An MCP client (Claude Desktop,
or the scheduled triage agent in `agent/triage.py`) can read issues and *propose* mutating actions
(add a comment, add or remove labels, assign, close). Nothing is written to GitHub until a human
approves the proposal in the Streamlit dashboard.

## Why a human approval layer

The MCP server's `propose_*` tools never call GitHub's write API directly. They validate the
request, snapshot the issue's current state, and insert a row into `pending_actions` in Postgres
(Neon). A separate write-capable process, the dashboard, executes the row only after a human clicks
Approve, and only if the issue hasn't changed since the snapshot was taken.

### Credential separation

The MCP server and the triage agent only ever hold `GITHUB_READ_PAT`. `issueops/config.py`
actively drops `GITHUB_WRITE_PAT` from the process environment when `load_config` is called with
`require_write_pat=False`, so even a read-only process that happened to inherit the write PAT in
its environment cannot use it. Only the dashboard process, which calls
`load_config(require_write_pat=True)`, keeps it.

## Approving an action without holding a database lock across GitHub calls

Earlier versions of `approve_action` opened a single Postgres transaction, took a `SELECT ... FOR
UPDATE` lock on the pending action row, and held that lock open across the GitHub read (re-fetch
the issue to check for staleness) and GitHub write (execute the action) HTTP calls. That guaranteed
an action can't be double-executed, but it meant a slow or rate-limited GitHub API call held a
Postgres backend connection for the whole round trip. On the pooled Neon connection string this
project recommends (so the dashboard survives Streamlit's constant reruns without exhausting direct
connections), a long transaction pins a pooled backend for its entire duration, which does not
scale past a couple of concurrent approvers.

`approve_action` now works in short phases instead of one long transaction:

1. **Claim.** A short transaction takes the row lock, checks it's still `pending`, checks it hasn't
   passed `PENDING_ACTION_TTL_HOURS`, checks the repo is still active in the allowlist, then flips
   `status` to `approving`, stamps `claimed_at = now()`, and commits, returning that `claimed_at`
   value as a lease token. No GitHub calls happen inside this transaction. Once committed, the row
   is no longer `pending`, so it also disappears from the dashboard's pending list, which keeps a
   second approver from ever seeing it to begin with.
2. **Execute.** Outside any transaction, `approve_action` re-fetches the issue, then re-checks the
   lease is still held (`status = 'approving' AND claimed_at = <lease>`) before trusting the
   staleness comparison against that fetch, then calls the GitHub write API. The lease is checked
   before the staleness comparison, not after: if the lease was reclaimed by another approver, the
   issue may have changed for reasons that have nothing to do with the *original* pending action,
   so that case is reported as `lost_lease`, not `stale`. Every terminal write (`executed`, `stale`,
   `failed`) is itself conditioned on `status = 'approving' AND claimed_at = <lease>`, not just on
   the row's id; a phase that runs before the GitHub call and finds that conditional write affects
   zero rows reports `lost_lease` (nothing was sent to GitHub by this call), while a phase that
   runs after the GitHub call reports `lost_lease_after_execution` instead, since a GitHub call may
   already have gone out.

Double-execution across two concurrent human clicks is prevented by the `status = 'pending'` guard
in the claim step: a second concurrent approve on the same row finds `status != 'pending'` as soon
as the first claim commits and returns `not_found_or_not_pending` immediately, without needing to
hold a lock across network I/O.

**Crash recovery and the lease.** If the process crashes between claiming a row and finishing it,
that row is stuck in `approving`. `actions.recover_stuck_approving` resets any row that's been in
`approving` for longer than `STUCK_APPROVING_RECOVERY_MINUTES` (default 10) back to `pending`, with
an audit log entry noting the recovery. The dashboard calls this on every page load, the same way
it already calls `expire_stale_pending`.

This recovery is a time-based guess, not a real liveness check, so it is possible in principle for
it to fire while the original `approve_action` call is still genuinely running (not crashed, just
slow). If that happens, the lease token protects correctness rather than raw wall-clock timing:
the original call's re-check right before the GitHub write (and every write after it) is
conditioned on still holding the exact `claimed_at` lease it was issued. If a second approver has
since reclaimed the row, those conditional writes affect zero rows and the original call returns
`lost_lease` (if caught before the GitHub call) or `lost_lease_after_execution` (if the GitHub call
had already gone out before the lease was found to be lost). The former means nothing was sent to
GitHub by that call. The latter means it was, and a human needs to check the audit log and the
issue on GitHub directly for a possible duplicate, since a GitHub API call that already fired can't
be undone. In practice this is unlikely: the recovery window (10 minutes by default) is generous
relative to how long a normal `approve_action` call takes (a handful of HTTP calls, each with a
15 second timeout), so a real crash and a false-positive recovery are very different durations.
Lower `STUCK_APPROVING_RECOVERY_MINUTES` and this risk shrinks further at the cost of recovering
genuinely crashed rows more slowly.

## Preventing redundant GitHub fetches during triage

`agent/triage.py` fetches each issue once with `tools.get_issue` to classify it. Previously, every
`propose_*` call the classifier's output triggered would independently re-fetch the same issue
inside `issueops/tools.py::_queue_proposal`, to build the state snapshot and compute the heuristic
flag. An issue that produced four proposals (labels, comment, close, assign) cost five full issue
fetches, each paginating comments, instead of one.

`_queue_proposal` now accepts an optional `prefetched_issue`, and every `propose_*` function in
`issueops/tools.py` takes a matching `issue=` keyword. `agent/triage.py` passes the issue it already
fetched into `PROPOSE_DISPATCH`, so a triage run does exactly one GitHub fetch per issue regardless
of how many proposals that issue generates. The MCP server's tool wrappers in `mcp_server/server.py`
don't pass `issue=`, since each MCP tool call is a one-shot request with no earlier fetch to reuse;
`_queue_proposal` fetches fresh in that case, same as before.

## Heuristic flagging

`is_heuristically_flagged` (`issueops/heuristics.py`) is a coarse, advisory-only substring check
against known prompt-injection phrasing (`ignore previous instructions`, `you are now`, and so on).
It is computed automatically inside `_queue_proposal` from the same issue fetch used to build the
state snapshot, so every `propose_*` call is flagged the same way regardless of caller: the MCP
server, the triage agent, or anything else that queues a proposal in the future. It is not a
security boundary, an attacker only has to avoid the listed phrases. It exists to give a human
approver a visible hint in the dashboard, nothing more.

## The ten MCP tools

Read (never write to GitHub): `list_issues`, `get_issue`, `list_pull_requests`, `search_issues`,
`get_repo_activity_summary`.

Propose (queue a row in `pending_actions`, never write to GitHub): `propose_add_comment`,
`propose_add_labels`, `propose_remove_labels`, `propose_assign`, `propose_close`.

## Repo allowlist

Every read and propose tool call checks `repo_allowlist.active` before doing anything else.
`scripts/allowlist.py` adds or deactivates repos. Deactivating sets `active = false` rather than
deleting the row, so existing `pending_actions` rows (which have a foreign key on `repo`) aren't
broken by deactivating a repo they reference.

## Prompt injection handling

Issue titles, bodies, and comments are untrusted, external, attacker-controlled text. The classifier
prompt in `agent/prompts.py` wraps that text in `<untrusted_issue_content>` markers and instructs
the model to treat it strictly as data. `build_untrusted_block` also strips any occurrence of the
marker tags themselves out of the issue text first, so an issue body can't inject a fake closing
marker and smuggle instructions outside the untrusted block.

## Setup

1. Apply `db/schema.sql` to a Neon Postgres database. Use the **pooled** connection string for
   `NEON_DSN`, both the dashboard (constant Streamlit reruns) and the MCP server / triage agent
   (a new connection per tool call) benefit from pooling.
2. Copy `.env.example` to `.env` and fill in `NEON_DSN`, `GITHUB_READ_PAT`, `GITHUB_WRITE_PAT`,
   `GROQ_API_KEY`. Set `DASHBOARD_ACCESS_TOKEN` before running the dashboard anywhere beyond a
   trusted local machine.
3. `python scripts/allowlist.py add owner/repo`
4. `pip install -r requirements.txt`
5. Run the MCP server: `python -m mcp_server.server` (or point Claude Desktop at it).
6. Run the dashboard: `streamlit run dashboard/app.py`
7. Run the triage agent on a schedule: `python -m agent.triage owner/repo`

## Testing

`pytest` from the repo root. Tests use `conftest.py`'s `FakeConn`/`FakeCursor` to exercise SQL
call sequences without a real database, and `unittest.mock.MagicMock` for the GitHub clients.

## Known limitations

- The heuristic flag is advisory only, see above.
- Approver identity in the dashboard is a free-text name field, not authentication.
  `DASHBOARD_ACCESS_TOKEN` gates access to the dashboard itself; it does not distinguish between
  approvers. Treat the audit log's `initiator`/`approved_by` fields as a record of what was typed,
  not a verified identity.
- A row stuck in `approving` after a crash is only recovered on the next dashboard page load
  (`recover_stuck_approving`), not immediately. There's no background worker in this project.
- The recovery sweep is time-based, not a true liveness check. See the lease discussion above;
  the narrow `lost_lease_after_execution` case needs a human to check GitHub directly, since the
  underlying write already happened and can't be rolled back automatically.
- `propose_remove_labels` executes its GitHub calls sequentially and can partially succeed; the
  failure message lists which labels were removed, which one failed, and which were never
  attempted, so a human can finish the job manually.
- `load_config` assumes one PAT role per process: the MCP server and triage agent call it with
  `require_write_pat=False`, the dashboard with `require_write_pat=True`, each in its own process.
  Calling it both ways in the same process raises a `RuntimeError` explaining why, rather than
  silently returning a `Config` without the write PAT.
- The repo label cache in `issueops/tools.py` (`_label_cache`) is process-local with a 5 minute
  TTL. It is not shared or invalidated across multiple MCP server or triage agent processes; if
  you ever run more than one worker of either, each holds its own view of a repo's labels for up
  to 5 minutes.
- `GitHubReadClient._paginated_get` raises `PaginationLimitExceededError` instead of silently
  truncating when a repo has more result pages than `max_pages` (default 20, i.e. 2000 items).
  A very active repo can therefore make `list_issues`, `search_issues`, or
  `get_repo_activity_summary` fail loudly rather than quietly under-report; narrow the query
  (state, labels, `since`, a shorter `days` window) or pass a higher `max_pages`.
