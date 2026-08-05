from datetime import datetime, timezone
from app.providers.email.base import EmailObject
from app.agents.tools.stage0_bulk_filter import run_stage0_filter


def _email(**overrides) -> EmailObject:
    defaults = dict(
        email_id="1", sender_name="Sender", sender_email="sender@example.com",
        recipients=["me@example.com"], subject="Subject", body_text="Body",
        timestamp=datetime.now(timezone.utc),
    )
    defaults.update(overrides)
    return EmailObject(**defaults)


def test_list_unsubscribe_header_marks_promotional():
    email = _email(list_unsubscribe="<mailto:unsub@example.com>")
    result = run_stage0_filter(email)
    assert result.is_promotional is True


def test_bulk_precedence_marks_promotional():
    email = _email(precedence="bulk")
    result = run_stage0_filter(email)
    assert result.is_promotional is True


def test_no_signals_is_inconclusive():
    email = _email()
    result = run_stage0_filter(email)
    assert result.is_promotional is False


def test_normal_email_precedence_not_flagged():
    email = _email(precedence="normal")
    result = run_stage0_filter(email)
    assert result.is_promotional is False
