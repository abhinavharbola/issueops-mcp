import issueops.tools as tools


def test_issue_plaintext_joins_title_body_and_comments():
    issue = {
        "title": "crash on save",
        "body": "steps to reproduce",
        "comments_detail": [{"body": "can confirm"}, {"body": "me too"}],
    }

    text = tools.issue_plaintext(issue)

    assert "crash on save" in text
    assert "steps to reproduce" in text
    assert "can confirm" in text
    assert "me too" in text


def test_issue_plaintext_handles_a_missing_body_and_no_comments():
    issue = {"title": "t"}

    text = tools.issue_plaintext(issue)

    assert text.startswith("t")
