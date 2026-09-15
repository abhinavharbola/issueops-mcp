import argparse
import json
import time

from groq import Groq, RateLimitError

from agent.heuristics import is_heuristically_flagged
from agent.prompts import SYSTEM_PROMPT, build_user_prompt
from issueops import tools
from issueops.config import Config, load_config
from issueops.github_client import GitHubReadClient
from issueops.observability import configure_logfire

DEFAULT_MODEL = "openai/gpt-oss-20b"

PROPOSE_DISPATCH = {
    "propose_add_labels": lambda dsn, rc, repo, num, args, initiator, flagged: tools.propose_add_labels(
        dsn, rc, repo, num, args["labels"], initiator, heuristic_flagged=flagged
    ),
    "propose_add_comment": lambda dsn, rc, repo, num, args, initiator, flagged: tools.propose_add_comment(
        dsn, rc, repo, num, args["body"], initiator, heuristic_flagged=flagged
    ),
    "propose_close": lambda dsn, rc, repo, num, args, initiator, flagged: tools.propose_close(
        dsn, rc, repo, num, args.get("reason"), initiator, heuristic_flagged=flagged
    ),
    "propose_assign": lambda dsn, rc, repo, num, args, initiator, flagged: tools.propose_assign(
        dsn, rc, repo, num, args["assignee"], initiator, heuristic_flagged=flagged
    ),
}


def _issue_plaintext(issue: dict) -> str:
    comments_text = " ".join(c.get("body", "") for c in issue.get("comments_detail", []))
    return f"{issue.get('title', '')} {issue.get('body') or ''} {comments_text}"


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
    if close_reason not in tools.VALID_CLOSE_REASONS:
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
    clients = [Groq(api_key=config.groq_api_key)]
    if config.groq_api_key_fallback:
        clients.append(Groq(api_key=config.groq_api_key_fallback))
    return clients


def _create_completion(groq_clients: list[Groq], model: str, messages: list[dict]):
    last_exc = None
    for client in groq_clients:
        try:
            return client.chat.completions.create(model=model, messages=messages, temperature=0)
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
            return client.chat.completions.create(model=model, messages=messages, temperature=0)
        except RateLimitError as exc:
            last_exc = exc
            continue

    raise last_exc


def classify_issue(groq_clients: list[Groq], model: str, issue: dict) -> dict:
    response = _create_completion(
        groq_clients,
        model,
        [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": build_user_prompt(issue)},
        ],
    )
    raw_text = response.choices[0].message.content
    if not raw_text:
        return _empty_classification("empty model output")
    try:
        return _parse_classification(raw_text)
    except (json.JSONDecodeError, ValueError) as exc:
        return _empty_classification(f"unparseable model output: {exc}")


def _plan_from_classification(classification: dict, repo: str, issue_number: int) -> list[tuple[str, dict]]:
    plan = []
    labels = classification.get("labels_to_add") or []
    if labels:
        plan.append(("propose_add_labels", {"labels": labels}))
    comment = classification.get("comment")
    if comment:
        plan.append(("propose_add_comment", {"body": comment}))
    close_reason = classification.get("close_reason")
    if close_reason:
        plan.append(("propose_close", {"reason": close_reason}))
    assign_to = classification.get("assign_to")
    if assign_to:
        plan.append(("propose_assign", {"assignee": assign_to}))
    return plan


def run_triage(repo: str, initiator: str, state: str = "open", max_issues: int | None = None, model: str = DEFAULT_MODEL):
    config = load_config(require_write_pat=False)
    configure_logfire(config.logfire_token, service_name="issueops-triage-agent")
    read_client = GitHubReadClient(config.github_read_pat)
    groq_clients = build_groq_clients(config)

    dsn = config.neon_dsn
    already_called = set()
    results = []

    issues = tools.list_issues(dsn, read_client, repo, initiator, state=state)
    if max_issues:
        issues = issues[:max_issues]

    for summary in issues:
        if "pull_request" in summary:
            continue

        issue_number = summary["number"]
        try:
            issue = tools.get_issue(dsn, read_client, repo, issue_number, initiator)
            flagged = is_heuristically_flagged(_issue_plaintext(issue))
            classification = classify_issue(groq_clients, model, issue)
            plan = _plan_from_classification(classification, repo, issue_number)

            proposals = []
            for tool_name, args in plan:
                dedup_key = (tool_name, repo, issue_number, json.dumps(args, sort_keys=True))
                if dedup_key in already_called:
                    continue
                already_called.add(dedup_key)
                try:
                    proposal = PROPOSE_DISPATCH[tool_name](dsn, read_client, repo, issue_number, args, initiator, flagged)
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
    parser.add_argument("--model", default=DEFAULT_MODEL)
    args = parser.parse_args()

    results = run_triage(args.repo, initiator="agent:cli", state=args.state, max_issues=args.max_issues, model=args.model)
    print(json.dumps(results, indent=2, default=str))


if __name__ == "__main__":
    main()
