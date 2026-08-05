"""
Stage 0 — deterministic, no-LLM bulk-mail pre-filter (specs v3 §2).

Uses only RFC-standard bulk-mail signals (List-Unsubscribe /
List-Unsubscribe-Post, Precedence: bulk|list) — never a hand-maintained
keyword/sender blocklist. Conclusive matches short-circuit straight to
Promotional + apply_label, skipping the LLM entirely. Inconclusive
emails fall through to Stage 1 reasoning, not more rules.
"""
import logging
from dataclasses import dataclass

from app.providers.email.base import EmailObject

logger = logging.getLogger(__name__)

_BULK_PRECEDENCE_VALUES = {"bulk", "list"}


@dataclass
class Stage0Result:
    is_promotional: bool
    signal_summary: str  # fed into Stage 1's Observe step even when inconclusive


def run_stage0_filter(email: EmailObject) -> Stage0Result:
    """
    Deterministic bulk-mail check. Never calls the LLM.

    Conclusive when:
    - List-Unsubscribe header is present (RFC 2369 / RFC 8058), OR
    - Precedence header is "bulk" or "list" (RFC 2076 convention)
    """
    reasons = []

    has_list_unsubscribe = bool(email.list_unsubscribe)
    has_list_unsubscribe_post = bool(email.list_unsubscribe_post)
    precedence = (email.precedence or "").strip().lower()
    has_bulk_precedence = precedence in _BULK_PRECEDENCE_VALUES

    if has_list_unsubscribe:
        reasons.append("List-Unsubscribe header present")
    if has_list_unsubscribe_post:
        reasons.append("List-Unsubscribe-Post header present (RFC 8058 one-click)")
    if has_bulk_precedence:
        reasons.append(f"Precedence: {precedence}")

    is_promotional = has_list_unsubscribe or has_bulk_precedence

    if is_promotional:
        summary = "Stage 0: conclusive — " + "; ".join(reasons)
        logger.info(f"[stage0] {email.email_id} → Promotional ({summary})")
    else:
        summary = "Stage 0: inconclusive — no RFC bulk-mail signals present"

    return Stage0Result(is_promotional=is_promotional, signal_summary=summary)
