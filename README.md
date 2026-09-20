# IssueOps MCP

A human-in-the-loop GitHub issue triage system. An MCP client (Claude Desktop) or a scheduled triage agent can read issues and *propose* changes: comments, labels, assignees, closing. Nothing reaches GitHub until a person approves the proposal in a Streamlit dashboard, and every read, proposal, and decision is written to an audit log.

Built on free-tier infrastructure: a Neon Postgres database, Groq for the classifier, and fine-grained GitHub tokens.

## What this is

Given an allowlisted repository, the system:

1. Exposes 10 MCP tools: 5 read tools and 5 propose tools. The propose tools never call GitHub's write API.
2. Validates each proposal (allowlist, arguments, queue limits, duplicates), snapshots the issue, and queues it in Postgres as a `pending_actions` row.
3. Shows queued proposals in a dashboard, where a human reads the source issue and approves or rejects.
4. On approval, claims the row with a lease, re-fetches the issue, checks it hasn't changed in a way that matters, and only then writes to GitHub using a separate write token.
5. Records everything in `audit_log`, including proposals that were deduplicated, rejected as invalid, went stale, or failed.

A scheduled triage agent (`agent/triage.py`) uses the same propose path. It classifies open issues with an LLM and queues labels, comments, assignments, or closes for a human to review.

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

    validate -.-> audit[("audit_log")]
    dash -.-> audit
    ghw -.-> audit
```

Design notes for each step (lease-based claiming, the stale check, crash recovery, transaction discipline) are in [`docs/architecture.md`](docs/architecture.md).

## Processes and credentials

Each process is started separately and loads only the credentials it needs.

| Process | Entry point | Credentials | Writes to GitHub |
|---|---|---|---|
| MCP server | `python -m mcp_server.server` | read token | No |
| Triage agent | `python -m agent.triage` | read token, Groq key | No |
| Dashboard | `streamlit run dashboard/app.py` | read token, write token | Yes, only after a human approves |

`load_config(require_write_pat=False)` never reads the write token from the env file, drops it if it was inherited from the environment, and makes a later write-mode load in the same process raise. The Groq key is only required by the triage agent and the eval, so the dashboard does not hold an LLM key. For real separation, give each process its own env file via `ISSUEOPS_ENV_FILE`. Details and limits of this approach are in [`docs/architecture.md`](docs/architecture.md#credential-separation).

## MCP tools

| Kind | Tool | What it does |
|---|---|---|
| Read | `list_issues` | List issues by state, labels, and recency |
| Read | `get_issue` | One issue with its comments |
| Read | `list_pull_requests` | List pull requests |
| Read | `search_issues` | Search within one repo. `repo:`, `org:`, `user:`, `owner:` qualifiers are rejected and results from other repos are dropped |
| Read | `get_repo_activity_summary` | Issues opened, issues closed, distinct issues with comments, and opened issues by label, over 1 to 365 days |
| Propose | `propose_add_comment` | Queue a comment |
| Propose | `propose_add_labels` | Queue label additions, checked against the repo's labels |
| Propose | `propose_remove_labels` | Queue label removals |
| Propose | `propose_assign` | Queue an assignee, checked against the repo's assignable users |
| Propose | `propose_close` | Queue closing, optionally as `completed` or `not_planned` |

## Triage agent

- **Model:** `openai/gpt-oss-20b` on Groq by default (`--model` to change). One call per issue, JSON mode requested with one fallback call if the model rejects it, and a second key (`GROQ_API_KEY_FALLBACK`) tried on rate limits.
- **Trusted context:** the classifier is told the issue's state, current labels and assignees, the labels that exist on the repo, and who can be assigned. Untrusted issue text is passed separately, inside delimiters.
- **Plan filtering:** proposals for labels already on the issue, labels not on the repo, assignees who are already assigned or not assignable, and closes on issues that are not open are dropped before anything is queued.
- **Memory:** issues that already have an agent proposal in status `pending`, `approving`, `rejected`, or `executed` are skipped on later runs. `expired`, `failed`, and `stale` proposals do not count, so those issues are re-evaluated.
- **Flags:** `--state`, `--max-issues`, `--since`, `--max-pages`, `--model`.

## Guardrails

- **Human approval:** no propose tool can write to GitHub. Only the dashboard process holds the write token.
- **Repo allowlist:** every read and propose call checks `repo_allowlist.active` first. Deactivating a repo keeps its history intact.
- **Lease-based claiming:** approval flips a row from `pending` to `approving` in a short transaction and returns a lease token. Every later write is conditioned on that lease, so two approvers cannot both execute the same action. No database lock is held across a GitHub call.
- **Scoped stale check:** an approval is refused if the title or body changed, if a close targets an issue that is no longer open, if a label to remove is already gone, or if an identical comment already exists. Unrelated changes do not block sibling proposals.
- **Outcome checks:** after an assign, the response is checked and the action fails if GitHub did not apply it. A network failure during a write is reported as `outcome_unknown` so the approver checks GitHub before retrying.
- **Queue limits:** at most `MAX_PENDING_PER_ISSUE` (10) and `MAX_PENDING_PER_INITIATOR` (500) open proposals, enforced before any GitHub call. This bounds what a prompt-injected client can queue.
- **Timeouts:** every GitHub request has a 15 second timeout. Pagination raises `PaginationLimitExceededError` instead of silently truncating.
- **Crash recovery:** rows stuck in `approving` longer than `STUCK_APPROVING_RECOVERY_MINUTES` return to `pending`, with an audit entry.
- **Proposal expiry:** unapproved proposals expire after `PENDING_ACTION_TTL_HOURS` (48).
- **Dashboard access:** an optional `DASHBOARD_ACCESS_TOKEN` gates the app, with failed attempts throttled to 5 per minute.

## Safety

Issue titles, bodies, and comments come from the open internet and are treated as untrusted. The classifier prompt wraps them in `<untrusted_issue_content>` markers, strips any copy of those markers out of the text first so an issue cannot fake a closing tag, and instructs the model to treat the content as data only. Tool descriptions carry the same warning for MCP clients. A coarse phrase check (`is_heuristically_flagged`) marks suspicious proposals in the dashboard. It is a visible hint for the approver, not a security boundary.

## Project Structure
```
issueops-mcp/
├── issueops/
│   ├── config.py              # env loading, per-role credential rules
│   ├── db.py                  # Postgres connection helper
│   ├── github_client.py       # read and write GitHub clients, pagination guard
│   ├── tools.py               # read tools, propose tools, validation, caps, audit writes
│   ├── actions.py             # approve, reject, expire, recover, lease handling
│   ├── heuristics.py          # advisory injection-phrase check
│   └── observability.py       # optional Logfire setup
│
├── agent/
│   ├── prompts.py             # classifier prompt, trusted context, untrusted block
│   ├── triage.py              # scheduled triage agent and CLI
│   └── heuristics.py
│
├── mcp_server/server.py       # MCP server exposing the 10 tools
├── dashboard/app.py           # Streamlit approval UI and audit log view
│
├── db/
│   ├── schema.sql             # tables and indexes
│   └── migrations/            # upgrade scripts for existing databases
│
├── eval/
│   ├── eval.py                # classification, adversarial, and audit-consistency checks
│   └── labels_template.json   # fixture format
│
├── scripts/
│   ├── allowlist.py           # add, deactivate, list allowlisted repos
│   ├── prune_audit_log.py     # delete audit rows older than N days
│   └── custom_client.py       # call one MCP tool from the command line
│
├── tests/                     # unit tests plus Postgres integration tests
├── docs/architecture.md
├── conftest.py                # fake DB used by the unit tests
├── .github/workflows/ci.yml
├── .env.example
├── requirements.txt
└── README.md
```

## Getting started

1. **Accounts and keys**, you'll need:
   - GitHub, two fine-grained tokens on the target repo: a read token (Issues read, Pull requests read, Metadata read) and a write token (Issues read and write). Create them at https://github.com/settings/personal-access-tokens
   - Neon (free tier): https://neon.tech. Copy the pooled connection string.
   - Groq, for the triage agent and the eval: https://console.groq.com/keys
   - Logfire (optional, tracing just no-ops without it): https://logfire.pydantic.dev

2. **Install**
   ```
   python3 -m venv .venv && source .venv/bin/activate
   pip install -r requirements.txt
   ```

3. **Database**, no local `psql` needed. Open your Neon project's **SQL Editor**, paste in [`db/schema.sql`](db/schema.sql), and run it. If you are upgrading an existing database, run [`db/migrations/001_hardening.sql`](db/migrations/001_hardening.sql) instead.

4. **Env files.** Keep the write token out of the read-only processes' file.

   `.env`, read by the MCP server, triage agent, and scripts:
   ```
   NEON_DSN=postgresql://...
   GITHUB_READ_PAT=...
   GROQ_API_KEY=...
   ```
   `.env.dashboard`, read by the dashboard only:
   ```
   NEON_DSN=postgresql://...
   GITHUB_READ_PAT=...
   GITHUB_WRITE_PAT=...
   DASHBOARD_ACCESS_TOKEN=a-long-random-string
   ```
   Run `chmod 600 .env.dashboard`. Both files are in `.gitignore`. All other settings are optional, see the table below.

5. **Allowlist a repo.** Nothing works on a repo until this is done.
   ```
   python scripts/allowlist.py add owner/repo
   ```

## Running it

Run these from the repo root.

| Command | What it does |
|---|---|
| `ISSUEOPS_ENV_FILE=.env.dashboard streamlit run dashboard/app.py` | Approval dashboard |
| `python -m agent.triage owner/repo --max-issues 5` | Queue proposals from the triage agent |
| `python -m mcp_server.server` | MCP server over stdio |
| `python scripts/custom_client.py list_issues '{"repo": "owner/repo"}'` | Smoke-test one MCP tool |
| `python scripts/prune_audit_log.py --days 90` | Delete audit rows older than 90 days |

To use the MCP server from Claude Desktop, add this to `claude_desktop_config.json` with absolute paths, then restart it:
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

In the dashboard, enter the access token and your name in the sidebar, expand a pending action, click **Load source issue**, then **Approve** or **Reject**. Check the audit log at the bottom of the page and the issue on GitHub.

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `NEON_DSN` | required | Postgres connection string (pooled) |
| `GITHUB_READ_PAT` | required | Read token |
| `GITHUB_WRITE_PAT` | dashboard only | Write token |
| `GROQ_API_KEY` | triage agent and eval | LLM key |
| `GROQ_API_KEY_FALLBACK` | none | Second key tried on rate limits |
| `LOGFIRE_TOKEN` | none | Enables tracing |
| `PENDING_ACTION_TTL_HOURS` | 48 | How long a proposal can wait |
| `STUCK_APPROVING_RECOVERY_MINUTES` | 10 | When an `approving` row is treated as crashed |
| `COMMENT_BODY_MAX_CHARS` | 65536 | Maximum proposed comment length |
| `MAX_PENDING_PER_ISSUE` | 10 | Open proposals per issue |
| `MAX_PENDING_PER_INITIATOR` | 500 | Open proposals per initiator |
| `MCP_CLIENT_LABEL` | none | Label recorded as the MCP initiator |
| `DASHBOARD_ACCESS_TOKEN` | none | Gates the dashboard |
| `ISSUEOPS_ENV_FILE` | auto-discovered `.env` | Env file for this process |

## Testing

Run `pytest` from the repo root.

Most tests use the fake database in `conftest.py` to check SQL call sequences, and mocks for the GitHub clients. Those cannot verify locking. `tests/test_integration_postgres.py` runs against a real Postgres and covers concurrent approvals, lease reclamation, concurrent dedup, queue caps, and the status constraint. It is skipped unless `TEST_DATABASE_URL` points at a scratch database. Each test creates and drops its own schema, and CI runs them against a Postgres service container.

## Evaluation

```
python -m eval.eval path/to/labels.json
```

Copy [`eval/labels_template.json`](eval/labels_template.json) as a starting point.

- **Classification accuracy** on non-adversarial issues, compared with `expected_labels`.
- **Adversarial behavior** on issues containing injected instructions, where the correct outcome is no action. `adversarial_any_action_rate` counts any proposal the plan would queue, and `proposal_level_susceptibility` also counts injection markers appearing in the model output.
- **Audit consistency**, checked in both directions between `audit_log` and `pending_actions`. Both tables are written by this code, so this catches bookkeeping bugs, not a write made outside the dashboard with the write token. To catch that, review the write token's activity in GitHub's audit log.

No labeled dataset is shipped, only the template.

## Known limitations

- The injection heuristic is advisory. An attacker only has to avoid the listed phrases.
- Approver identity in the dashboard is a typed name, not authentication. The access token gates the app but does not tell approvers apart, and its throttle is a speed bump, not a defense.
- Recovery of a row stuck in `approving` runs on dashboard page loads, not in a background worker, and it is time-based rather than a liveness check. In the rare case where the original call was only slow, the result is reported as `lost_lease_after_execution` and a person must check GitHub for a duplicate.
- The stale check does not cover new comments or edits to existing comments. A stale or failed action is terminal, so the agent can re-propose it on a later run but a person cannot retry it.
- `propose_remove_labels` calls GitHub once per label and can partially succeed. The failure message lists what was removed and what was not.
- The label and assignee caches are process-local with a 5 minute TTL and are not shared across workers.
- Very large repos can hit the pagination limit (20 pages, 2000 items by default). The call fails loudly instead of under-reporting. Narrow the query or raise `--max-pages`.
- The MCP initiator string includes the process id, so the per-initiator queue cap is per process, not per person.
- No dependency lockfile or license file is included. Choosing a license is up to the repository owner.