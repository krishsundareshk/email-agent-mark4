"""
link_extractor — regex candidate meeting-link extraction (specs v3 §7).

Feeds Stage 2's LLM extraction. This is the "real" home for the
_MEETING_LINK_PATTERNS regexes that used to live dormant in
detect_meeting.py (pipeline_changes §2).
"""
import re

_MEETING_LINK_PATTERNS = [
    re.compile(r"https?://meet\.google\.com/[a-z0-9\-]+", re.I),
    re.compile(r"https?://[\w.-]*zoom\.us/j/\d+[^\s<>\"']*", re.I),
    re.compile(r"https?://teams\.microsoft\.com/l/meetup-join/[^\s<>\"']+", re.I),
    re.compile(r"https?://[\w.-]*webex\.com/[^\s<>\"']+", re.I),
    re.compile(r"https?://[\w.-]*gotomeeting\.com/[^\s<>\"']+", re.I),
]


def extract_candidate_links(text: str) -> list[str]:
    """
    Return every candidate video-conferencing link found in `text`,
    de-duplicated, order preserved. Used by Stage 1's Observe step (as a
    structural-corroboration signal for the Meeting flag) and passed to
    Stage 2 as extraction hints.
    """
    if not text:
        return []
    found: list[str] = []
    seen = set()
    for pattern in _MEETING_LINK_PATTERNS:
        for match in pattern.findall(text):
            if match not in seen:
                seen.add(match)
                found.append(match)
    return found
