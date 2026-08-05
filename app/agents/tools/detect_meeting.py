import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from app.providers.email.base import EmailObject
from app.providers.calendar.base import MeetingCard
from app.agents.models.enums import CalendarProviderEnum, MeetingStatus
from app.agents.tools.base import run_llm_agent_tool, safe_int, safe_bool
from app.agents.tools.link_extractor import extract_candidate_links

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Result Dataclass
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class MeetingDetectionResult:
    """
    Result returned by detect_meeting().
    """
    is_meeting: bool
    meeting_card: Optional[MeetingCard] = None
    ambiguity_note: str = ""


# ─────────────────────────────────────────────────────────────────────────────
# System Prompt
# ─────────────────────────────────────────────────────────────────────────────

SYSTEM_PROMPT = """You are a meeting detection assistant for an enterprise email system.

Your job is to analyze an email and determine if it is a meeting invitation.
You must ALWAYS respond with valid JSON and nothing else — no explanations,
no markdown, no code fences, just the raw JSON object.

## Task

Step 1: Determine if this email is a meeting invitation.
A meeting invitation must have:
  - A clear intent to schedule or invite someone to a meeting, call, or event
  - A specific date and/or time (not vague language like "sometime next week")

Step 2: If it IS a meeting invitation, extract all available fields.
Step 3: If it is NOT a meeting invitation, return is_meeting as false.
Step 4: If the email contains scheduling language but NO specific date/time,
        return is_meeting as false and set ambiguous to true.

## Output Format

Always return exactly this JSON structure:

{
  "is_meeting": true or false,
  "ambiguous": true or false,
  "meeting_title": "<title or null>",
  "meeting_datetime": "<ISO 8601 datetime string or null>",
  "duration_minutes": <integer or null>,
  "organizer_name": "<name or null>",
  "organizer_email": "<email or null>",
  "attendees": ["<email1>", "<email2>"] or [],
  "location_or_link": "<location or video link or null>",
  "meeting_summary": "<1-2 sentence purpose summary or null>"
}

## Field Rules

- meeting_datetime: Must be a valid ISO 8601 string, and if the email states or
  implies a timezone, you MUST include it as a UTC offset suffix — never drop
  it. Example: "2:00 PM IST" -> "2026-06-27T14:00:00+05:30", NOT
  "2026-06-27T14:00:00". Common offsets: IST=+05:30, EST=-05:00, EDT=-04:00,
  PST=-08:00, PDT=-07:00, CST=-06:00, CET=+01:00, GMT/UTC=+00:00, BST=+01:00.
  If the email gives an explicit numeric offset (e.g. "UTC+2"), use that
  exact value instead of guessing from an abbreviation. Only omit the offset
  (bare "2026-06-27T14:00:00") if the email truly states no timezone
  anywhere and none can be reasonably inferred — in that case it will be
  treated as UTC, so prefer including an offset whenever you can find one.
  If no specific date/time found, set to null and set ambiguous to true.
- duration_minutes: Integer number of minutes. If not mentioned, set to null.
- attendees: List of email addresses found in the email. Can be empty list.
- location_or_link: Physical address OR video call link (Zoom, Meet, Teams).
  Set to null if neither found.
- meeting_summary: 1-2 sentences describing the purpose of the meeting.
  Set to null if is_meeting is false.
- ambiguous: Set to true ONLY when scheduling language exists but datetime is missing.

## Examples

Example 1 — Clear meeting invite:
Email: "Hi team, you are invited to the Q3 Planning meeting on July 1, 2026 at 2:00 PM IST.
Join via Google Meet: https://meet.google.com/abc-defg-hij. Duration: 1 hour."
Response:
{"is_meeting": true, "ambiguous": false, "meeting_title": "Q3 Planning Meeting",
"meeting_datetime": "2026-07-01T14:00:00+05:30", "duration_minutes": 60,
"organizer_name": null, "organizer_email": null,
"attendees": [], "location_or_link": "https://meet.google.com/abc-defg-hij",
"meeting_summary": "Quarterly planning meeting for Q3 scheduled on July 1st at 2 PM IST via Google Meet."}

Example 2 — Regular email (not a meeting):
Email: "Please find attached the Q2 sales report for your review."
Response:
{"is_meeting": false, "ambiguous": false, "meeting_title": null,
"meeting_datetime": null, "duration_minutes": null,
"organizer_name": null, "organizer_email": null,
"attendees": [], "location_or_link": null, "meeting_summary": null}

Example 3 — Ambiguous (scheduling language, no datetime):
Email: "Hey, would love to catch up sometime next week. Let me know when you are free."
Response:
{"is_meeting": false, "ambiguous": true, "meeting_title": null,
"meeting_datetime": null, "duration_minutes": null,
"organizer_name": null, "organizer_email": null,
"attendees": [], "location_or_link": null,
"meeting_summary": "Sender is suggesting a meeting but no specific date or time has been provided."}

Now analyze the email provided by the user. Return only the JSON object."""


# ─────────────────────────────────────────────────────────────────────────────
# Main Tool Function
# ─────────────────────────────────────────────────────────────────────────────

def detect_meeting(
    email: EmailObject,
    user_id: str,
    calendar_provider: CalendarProviderEnum = CalendarProviderEnum.GOOGLE,
    llm_client = None,
    llm_model: str = None,
) -> MeetingDetectionResult:
    """
    Detect if an email is a meeting invitation and extract metadata.
    """
    user_prompt = _build_user_prompt(email)

    def parser_fn(parsed: dict) -> MeetingDetectionResult:
        is_meeting = safe_bool(parsed.get("is_meeting"), default=False)
        is_ambiguous = safe_bool(parsed.get("ambiguous"), default=False)

        # Path 3 — clean non-meeting
        if not is_meeting and not is_ambiguous:
            return MeetingDetectionResult(is_meeting=False)

        # Path 2 — ambiguous scheduling language
        if not is_meeting and is_ambiguous:
            note = (
                "Note: This email contains scheduling language but no specific "
                "date or time was found. You may want to follow up with the sender."
            )
            logger.info(
                f"Ambiguous meeting language detected in email {email.email_id}."
            )
            return MeetingDetectionResult(is_meeting=False, ambiguity_note=note)

        # Path 1 — is_meeting=True, try to build MeetingCard
        datetime_str = parsed.get("meeting_datetime")
        if not datetime_str:
            # LLM said is_meeting=True but gave no datetime — treat as ambiguous
            logger.warning(
                f"LLM returned is_meeting=True but no meeting_datetime "
                f"for email {email.email_id}. Treating as ambiguous."
            )
            note = (
                "Note: This email may be a meeting invitation but no specific "
                "date or time could be extracted automatically."
            )
            return MeetingDetectionResult(is_meeting=False, ambiguity_note=note)

        # Parse datetime string
        meeting_dt = _parse_datetime(datetime_str, email.email_id)
        if not meeting_dt:
            note = (
                "Note: This email may be a meeting invitation but the date "
                "or time format could not be recognized."
            )
            return MeetingDetectionResult(is_meeting=False, ambiguity_note=note)

        raw_attendees = parsed.get("attendees")
        attendees = raw_attendees if isinstance(raw_attendees, list) else []

        # Build MeetingCard — use email fields as fallbacks for organizer
        meeting_card = MeetingCard(
            meeting_id=str(uuid.uuid4()),
            email_id=email.email_id,
            user_id=user_id,
            meeting_title=parsed.get("meeting_title") or email.subject,
            meeting_datetime=meeting_dt,
            duration_minutes=safe_int(parsed.get("duration_minutes"), default=60),
            organizer_name=parsed.get("organizer_name") or email.sender_name,
            organizer_email=parsed.get("organizer_email") or email.sender_email,
            attendees=attendees,
            location_or_link=parsed.get("location_or_link") or None,
            meeting_summary=parsed.get("meeting_summary") or (
                f"Meeting invitation from {email.sender_name}."
            ),
            status=MeetingStatus.PENDING,
            calendar_provider=calendar_provider,
        )

        logger.info(
            f"Meeting detected in email {email.email_id}: "
            f"'{meeting_card.meeting_title}' on {meeting_card.meeting_datetime}"
        )

        return MeetingDetectionResult(is_meeting=True, meeting_card=meeting_card)

    def fallback_fn() -> MeetingDetectionResult:
        return MeetingDetectionResult(is_meeting=False)

    return run_llm_agent_tool(
        system_prompt=SYSTEM_PROMPT,
        user_prompt=user_prompt,
        parser_fn=parser_fn,
        fallback_fn=fallback_fn,
        llm_client=llm_client,
        llm_model=llm_model,
        logger=logger,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Private Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _build_user_prompt(email: EmailObject) -> str:
    """Build the user prompt from EmailObject fields."""
    candidate_links = extract_candidate_links(email.body_text)
    links_hint = (
        f"\nCandidate video-conferencing links found by regex: {candidate_links}\n"
        if candidate_links
        else ""
    )
    return (
        f"From: {email.sender_name} <{email.sender_email}>\n"
        f"Subject: {email.subject}\n"
        f"{links_hint}\n"
        f"Body:\n{email.body_text[:3000]}\n\n"
        f"Return only the JSON object."
    )


def _parse_datetime(datetime_str: str, email_id: str) -> Optional[datetime]:
    """
    Parse an ISO 8601 datetime string into a timezone-aware datetime.
    Returns None if parsing fails.
    """
    if not datetime_str:
        return None

    cleaned = datetime_str.strip()
    if cleaned.endswith("Z"):
        cleaned = cleaned[:-1] + "+00:00"

    try:
        dt = datetime.fromisoformat(cleaned)
        if dt.tzinfo is None:
            return dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except ValueError:
        pass

    formats = [
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%dT%H:%M",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%Y-%m-%d",
    ]

    for fmt in formats:
        try:
            dt = datetime.strptime(cleaned, fmt)
            return dt.replace(tzinfo=timezone.utc)
        except ValueError:
            continue

    logger.warning(
        f"Could not parse meeting_datetime '{datetime_str}' "
        f"for email {email_id}."
    )
    return None
