import argparse
import json
import sys
import time

from groq import BadRequestError, Groq, RateLimitError

from agent.heuristics import is_heuristically_flagged
from agent.prompts import SYSTEM_PROMPT, build_user_prompt
from issueops import tools
from issueops.config import Config, load_config
from issueops.github_client import GitHubAPIError, GitHubReadClient
from issueops.observability import configure_logfire

DEFAULT_MODEL = "openai/gpt-oss-20b"
JSON_RESPONSE_FORMAT = {"type": "json_object"}

PROPOSE_DISPATCH = {
    "propose_add_labels": lambda dsn, rc, repo, num, args, initiator, flagged, issue: tools.propose_add_labels(
        dsn, rc, repo, num, args["labels"], initiator, heuristic_flagged=flagged, issue=issue
    ),
    "propose_add_comment": lambda dsn, rc, repo, num, args, initiator, flagged, issue: tools.propose_add_comment(
        dsn, rc, repo, num, args["body"], initiator, heuristic_flagged=flagged, issue=issue
    ),
    "propose_close": lambda dsn, rc, repo, num, args, initiator, flagged, issue: tools.propose_close(
        dsn, rc, repo, num, args.get("reason"), initiator, heuristic_flagged=flagged, issue=issue
    ),
    "propose_assign": lambda dsn, rc, repo, num, args, initiator, flagged, issue: tools.propose_assign(
        dsn, rc, repo, num, args["assignee"], initiator, heuristic_flagged=flagged, issue=issue
    ),
}


EMPTY_CLASSIFICATION = {
    "labels_to_add": [],
    "comment": None,
    "close_reason": None,
    "assign_to": None,
    "rationale": None,
}


def _empty_classification(rationale: str) -> dict:
    return {**EMPTY_CLASSIFICATION, "labels_to_add": [], "rationale": rationale}


def _sanitize_classification(data) -> dict:
    if not isinstance(data, dict):
        raise ValueError("classification is not a JSON object")

    dropped = []

    labels = data.get("labels_to_add") or []
    if not isinstance(labels, list) or not all(isinstance(l, str) for l in labels):
        dropped.append("labels_to_add")
        labels = []

    comment = data.get("comment")
    if comment is not None and not isinstance(comment, str):
        dropped.append("comment")
        comment = None

    close_reason = data.get("close_reason")
    if close_reason is not None and (
        not isinstance(close_reason, str) or close_reason not in tools.VALID_CLOSE_REASONS
    ):
        dropped.append(f"close_reason={close_reason!r}")
        close_reason = None

    assign_to = data.get("assign_to")
    if assign_to is not None and not isinstance(assign_to, str):
        dropped.append("assign_to")
        assign_to = None

    rationale = data.get("rationale")
    rationale = rationale if isinstance(rationale, str) else None
    if dropped:
        note = f"dropped malformed field(s): {', '.join(dropped)}"
        rationale = f"{rationale} | {note}" if rationale else note

    return {
        "labels_to_add": labels,
        "comment": comment,
        "close_reason": close_reason,
        "assign_to": assign_to,
        "rationale": rationale,
    }


def _parse_classification(raw_text: str) -> dict:
    cleaned = raw_text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`")
        if cleaned.startswith("json"):
            cleaned = cleaned[4:]
    parsed = json.loads(cleaned)
    return _sanitize_classification(parsed)


def build_groq_clients(config: Config) -> list[Groq]:
    if not config.groq_api_key:
        raise RuntimeError("GROQ_API_KEY is required to build Groq clients")
    clients = [Groq(api_key=config.groq_api_key)]
    if config.groq_api_key_fallback:
        clients.append(Groq(api_key=config.groq_api_key_fallback))
    return clients


def _complete_once(client: Groq, model: str, messages: list[dict]):
    try:
        return client.chat.completions.create(
            model=model, messages=messages, temperature=0, response_format=JSON_RESPONSE_FORMAT,
        )
    except BadRequestError:
        return client.chat.completions.create(model=model, messages=messages, temperature=0)


def _create_completion(groq_clients: list[Groq], model: str, messages: list[dict]):
    last_exc = None
    for client in groq_clients:
        try:
            return _complete_once(client, model, messages)
        except RateLimitError as exc:
            last_exc = exc
            continue

    retry_after = 1.0
    if last_exc is not None:
        header_value = last_exc.response.headers.get("retry-after")
        if header_value:
            try:
                retry_after = float(header_value)
            except ValueError:
                pass
    time.sleep(retry_after)

    for client in groq_clients:
        try:
            return _complete_once(client, model, messages)
        except RateLimitError as exc:
            last_exc = exc
            continue

    raise last_exc


def classify_issue(
    groq_clients: list[Groq], model: str, issue: dict,
    repo_labels: list[str] | None = None, assignable: list[str] | None = None,
) -> dict:
    response = _create_completion(
        groq_clients,
        model,
        [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": build_user_prompt(issue, repo_labels, assignable)},
        ],
    )
    raw_text = response.choices[0].message.content
    if not raw_text:
        return _empty_classification("empty model output")
    try:
        return _parse_classification(raw_text)
    except (json.JSONDecodeError, ValueError) as exc:
        return _empty_classification(f"unparseable model output: {exc}")


def _issue_label_names(issue: dict) -> set[str]:
    return {
        (l["name"] if isinstance(l, dict) else str(l)).lower()
        for l in issue.get("labels", [])
    }


def _issue_assignee_logins(issue: dict) -> set[str]:
    return {a.get("login", "").lower() for a in issue.get("assignees", [])}


def _plan_from_classification(
    classification: dict, repo: str, issue_number: int,
    issue: dict | None = None, repo_labels: list[str] | None = None, assignable: list[str] | None = None,
) -> list[tuple[str, dict]]:
    plan = []

    labels = []
    for label in classification.get("labels_to_add") or []:
        if label not in labels:
            labels.append(label)
    if issue is not None:
        existing = _issue_label_names(issue)
        labels = [l for l in labels if l.lower() not in existing]
    if repo_labels is not None:
        known = set(repo_labels)
        labels = [l for l in labels if l in known]
    if labels:
        plan.append(("propose_add_labels", {"labels": labels}))

    comment = classification.get("comment")
    if comment:
        plan.append(("propose_add_comment", {"body": comment}))

    close_reason = classification.get("close_reason")
    if close_reason and (issue is None or issue.get("state") == "open"):
        plan.append(("propose_close", {"reason": close_reason}))

    assign_to = classification.get("assign_to")
    if assign_to:
        allowed = assignable is None or assign_to.lower() in {a.lower() for a in assignable}
        already = issue is not None and assign_to.lower() in _issue_assignee_logins(issue)
        if allowed and not already:
            plan.append(("propose_assign", {"assignee": assign_to}))
    return plan


def run_triage(
    repo: str, initiator: str, state: str = "open", max_issues: int | None = None,
    model: str = DEFAULT_MODEL, since: str | None = None, max_pages: int = 20,
):
    config = load_config(require_write_pat=False, require_groq=True)
    configure_logfire(config.logfire_token, service_name="issueops-triage-agent")
    read_client = GitHubReadClient(config.github_read_pat)
    groq_clients = build_groq_clients(config)

    dsn = config.neon_dsn
    already_called = set()
    results = []

    issues = tools.list_issues(dsn, read_client, repo, initiator, state=state, since=since, max_pages=max_pages)
    handled = tools.list_handled_issue_numbers(dsn, repo)

    candidates = [i for i in issues if "pull_request" not in i and i.get("number") not in handled]
    skipped = sum(1 for i in issues if "pull_request" not in i and i.get("number") in handled)
    if skipped:
        print(f"skipped {skipped} issue(s) that already have a pending, approved, or rejected agent proposal", file=sys.stderr)
    if max_issues:
        candidates = candidates[:max_issues]
    if not candidates:
        return results

    repo_labels = tools.get_repo_label_names(read_client, repo)
    try:
        assignable = tools.get_repo_assignable_logins(read_client, repo)
    except GitHubAPIError:
        assignable = []

    for summary in candidates:
        issue_number = summary.get("number")
        try:
            if issue_number is None:
                raise ValueError("issue summary is missing a 'number' field")
            issue = tools.get_issue(dsn, read_client, repo, issue_number, initiator)
            flagged = is_heuristically_flagged(tools.issue_plaintext(issue))
            classification = classify_issue(groq_clients, model, issue, repo_labels, assignable)
            plan = _plan_from_classification(classification, repo, issue_number, issue, repo_labels, assignable)

            proposals = []
            for tool_name, args in plan:
                dedup_key = (tool_name, repo, issue_number, json.dumps(args, sort_keys=True))
                if dedup_key in already_called:
                    continue
                already_called.add(dedup_key)
                try:
                    proposal = PROPOSE_DISPATCH[tool_name](dsn, read_client, repo, issue_number, args, initiator, flagged, issue)
                    proposals.append({"tool_name": tool_name, "result": proposal})
                except tools.ValidationError as exc:
                    proposals.append({"tool_name": tool_name, "error": str(exc)})

            results.append(
                {
                    "issue_number": issue_number,
                    "heuristic_flagged": flagged,
                    "classification": classification,
                    "proposals": proposals,
                }
            )
        except Exception as exc:
            results.append(
                {
                    "issue_number": issue_number,
                    "heuristic_flagged": None,
                    "classification": None,
                    "proposals": [],
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )

    return results


def run_scheduled(repo: str, state: str = "open"):
    return run_triage(repo, initiator="agent:scheduled", state=state)


def main():
    parser = argparse.ArgumentParser(description="Run the IssueOps triage agent against one repo.")
    parser.add_argument("repo", help="owner/name of an allowlisted repo")
    parser.add_argument("--state", default="open")
    parser.add_argument("--max-issues", type=int, default=None)
    parser.add_argument("--since", default=None, help="only issues updated at or after this ISO 8601 timestamp")
    parser.add_argument("--max-pages", type=int, default=20)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    args = parser.parse_args()

    results = run_triage(
        args.repo, initiator="agent:cli", state=args.state, max_issues=args.max_issues,
        model=args.model, since=args.since, max_pages=args.max_pages,
    )
    print(json.dumps(results, indent=2, default=str))


if __name__ == "__main__":
    main()
