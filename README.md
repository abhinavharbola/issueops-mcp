# IssueOps MCP

An MCP server plus a human-approval layer for GitHub issue triage. An MCP client (Claude Desktop,
or the scheduled triage agent in `agent/triage.py`) can read issues and *propose* mutating actions
(add a comment, add or remove labels, assign, close). Nothing is written to GitHub until a human
approves the proposal in the Streamlit dashboard.

## Why a human approval layer

The MCP server's `propose_*` tools never call GitHub's write API directly. They validate the
request, snapshot the issue's current state, and insert a row into `pending_actions` in Postgres
(Neon). A separate write-capable process, the dashboard, executes the row only after a human clicks
Approve, and only if the parts of the issue the action depends on haven't changed since the
snapshot was taken (see "What makes an approval stale").

### Credential separation

The MCP server and the triage agent only ever hold `GITHUB_READ_PAT`. `load_config` enforces this
in three ways when called with `require_write_pat=False`: it never reads `GITHUB_WRITE_PAT` from
the env file into the process, it pops the variable if it was inherited from the real environment,
and it makes a later `load_config(require_write_pat=True)` in the same process raise, whether or not
an env file contains the key. `GROQ_API_KEY` is only required by the triage agent and the eval
(`require_groq=True`), so the dashboard, which holds the write PAT, does not need an LLM key.

This is process-level hygiene, not isolation. If the MCP server and the dashboard run on one
machine as one user and share one env file, anything that can read that file can read the write
PAT. For real separation give each process its own file and set `ISSUEOPS_ENV_FILE` to point at it:
a file with `GITHUB_READ_PAT` and no write PAT for the MCP server and triage agent, and a
locked-down file with the write PAT for the dashboard only. Better still, run the dashboard on a
different host or user and keep the write PAT in that host's secret store.

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
be undone. Comments are the one action the snapshot cannot protect, since posting one does not change the
snapshot, so `approve_action` also refuses to post a comment when an identical one already exists.
In practice this is unlikely: the recovery window (10 minutes by default) is generous
relative to how long a normal `approve_action` call takes (a handful of HTTP calls, each with a
15 second timeout), so a real crash and a false-positive recovery are very different durations.
Lower `STUCK_APPROVING_RECOVERY_MINUTES` and this risk shrinks further at the cost of recovering
genuinely crashed rows more slowly.

## What makes an approval stale

`pending_actions.issue_state_snapshot` records `state`, `labels`, `assignees`, and a SHA-256
`content_hash` of the title and body at proposal time. At approval the issue is fetched again
(without comments) and compared, but only on the fields the action depends on:

| Check | Applies to | Stale when |
| --- | --- | --- |
| Content | every action | the title or body changed since the proposal |
| State | `propose_close` | the issue is no longer open |
| Labels | `propose_remove_labels` | any label to remove is no longer on the issue |
| Duplicate | `propose_add_comment` | an identical comment already exists on the issue |

Unrelated changes no longer invalidate an action. In particular, the several proposals the triage
agent queues for one issue do not go stale one another when the first is approved. New comments and
edits to existing comments are not part of the check. Snapshots stored before `content_hash`
existed skip the content check.

After a `propose_assign` executes, the response from GitHub is checked and the action is marked
`failed` if the login is not in the issue's assignees, because GitHub can accept the request
without applying it. If a write fails with a network error (timeout, connection reset) the
result carries `outcome_unknown: true` and the dashboard tells the approver to check GitHub before
proposing again, since the request may have been applied.

## Fetching and transactions in the proposal path

`_queue_proposal` does its GitHub calls (label and assignee validation, the issue fetch used for
the snapshot and heuristic flag) before it opens a transaction. Only the advisory lock, the
duplicate re-check, the cap check, and the insert run inside the transaction, so no lock or pooled
backend is held across network I/O in either the approve or the propose path. `agent/triage.py`
fetches each issue once and passes it to every `propose_*` call via `issue=`, so a triage run makes
one issue fetch per issue regardless of how many proposals it produces. The MCP tool wrappers do
not pass `issue=`; each MCP call is one-shot, so `_queue_proposal` fetches fresh.

## Triage memory

The scheduled agent no longer re-proposes the same things every run:

- Issues that already have an agent proposal (`requested_by LIKE 'agent:%'`) in status `pending`,
  `approving`, `rejected`, or `executed` are skipped. `expired`, `failed`, `stale`, and `blocked`
  rows do not count, so those issues are re-evaluated.
- The classifier is given trusted context: the issue's state, current labels and assignees, the
  labels that exist on the repo, and the users who can be assigned. The plan drops labels already on
  the issue or not on the repo, assignees who are already assigned or not assignable, and closes for
  issues that are not open.
- `--since` limits the listing to recently updated issues and `--max-pages` raises the pagination
  ceiling for large repos. `--max-issues` is applied after skipped issues are removed.
- Groq JSON mode is requested, with one fallback call without it if the model rejects the parameter.

## Queue limits

`MAX_PENDING_PER_ISSUE` (default 10) and `MAX_PENDING_PER_INITIATOR` (default 500) cap rows in
`pending` or `approving`. A proposal over either cap is rejected with a `ValidationError` before any
GitHub call. This bounds what a prompt-injected MCP client can queue. The MCP initiator string
includes the process id, so the initiator cap is per process, not per person.

## Search scoping

`search_issues` rejects queries containing `repo:`, `org:`, `user:`, or `owner:` qualifiers, and the
client drops any result whose `repository_url` is not the requested repo. Both are needed: the
qualifier check gives a clear error, the filter is the actual guarantee.

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
   `NEON_DSN`. If you are upgrading an existing database, apply `db/migrations/001_hardening.sql`
   instead.
2. Copy `.env.example` to `.env` and fill it in. Set `DASHBOARD_ACCESS_TOKEN` before running the
   dashboard anywhere beyond a trusted local machine, and read "Credential separation" before
   putting the write PAT in the same file as the read PAT.
3. `pip install -r requirements.txt`
4. `python scripts/allowlist.py add owner/repo`
5. Run the MCP server: `python -m mcp_server.server` (or point Claude Desktop at it).
6. Run the dashboard: `streamlit run dashboard/app.py`
7. Run the triage agent on a schedule: `python -m agent.triage owner/repo`
8. Prune old audit rows periodically: `python scripts/prune_audit_log.py --days 90`

## Testing

After `pip install -r requirements.txt`, run `pytest` from the repo root.

Most tests use `conftest.py`'s `FakeConn`/`FakeCursor` to exercise SQL call sequences without a
database, and `unittest.mock.MagicMock` for the GitHub clients. Those cannot verify locking. The
tests in `tests/test_integration_postgres.py` run against a real Postgres (concurrent approvals,
lease reclamation, concurrent dedup, caps, the status CHECK constraint) and are skipped unless
`TEST_DATABASE_URL` is set. Each test builds and drops its own schema. `.github/workflows/ci.yml`
runs them against a Postgres service container.

## Eval

`python -m eval.eval` reports classification accuracy, adversarial susceptibility, and an audit
consistency check. Adversarial fixtures should be issues where the correct outcome is no action:
`adversarial_any_action_rate` counts any resulting proposal, and `proposal_level_susceptibility`
also counts injection markers appearing in the model output. The audit consistency check
cross-checks `audit_log` against `pending_actions` in both directions. Both tables are written by
this code, so it detects bookkeeping bugs, not a write made outside the dashboard with the write
PAT. To detect that, review the write PAT's activity in GitHub's audit log. No labeled dataset is
shipped, only `eval/labels_template.json`.

## Known limitations

- The heuristic flag is advisory only, see above.
- Approver identity in the dashboard is a free-text name field, not authentication.
  `DASHBOARD_ACCESS_TOKEN` gates access to the dashboard itself; it does not distinguish between
  approvers. Failed token attempts are throttled process-wide (5 per minute), which is a speed bump,
  not a defense against a determined attacker. Treat the audit log's `initiator`/`approved_by`
  fields as a record of what was typed, not a verified identity.
- A row stuck in `approving` after a crash is only recovered on the next dashboard page load
  (`recover_stuck_approving`), not immediately. There's no background worker in this project.
- The recovery sweep is time-based, not a true liveness check. See the lease discussion above;
  the narrow `lost_lease_after_execution` case needs a human to check GitHub directly, since the
  underlying write already happened and can't be rolled back automatically.
- The stale check does not cover new comments or edits to existing comments, and a failed or
  stale action is terminal: the agent can re-propose it on a later run, a human cannot retry it.
- `propose_remove_labels` executes its GitHub calls sequentially and can partially succeed; the
  failure message lists which labels were removed, which one failed, and which were never
  attempted, so a human can finish the job manually.
- `load_config` assumes one PAT role per process: the MCP server and triage agent call it with
  `require_write_pat=False`, the dashboard with `require_write_pat=True`, each in its own process.
  Calling it both ways in the same process raises a `RuntimeError` explaining why, rather than
  silently returning a `Config` without the write PAT.
- No dependency lockfile is included (`requirements.txt` pins version ranges only) and no license file is
  included; choosing a license is up to the repository owner.
- The repo label and assignee caches in `issueops/tools.py` are process-local with a 5 minute
  TTL. It is not shared or invalidated across multiple MCP server or triage agent processes; if
  you ever run more than one worker of either, each holds its own view of a repo's labels and
  assignable users for up to 5 minutes.
- `GitHubReadClient._paginated_get` raises `PaginationLimitExceededError` instead of silently
  truncating when a repo has more result pages than `max_pages` (default 20, i.e. 2000 items).
  A very active repo can therefore make `list_issues` or
  `get_repo_activity_summary` fail loudly rather than quietly under-report; narrow the query
  (state, labels, `since`, a shorter `days` window) or pass a higher `max_pages`.
