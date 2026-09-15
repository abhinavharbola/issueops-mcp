# IssueOps MCP

A guarded MCP server for GitHub issue triage. The model-facing surface has ten tools: five read-only,
five that queue a proposal for a human to approve. There is no `execute` or `confirm` tool anywhere on
that surface, for any client. That is the entire premise of the project.

## Interoperability

`mcp_server/server.py` is a single stdio MCP server. It has been called successfully from both:

- Claude Desktop, via its MCP config pointing at `python -m mcp_server.server`
- A minimal custom client (`tests/custom_client.py`), which spawns the same server over stdio and calls
  the same tool

*(Run status: not yet executed in this environment — no live Neon DB, GitHub PAT, or Claude Desktop
instance was available while building. See "What's unverified" below for exactly what to run and what to
paste back.)*

## Architecture

```
Claude Desktop  ─┐
                  ├─ stdio ─> mcp_server/server.py ─> issueops/tools.py ─┬─> GitHub REST (read PAT only)
custom client   ─┘                                                       └─> Neon (pending_actions, audit_log)

agent/triage.py (CLI or cron) ───────────────────────> issueops/tools.py ─┬─> GitHub REST (read PAT only)
   never opens an MCP session                                             └─> Neon

dashboard/app.py (Streamlit) ───> dashboard/actions.py ─┬─> GitHub REST (write PAT — only process that holds it)
   human approve/reject                                  └─> Neon (pending_actions, audit_log)
```

`issueops/tools.py` is the only module both the MCP server and the triage agent import. It has zero MCP
or Streamlit imports. The write-capable `GitHubWriteClient` is only ever constructed inside
`dashboard/actions.py`.

The triage agent, whether run interactively (`python -m agent.triage <repo>`) or from a scheduled job,
calls `issueops.tools` directly. It never opens an MCP client session. A GitHub Actions runner triggering
the triage agent is not doing stdio to a local process — it is running a normal Python script.

## Eval numbers

Not yet run — there is no hand-labeled eval set or scratch repo in this environment. `eval/eval.py`
computes, honestly, once you provide `eval/labels.json` (copy `eval/labels_template.json` and fill in a
real repo and 20-30 hand-labeled issues):

- **Proposal-level susceptibility** — how often the agent proposed a malicious or nonsensical action on
  an adversarial issue. Expected to be non-zero; that number will be reported as-is, not massaged.
- **Execution-level guarantee** — a SQL check that every `audit_log` row recording a completed GitHub
  mutation is backed by a `pending_actions` row with `status = 'executed'` and a non-null `approved_by`.
  This number must be zero. See "Ambiguity I flagged" below for how I scoped this check.
- Label accuracy on the legitimate subset (exact set match, no judge model) and latency per triage run.

The heuristic keyword flag (`agent/heuristics.py`) is advisory only. It is surfaced in the dashboard as a
signal, never checked anywhere in the approval or execution path, and it is not a security boundary. The
actual guarantee is architectural: no execute path exists on the model-facing surface, full stop.

## Setup

1. Create a Neon Postgres database, apply `db/schema.sql`.
2. Copy `.env.example` to `.env`, fill in `NEON_DSN` (use Neon's **pooled** connection string, Streamlit
   reruns the whole script on every interaction and will exhaust a direct connection fast),
   `GITHUB_READ_PAT`, `GITHUB_WRITE_PAT`, `GROQ_API_KEY`, `LOGFIRE_TOKEN`. Optionally, `GROQ_API_KEY_FALLBACK`
   if you have a second Groq account — the triage agent and eval script fail over to it automatically on a
   429 from the first, retrying once more after a short wait if both accounts are rate-limited at the same
   moment. Leave it unset for a single-account setup.
3. `pip install -r requirements.txt`
4. Allowlist a scratch repo you own: `python scripts/allowlist.py add owner/scratch-repo`
5. Verify the MCP server: `python -m mcp_server.server` (should idle on stdio, Ctrl+C to stop), then
   `python tests/custom_client.py list_issues '{"repo": "owner/scratch-repo"}'`
6. Point Claude Desktop's MCP config at `python -m mcp_server.server` with `cwd` set to the repo root.
   `config.py` calls `load_dotenv()`, which finds `.env` by walking up from the process's working
   directory — so with `cwd` set correctly, Claude Desktop's spawned server process picks up your `.env`
   on its own. You do **not** need to duplicate the credentials into an explicit `env` block for this to
   work. Example config:
   ```json
   {
     "mcpServers": {
       "issueops-mcp": {
         "command": "python3",
         "args": ["-m", "mcp_server.server"],
         "cwd": "/absolute/path/to/issueops-mcp"
       }
     }
   }
   ```
   If your MCP client doesn't run with your intended `cwd`, or you'd rather not rely on file discovery,
   an explicit `env` block also works — the MCP SDK does not inherit the parent process's full
   environment for stdio-spawned servers (it only passes a small fixed allowlist like `PATH`, `HOME`), so
   without either `cwd`-based `.env` discovery or an explicit `env` block, the server has no credentials
   at all and fails at startup with a clear `RuntimeError`.
   Confirm the same tool call returns the same shape of result as the custom client.
7. Run the dashboard: `streamlit run dashboard/app.py`
8. Run the agent once by hand: `python -m agent.triage owner/scratch-repo --max-issues 3`
9. Hand-label 20-30 issues into `eval/labels.json`, run `python -m eval.eval eval/labels.json`

## What's unverified

Everything above is written and syntax-checked (`python -m py_compile` on every file, and the MCP server
was instantiated in-process to confirm all ten tools register with the correct schemas), but none of it
has touched a real Neon database or the real GitHub API, and no one has run the Claude Desktop side of
the interoperability check. That is the trial-run pass we agreed to do next.

## Fixes applied after a code review pass

- **`propose_close` validation was dead code.** The `reason` check was defined but never wired into
  `_queue_proposal`'s `validate_fn`. An invalid `state_reason` would have been queued and only failed
  at GitHub API time on execute, instead of at proposal time like every other `propose_*` tool. Fixed by
  passing `validate_fn=validate`.
- **`propose_assign` called an endpoint outside the read PAT's declared scope.** See the collaborator
  note above. Downgraded to a syntactic check; GitHub enforces the real constraint at execute time.
- **The triage agent had no per-issue error isolation.** A single malformed-but-valid-JSON classification
  (right shape violated, e.g. `labels_to_add` as a string instead of a list) would raise inside
  `_plan_from_classification` and abort the entire batch. `classify_issue` now validates the classification's
  shape before returning it, and `run_triage`'s per-issue body is wrapped so one bad issue is recorded as
  an error in that issue's result and the run continues.
- **The heuristic phrase list included `"you must"`,** a phrase that shows up constantly in ordinary bug
  report templates ("you must provide steps to reproduce"). Since this drives an advisory flag shown to
  the operator, a high false-positive rate erodes its usefulness fast. Replaced with more specific phrases.

## Second review pass, after the first round of fixes

- **`VALID_CLOSE_REASONS` was duplicated** in `issueops/tools.py` and `agent/triage.py` as two separately
  maintained set literals. `agent/triage.py` now imports the constant from `issueops.tools` instead of
  redefining it, so there's one source of truth.
- **The first fix for the triage agent's classification validation was all-or-nothing.** A single
  malformed field (e.g. a garbage `close_reason`) rejected the entire classification, silently discarding
  a valid `labels_to_add` and `comment` from the same response. Replaced with per-field sanitization: a
  malformed field is dropped and noted in `rationale`, valid fields from the same response still turn
  into proposals. The classification is only rejected outright if the model's output isn't a JSON object
  at all. Verified with a scripted test: a response with a valid label and comment but a garbage
  `close_reason` now still produces `propose_add_labels` and `propose_add_comment`, where the previous
  fix produced neither.
- Verified with scripted mocks (not against live GitHub or Neon, that's still the pending trial run):
  `propose_close` rejects an invalid `reason` and accepts a valid one, `propose_assign` rejects an empty
  or malformed login and accepts a well-formed one, and `run_triage` isolates a per-issue failure
  (simulated Groq exception on one issue in a three-issue batch) without dropping the other two issues'
  results.

## Third review pass

- **`propose_remove_labels` could misreport a partial GitHub mutation as a clean failure.** GitHub has no
  batch label-removal endpoint, so multi-label removal is N sequential DELETE calls. If label 2 of 3
  failed (already removed by someone else, permissions, rate limit), the old code raised a bare exception
  and the whole action was marked `failed` in `pending_actions` and `audit_log`, with no record that label
  1 had, in fact, already been removed from GitHub. That's a real gap against Section 11.2's premise that
  `audit_log` is the ground truth for what happened. `_execute_on_github` now tracks which labels
  succeeded before a failure and puts that in the exception message, so `failure_reason` reads e.g.
  `"removed ['bug'] before failing on 'wontfix' (404: label not found); never attempted ['duplicate']"`
  instead of just the raw 404 text. The `pending_actions.status` is still just `failed`, no new status was
  added, this is a message-accuracy fix, not a schema change.
- **A GitHub network failure (timeout, DNS, connection reset) bypassed `_translate_errors` entirely** in
  `mcp_server/server.py`. Only `RepoNotAllowedError`, `ValidationError`, and `GitHubAPIError` (HTTP
  4xx/5xx responses) were caught; a `requests.exceptions.ConnectionError` or `Timeout` propagated as a raw
  exception to the MCP client instead of a clean `ToolError`. Now caught alongside the others and wrapped
  with the same message pattern.
- **Confirmed but deliberately not changed:** `_queue_proposal` calls `snapshot_issue_state`, which does
  call GitHub's read API (via the read PAT) to populate the required `issue_state_snapshot` column.
  Section 7.3's prose ("It never calls the GitHub API") is stricter than Section 9's own schema, which
  makes `issue_state_snapshot` `NOT NULL` and can only be populated by reading the issue. Read Section
  7.3's claim as "never calls GitHub's mutating endpoints," which is what the credential-level guarantee
  in Section 6.3 is actually about, and what the code delivers.
- **Confirmed but deliberately not changed:** `dashboard/actions.py::approve_action` holds a
  `SELECT ... FOR UPDATE` row lock across two live network calls (the stale-state re-fetch and, on success,
  the GitHub write call). For a single operator clicking Approve one at a time, this is harmless. It would
  become a real bottleneck under concurrent approvers, which is explicitly out of scope (Section 4, Section
  14: not multi-operator).

## Choices I made where the PRD was silent, stated rather than silently picked

- **psycopg (v3), not asyncpg.** One driver covers the sync contexts (Streamlit, the interactive agent
  CLI) and the async context (the MCP server) without running two DB libraries for a three-table schema.
- **`tools.py` functions take an explicit `dsn` and GitHub client instance** rather than building them
  from `Config` internally, so the module has no import-time side effects and stays testable.
- **`requested_by` for MCP-originated proposals is a static client label** (`mcp:stdio`), not a human
  identity — stdio MCP carries no auth. Real human attribution only exists at `approved_by`, set by the
  dashboard.
- **Streamlit approver identity is a free-text name field**, not authentication. Flagged loudly in the
  sidebar. If you want real access control here, that's a separate piece of work this PRD doesn't specify.
- **Stale-check diffs `state`, `labels`, `assignees` only**, not the full comment thread — those are the
  only fields a `propose_*` action can conflict with.
- **Dedup comparison normalizes JSONB arguments with sorted keys** before comparing, so two functionally
  identical proposals with differently-ordered keys don't both get inserted.
- **`propose_close`'s `reason` is constrained to GitHub's actual `state_reason` enum** (`completed`,
  `not_planned`, or omitted) — the PRD's "reason" didn't specify this, but GitHub's API rejects anything
  else.
- **`propose_assign` validates the assignee syntactically only** (non-empty, a well-formed GitHub login),
  not against the repo's actual collaborator list. An earlier version called the collaborators endpoint,
  but that requires member/admin-level permission the PRD's read PAT (Issues/PRs read only, Section 6.3)
  doesn't grant. Rather than widen the PAT's scope past what the PRD specifies, invalid assignees are now
  caught by GitHub itself at execute time, the same pattern already used for `propose_close`'s reason.
- **`repo_allowlist.active` and `audit_log.pending_action_id`** are not in the PRD's Section 9 schema.
  `active` lets a repo be deactivated without deleting its row (needed for the Section 7.4 "removed from
  allowlist" re-check and for `pending_actions`' foreign key to still resolve for old rows). `pending_action_id`
  is the join the eval's execution-level-guarantee query (Section 11.2) needs to tie an `audit_log` row
  back to its approval. Both are additive, nothing in Section 9 was removed or changed.
- **Triage agent's classification schema** (`labels_to_add`, `comment`, `close_reason`, `assign_to`) is
  my own vocabulary — the PRD didn't specify one. It doesn't exercise `propose_remove_labels`; that tool
  is only reachable by a human or Claude Desktop.

## Ambiguity I flagged rather than resolved silently

The PRD's execution-level guarantee check says "every `audit_log` row where `tool_name` is a mutating
tool" must have a valid approved-and-executed chain. Read literally, that would also apply to rows with
`result_status = 'proposed'`, `'deduped'`, or `'error'` — none of which represent GitHub was ever touched,
and none of which can point at a pending action with `status = 'executed'` at the moment they're written.
I scoped the check in `eval/eval.py` to `result_status = 'executed'` rows only, since those are the only
rows that assert a real GitHub mutation happened. Tell me if you meant something else.
