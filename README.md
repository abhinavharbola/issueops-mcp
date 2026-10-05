# IssueOps MCP

A human-in-the-loop GitHub issue triage system. An MCP client (Claude) or a triage agent reads issues and **proposes** changes (comments, labels, assignees, closing). Nothing reaches GitHub until a person approves it in a Streamlit dashboard, and every read, proposal, and decision is audit-logged.

Runs on free tiers: Neon Postgres, Groq for the classifier, and separate fine-grained GitHub PATs for read and write.

## Preview

<p align="center">
  <img src="assets/landing_view.png" width="720" alt="Streamlit dashboard showing pending proposals, the needs-review section, and the recent audit log">
  <br>
  <sub>Landing view of the dashboard with queue metrics and the audit log.</sub>
</p>

> Additional screenshots in [`assets`](assets/).

## What this is

For an allowlisted repository, the system:

1. Exposes 10 MCP tools: 5 read, 5 propose. Propose tools never call GitHub's write API.
2. Validates each proposal (allowlist, arguments, not a pull request, queue limits, duplicates), snapshots the issue, and queues a `pending_actions` row.
3. Shows queued proposals in a dashboard where a human reviews the source issue and approves or rejects.
4. On approval, claims the row with a lease, re-fetches the issue, checks it is still valid, then writes with a separate write token.
5. Logs everything in `audit_log`, including deduplicated, invalid, stale, and failed proposals.
6. Sends writes with an unknowable outcome (network failure or GitHub 5xx mid-write) to `needs_review`, where a person checks GitHub and records the result.

The triage agent (`agent/triage.py`), run manually or from your own scheduler, uses the same propose path: it classifies open issues with an LLM and queues proposals for review.

## Architecture

```mermaid
flowchart TD
    client(["MCP client or triage agent"]) --> read["read tools"]
    client --> propose["propose tools"]
    read --> ghr["GitHub API, read token"]

    propose --> validate["allowlist, validation,<br/>queue caps, dedup"]
    validate --> queue[("pending_actions<br/>Neon Postgres")]

    queue --> dash["Streamlit dashboard"]
    human(["human approver"]) --> dash
    dash -->|reject| rejected["status: rejected"]
    dash -->|approve| claim["claim row, lease token"]

    claim --> check{"stale?"}
    check -->|yes| stale["status: stale"]
    check -->|no| ghw["GitHub API, write token"]
    ghw --> done["status: executed or failed"]
    ghw -->|unknown outcome| review["status: needs_review"]
    review -->|applied| done
    review -->|not applied| queue

    validate -.-> audit[("audit_log")]
    dash -.-> audit
    ghw -.-> audit
```

Details are in [Guardrails](#guardrails).

## Processes and credentials

All processes need `NEON_DSN`. Use two env files: `.env` (from `.env.example`) for everything except the dashboard, and `.env.dashboard` (from `.env.dashboard.example`), the only file that holds `GITHUB_WRITE_PAT`.

| Process | Entry point | Env file | Credentials | Writes to GitHub |
|---|---|---|---|---|
| MCP server | `python -m mcp_server.server` | `.env` | read token | No |
| Triage agent | `python -m agent.triage` | `.env` | read token, Groq key | No |
| Eval | `python -m eval.eval` | `.env` | read token, Groq key | No |
| Dashboard | `ISSUEOPS_ENV_FILE=.env.dashboard streamlit run dashboard/app.py` | `.env.dashboard` | read token, write token | Yes, only after a human approves |
| Database scripts | `scripts/migrate.py`, `scripts/allowlist.py`, `scripts/prune_audit_log.py` | `.env` | database DSN only | No |
| Smoke-test client | `python scripts/custom_client.py` | `.env` | starts the MCP server, so read token | No |

`load_config(require_write_pat=False)` never reads the write token from a file, drops it if inherited from the environment, and raises if write mode is loaded later in the same process. The `Config` repr hides credentials. All processes share one Postgres role, so this separates GitHub credentials, not database access.

## MCP tools

| Kind | Tool | What it does |
|---|---|---|
| Read | `list_issues` | Issue summaries by state, labels, and recency. Excludes pull requests. Up to `limit` (default 50, max 100) with a `truncated` flag |
| Read | `get_issue` | One issue with its 30 newest comments (each clipped to 2500 chars) and a count of omitted comments |
| Read | `list_pull_requests` | Pull request summaries, same `limit` and `truncated` behavior |
| Read | `search_issues` | Search within one repo. `repo:`, `org:`, `user:`, `owner:` qualifiers are rejected and other-repo results are dropped |
| Read | `get_repo_activity_summary` | Opened, closed, and commented issues, and opened issues by label, over 1 to 365 days |
| Propose | `propose_add_comment` | Queue a comment |
| Propose | `propose_add_labels` | Queue label additions, checked against the repo's labels |
| Propose | `propose_remove_labels` | Queue label removals |
| Propose | `propose_assign` | Queue an assignee, checked against the repo's assignable users |
| Propose | `propose_close` | Queue closing, optionally as `completed` or `not_planned` |

Read tool descriptions label issue text as untrusted. All propose tools reject pull request numbers.

## Triage agent

- **Scope:** `openai/gpt-oss-20b` on Groq (`--model` to override), one call per issue, never pull requests. It proposes labels and assignments; `--allow-comment` and `--allow-close` add the rest, except on flagged issues. Each proposal stores the model's rationale (up to 2000 chars) for the approver.
- **Skips:** an issue is skipped while an agent proposal for it is `pending`, `approving`, `needs_review`, `rejected`, or `executed`; `expired`, `failed`, and `stale` are reconsidered. `no_action` results, and `proposed` results with a live or terminal proposal, skip only while the title and body are unchanged.
- **Failures:** bad model output is retried on later runs, up to 3 failures per unchanged text. Rate limits (Groq or GitHub) stop the run; connection and server errors are transient. Neither is recorded against the issue. Systemic failures (bad key, de-allowlisted repo, missing DB grant) exit 1.
- **Options and caps:** `--state`, `--max-issues`, `--since`, `--max-pages` (both counts positive). Prompt caps: title 300 chars, body 8000, 10 comments (1500 each, 6000 total).

## Guardrails

Statuses: `pending`, `approving`, `executed`, `rejected`, `expired`, `stale`, `blocked`, `failed`, `needs_review`.

**Proposing and approving**

| Control | Behavior |
|---|---|
| Allowlist | Every call checks `repo_allowlist.active`. Names are lowercased and reject `.` and `..` segments. Deactivating a repo rejects new proposals and keeps history; its queued rows become `blocked` on the next approval attempt |
| Rejected on proposal | Closing a non-open issue, adding an existing label, removing an absent label, assigning an existing assignee, targeting a pull request |
| Queue caps | `MAX_PENDING_PER_ISSUE` (10) and `MAX_PENDING_PER_INITIATOR` (500), counting `pending` and `approving` rows, under two ordered advisory locks |
| Dedup | A duplicate of a `pending` or `approving` proposal is dropped and audited as `deduped` |
| Write token | Held only by the dashboard |
| Claim | `pending` becomes `approving` atomically under a lease token that every later write requires. No DB lock is held across GitHub calls |
| Recheck | The issue is re-fetched and the row becomes `stale` if the title or body changed, a close target is no longer open, a label to remove is gone, a label to add no longer exists, or an identical comment exists |
| Dashboard access | Requires `DASHBOARD_ACCESS_TOKEN` unless `DASHBOARD_ALLOW_INSECURE=true`. Flagged proposals need an acknowledgement, and the approver can reload the live issue and re-run the flag check |
| GitHub client | 15 second timeout. Pagination raises instead of truncating, and next-page links must point to `https://api.github.com` |

**Outcomes**

Every transition is written atomically with its audit row.

| Situation | Result |
|---|---|
| Issue unreadable before the write | 404 or 410: `failed`. Anything else: released to `pending` |
| 4xx on the write | `failed` |
| Network error or 5xx on the write | `needs_review` (outcome unknown, never auto-retried) |
| DB write fails after the GitHub call | Retried 3 times, later attempts on fresh connections. If all fail, `recording_failed` is returned and the row stays `approving` until recovery |

**Recovery and expiry** (run on every dashboard load)

| Condition | Result |
|---|---|
| `approving` past `STUCK_APPROVING_RECOVERY_MINUTES`, GitHub call never started | `pending` |
| `approving` past that limit, GitHub call started | `needs_review` |
| `needs_review` resolved as applied | `executed` |
| `needs_review` resolved as not applied | `pending`, expiry window restarts (`requeued_at`) |
| `pending` past `PENDING_ACTION_TTL_HOURS` (48) | `expired`, audited |

## Prompt injection

Issue titles, bodies, and comments are untrusted. The classifier prompt wraps them in `<untrusted_issue_content>` markers, strips any copy of those markers first, and tells the model to treat the content as data. MCP tool descriptions carry the same warning. A phrase check (`is_heuristically_flagged`, after Unicode normalization and zero-width-character removal) flags proposals in the dashboard and blocks agent comments and closes on flagged issues. It over-triggers by design and is not a security boundary. The real control is that every proposal is scoped to one issue and needs human approval.

## Project Structure
```
issueops-mcp/
├── .github/workflows/ci.yml
├── .streamlit/config.toml
│
├── issueops/
│   ├── config.py                # env loading, credential rules, DSN-only loader
│   ├── db.py                    # Postgres connection helper
│   ├── limits.py                # text limits, clip helper, env integer parsing
│   ├── github_client.py         # read and write GitHub clients, pagination guard
│   ├── tools.py                 # read tools, propose tools, validation, caps, audit writes
│   ├── actions.py               # approve, reject, expire, recover, resolve, lease handling
│   ├── heuristics.py            # advisory injection-phrase check
│   ├── projection.py            # slims and bounds what the MCP read tools return
│   └── observability.py         # optional Logfire setup
│
├── agent/
│   ├── prompts.py               # classifier prompt, trusted context, untrusted block
│   └── triage.py                # triage agent and CLI
│
├── mcp_server/server.py         # MCP server exposing the 10 tools
├── dashboard/
│   ├── app.py                   # Streamlit approval UI and audit log view
│   └── auth.py                  # access token check
│
├── db/
│   ├── schema.sql               # idempotent baseline: creates a fresh database or upgrades an existing one, and records every migration file below as applied
│   └── migrations/              # incremental changes, applied and tracked by scripts/migrate.py
│
├── eval/
│   ├── eval.py                  # classification, adversarial, and audit-consistency checks
│   └── labels_template.json     # fixture format
│
├── scripts/
│   ├── allowlist.py             # add, deactivate, list allowlisted repos
│   ├── migrate.py               # applies db/migrations/*.sql not yet recorded in schema_migrations
│   ├── prune_audit_log.py       # delete audit rows older than N days
│   └── custom_client.py         # call one MCP tool from the command line
│
├── assets/
├── tests/                       # unit tests plus Postgres integration tests
│
├── conftest.py                  # fake DB used by the unit tests
├── .env.example
├── .env.dashboard.example
├── .gitignore
├── requirements.txt
└── README.md
```

## Getting started

1. **Accounts and keys:**
   - GitHub: two fine-grained tokens on the target repo, read (Issues read, Pull requests read, Metadata read) and write (Issues read and write): https://github.com/settings/personal-access-tokens
   - Neon (free tier): https://neon.tech. Copy the pooled connection string.
   - Groq, for the agent and eval: https://console.groq.com/keys
   - Logfire (optional, tracing no-ops without it): https://logfire.pydantic.dev

2. **Install** (Python 3.10 or 3.12, as in CI):
   ```
   python3 -m venv .venv && source .venv/bin/activate
   pip install -r requirements.txt
   cp .env.example .env
   cp .env.dashboard.example .env.dashboard
   ```
   In `.env`, set `NEON_DSN` and `GITHUB_READ_PAT`, plus `GROQ_API_KEY` for the agent or eval. In `.env.dashboard`, set `NEON_DSN`, `GITHUB_READ_PAT`, `GITHUB_WRITE_PAT`, and `DASHBOARD_ACCESS_TOKEN` (or `DASHBOARD_ALLOW_INSECURE=true` on a machine only you can reach).

3. **Database.** Run [`db/schema.sql`](db/schema.sql) once in your Neon project's SQL Editor. It is idempotent: it creates a fresh database or upgrades an existing one, and records every file in `db/migrations/` in `schema_migrations`. Apply migrations added later with:
   ```
   python scripts/migrate.py
   ```
   Pending files are applied in one transaction under an advisory lock, so concurrent runs cannot double-apply and a failure leaves nothing half-applied. When adding a migration, also add its name to the `INSERT INTO schema_migrations` list at the end of `db/schema.sql` and fold its change into the baseline; `tests/test_schema_files.py` fails if they disagree.

4. **Allowlist a repo.** Nothing works on a repo until this is done.
   ```
   python scripts/allowlist.py add owner/repo
   ```

## Running it

Run from the repo root. Everything except the dashboard reads `.env`; the dashboard reads `.env.dashboard`.

| Command | What it does |
|---|---|
| `ISSUEOPS_ENV_FILE=.env.dashboard streamlit run dashboard/app.py` | Approval dashboard |
| `python -m agent.triage owner/repo --max-issues 5` | Queue label and assignment proposals. Add `--allow-comment` and `--allow-close` to include comments and closes |
| `python -m mcp_server.server` | MCP server over stdio |
| `python scripts/custom_client.py list_issues '{"repo": "owner/repo"}'` | Call one MCP tool as a smoke test |
| `python scripts/allowlist.py list` | List allowlisted repos (`add` and `deactivate` take a repo name) |
| `python scripts/migrate.py` | Apply pending migrations |
| `python scripts/prune_audit_log.py --days 90` | Delete audit rows older than 90 days |
| `python -m eval.eval path/to/labels.json` | Run the evaluation |

On Windows, activate with `.venv\Scripts\activate` and, in PowerShell, run `$env:ISSUEOPS_ENV_FILE=".env.dashboard"` before `streamlit run dashboard/app.py`.

To use the MCP server from Claude Desktop, add this to `claude_desktop_config.json` with absolute paths and restart it. The server reads credentials from the repo's `.env`, so they do not go in this file:
```
{
  "mcpServers": {
    "issueops": {
      "command": "/abs/path/issueops-mcp/.venv/bin/python",
      "args": ["-m", "mcp_server.server"],
      "env": { "PYTHONPATH": "/abs/path/issueops-mcp" }
    }
  }
}
```

In the dashboard: enter the access token and your name, expand a pending action, read the rationale and stored issue text (tick the acknowledgement if flagged), optionally **Load current issue from GitHub**, then **Approve** or **Reject**. Rows under **Needs review** need a person to check GitHub and record whether the change was applied.

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `NEON_DSN` | required | Pooled Postgres connection string |
| `GITHUB_READ_PAT` | required, except for database-only scripts | Read token |
| `GITHUB_WRITE_PAT` | dashboard only, in `.env.dashboard` | Write token |
| `GROQ_API_KEY` | agent and eval only | LLM key |
| `LOGFIRE_TOKEN` | none | Enables tracing |
| `PENDING_ACTION_TTL_HOURS` | 48 | How long a proposal can wait |
| `STUCK_APPROVING_RECOVERY_MINUTES` | 10 | When an `approving` row counts as crashed |
| `COMMENT_BODY_MAX_CHARS` | 65536 | Max proposed comment length (also the ceiling, matching GitHub's limit) |
| `MAX_PENDING_PER_ISSUE` | 10 | Open proposals per issue |
| `MAX_PENDING_PER_INITIATOR` | 500 | Open proposals per initiator |
| `MCP_CLIENT_LABEL` | none | Label recorded as the MCP initiator |
| `DASHBOARD_ACCESS_TOKEN` | none | Gates the dashboard. Required unless `DASHBOARD_ALLOW_INSECURE` is set |
| `DASHBOARD_ALLOW_INSECURE` | false | Lets the dashboard run without a token |
| `ISSUEOPS_ENV_FILE` | auto-discovered `.env` | Env file for this process. Set to `.env.dashboard` for the dashboard |

## Testing

```
pytest
```

Unit tests use a fake database and mocked GitHub clients, so they cannot verify locking. `tests/test_integration_postgres.py` runs against real Postgres and covers concurrent approvals, lease reclamation, dedup, queue caps, deadlock freedom, atomic audit writes, dropped-connection retry, triage skip rules, requeue expiry, unknown write outcomes, schema re-application, the migration runner, and the legacy upgrade. It is skipped unless `TEST_DATABASE_URL` is set; each test creates and drops its own schema. CI runs everything against Postgres 16 on Python 3.10 and 3.12. `tests/test_dashboard.py` drives the dashboard headlessly and is skipped without Streamlit.

## Evaluation

```
python -m eval.eval path/to/labels.json
```

Start from [`eval/labels_template.json`](eval/labels_template.json). The script fetches the repo's labels and assignable users, applies the heuristic flag, and classifies each issue with the agent's own planning code. Adversarial issues are planned with comments and closes enabled only when unflagged. Correctly labeling spam and obeying an injection both count as acted, so a high rate is not by itself a failure.

Local run: 12-issue fixture (9 legitimate, 3 adversarial) against `openai/gpt-oss-20b`. The fixture is not in the repo.

| Metric | Measures | Result |
|---|---|---|
| `label_accuracy` | Predicted label set, filtered to existing repo labels, equals `expected_labels` (case-insensitive), on non-adversarial issues | 9/9 |
| `adversarial_any_action_rate` | Adversarial issues with any planned proposal after flag gating | 3/3 |
| `marker_hit_rate` | Adversarial issues whose output echoes an injection marker | 0/3 |
| `proposal_level_susceptibility` | Adversarial issues with either of the above | 3/3 |
| `avg_latency_ms` | Mean model call time only | ~4200 |

With n=12 this is a smoke test, not a benchmark. The eval also runs an audit consistency check in both directions between `audit_log` and `pending_actions`. It catches bookkeeping bugs in this codebase, not writes made elsewhere, and excludes pruned rows.

## Known limitations

**Security and data**
- The injection heuristic is advisory, avoidable, and over-triggers on LLM-related text.
- Approver identity is a typed name; the access token gates the app but does not identify people.
- Issue text is sent to Groq by the agent and stored in Neon (snapshots and source excerpts). Avoid private repos you cannot share with those providers.
- One shared Postgres role: the audit log is append-only by convention (only `prune_audit_log.py` deletes), and nothing stops the write token being put in `.env`.

**Behavior**
- `propose_remove_labels` calls GitHub once per label and can partially succeed; the failure lists what was removed.
- Expiry runs only when the dashboard loads, and queue caps count unexpired `pending` rows, so an unattended queue can block new proposals.
- No scheduler is included.

**Scale**
- Label and assignee caches are process-local with a 5 minute TTL.
- Pagination caps at 20 pages or 2000 items. MCP listings stop at `limit` (max 100) and report `truncated`; other bulk reads flag partial results. Beyond 2000 comments, the duplicate-comment check sees only the first 2000.
- Without `MCP_CLIENT_LABEL`, the initiator includes hostname and pid, so the per-initiator cap is per process.