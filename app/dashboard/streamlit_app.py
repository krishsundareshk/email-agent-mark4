"""
DEPRECATED — this Streamlit dashboard was NOT updated for the specs v3
rewrite (pipeline_changes.md §7 targets the React frontend at
frontend/src/App.jsx as the maintained dashboard). It still runs against
the new API (all its API field access uses dict.get() with defaults, so
it degrades gracefully) but shows stale Priority-based filters/labels and
won't reflect Relationship, confidence_tier, meeting/department badges,
the human-correction action, or the Analytics tab.

Left in the repo for reference only. Use frontend/ instead.
"""
import streamlit as st
import requests
from datetime import datetime, timezone

# ─────────────────────────────────────────────────────────────────────────────
# Config
# ─────────────────────────────────────────────────────────────────────────────

API_BASE = "http://localhost:8000"

st.set_page_config(
    page_title="Email Agent Dashboard (DEPRECATED — see frontend/)",
    page_icon="📧",
    layout="wide",
)

# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def api_get(path: str, params: dict = None):
    """GET request to FastAPI backend. Returns JSON or None on error."""
    try:
        resp = requests.get(f"{API_BASE}{path}", params=params, timeout=5)
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        st.error(f"API error: {e}")
        return None


def api_post(path: str) -> dict:
    """POST request to FastAPI backend. Returns JSON or error dict."""
    try:
        resp = requests.post(f"{API_BASE}{path}", timeout=5)
        return resp.json()
    except Exception as e:
        return {"error": str(e)}


def time_ago(iso_str: str) -> str:
    """Convert ISO datetime string to human-readable 'X minutes ago'."""
    if not iso_str:
        return "Unknown"
    try:
        dt = datetime.fromisoformat(iso_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        diff = datetime.now(timezone.utc) - dt
        minutes = int(diff.total_seconds() / 60)
        if minutes < 1:
            return "just now"
        elif minutes == 1:
            return "1 minute ago"
        elif minutes < 60:
            return f"{minutes} minutes ago"
        elif minutes < 120:
            return "1 hour ago"
        else:
            return f"{minutes // 60} hours ago"
    except Exception:
        return "Unknown"


PRIORITY_COLORS = {
    "High": "🔴",
    "Medium": "🟡",
    "Low": "🟢",
}

INTENT_ICONS = {
    "Request": "📋",
    "Complaint": "😤",
    "Inquiry": "❓",
    "Spam": "🚫",
    "Notification": "🔔",
    "Other": "📄",
}


# ─────────────────────────────────────────────────────────────────────────────
# Header — Batch Run Status
# ─────────────────────────────────────────────────────────────────────────────

st.title("📧 Email Agent Dashboard")

batch = api_get("/api/batch/latest")

if batch and batch.get("found"):
    status = batch.get("status", "Unknown")
    col1, col2, col3, col4 = st.columns(4)

    with col1:
        icon = "✅" if status == "Success" else "⚠️" if status == "PartialFailure" else "❌" if status == "Failed" else "🔄"
        st.metric("Last Run Status", f"{icon} {status}")
    with col2:
        st.metric("Last Synced", time_ago(batch.get("completed_at")))
    with col3:
        st.metric("Emails Classified", batch.get("emails_classified", 0))
    with col4:
        st.metric("Meetings Detected", batch.get("meetings_detected", 0))

    if status == "Failed" and batch.get("error_message"):
        st.error(f"❌ Last batch run failed: {batch['error_message']}")

    if batch.get("emails_deferred", 0) > 0:
        st.warning(
            f"⚠️ {batch['emails_deferred']} emails were deferred to the next run due to high volume."
        )
else:
    st.info("No batch runs found. The scheduler will run automatically, or trigger one manually.")

st.divider()

# ─────────────────────────────────────────────────────────────────────────────
# Meeting Cards Section
# ─────────────────────────────────────────────────────────────────────────────

st.subheader("📅 Meeting Invitations")

meetings = api_get("/api/meetings/pending") or []

if not meetings:
    st.caption("No pending meeting invitations.")
else:
    for meeting in meetings:
        with st.container(border=True):
            col_title, col_action = st.columns([3, 1])

            with col_title:
                st.markdown(f"### {meeting['meeting_title']}")
                dt_str = meeting.get("meeting_datetime", "")
                if dt_str:
                    try:
                        dt = datetime.fromisoformat(dt_str)
                        st.caption(
                            f"📆 {dt.strftime('%B %d, %Y at %I:%M %p UTC')} "
                            f"· ⏱ {meeting.get('duration_minutes', 60)} min"
                        )
                    except Exception:
                        st.caption(dt_str)

                organizer = meeting.get("organizer_name") or meeting.get("organizer_email", "Unknown")
                st.caption(f"👤 Organizer: {organizer}")

                attendees = meeting.get("attendees", [])
                if attendees:
                    st.caption(f"👥 Attendees: {', '.join(attendees)}")

                location = meeting.get("location_or_link")
                if location:
                    st.caption(f"📍 {location}")

                st.markdown(f"**Purpose:** {meeting.get('meeting_summary', '')}")

            with col_action:
                meeting_id = meeting["meeting_id"]

                if st.button("📅 Add to Calendar", key=f"confirm_{meeting_id}"):
                    result = api_post(f"/api/meetings/{meeting_id}/confirm")
                    if result.get("success"):
                        st.success("✅ Added to Google Calendar!")
                        st.rerun()
                    else:
                        st.error(
                            result.get("error_message") or
                            result.get("detail") or
                            "Failed to add to calendar."
                        )

                if st.button("❌ Dismiss", key=f"dismiss_{meeting_id}"):
                    result = api_post(f"/api/meetings/{meeting_id}/dismiss")
                    if result.get("success"):
                        st.success("Dismissed.")
                        st.rerun()
                    else:
                        st.error(
                            result.get("detail") or "Failed to dismiss."
                        )

st.divider()

# ─────────────────────────────────────────────────────────────────────────────
# Sidebar Filters
# ─────────────────────────────────────────────────────────────────────────────

st.sidebar.header("🔍 Filter Emails")

priority_filter = st.sidebar.selectbox(
    "Priority",
    options=["All", "High", "Medium", "Low"],
)

intent_filter = st.sidebar.selectbox(
    "Intent Type",
    options=["All", "Request", "Complaint", "Inquiry", "Spam", "Notification", "Other"],
)

dept_filter = st.sidebar.selectbox(
    "Department",
    options=["All", "HR", "Finance", "IT", "Legal", "Operations", "General"],
)

show_flagged_only = st.sidebar.checkbox("Show 'Needs Review' only")

st.sidebar.divider()
if st.sidebar.button("🔄 Refresh Dashboard"):
    st.rerun()

# ─────────────────────────────────────────────────────────────────────────────
# Email List
# ─────────────────────────────────────────────────────────────────────────────

st.subheader("📬 Email Summary")

params = {}
if priority_filter != "All":
    params["priority"] = priority_filter
if intent_filter != "All":
    params["intent_type"] = intent_filter
if dept_filter != "All":
    params["department"] = dept_filter

emails = api_get("/api/emails/", params=params) or []

if show_flagged_only:
    emails = [e for e in emails if e.get("flagged_for_review")]

if not emails:
    st.caption("No emails found. Adjust filters or wait for the next batch run.")
else:
    st.caption(f"Showing {len(emails)} email(s)")

    for email in emails:
        priority = email.get("priority", "Low")
        intent = email.get("intent_type", "Other")
        dept = email.get("department", "General")
        flagged = email.get("flagged_for_review", False)
        is_meeting = email.get("is_meeting", False)

        priority_icon = PRIORITY_COLORS.get(priority, "⚪")
        intent_icon = INTENT_ICONS.get(intent, "📄")

        # Build card title
        title_parts = [
            f"{priority_icon} **{priority}**",
            f"{intent_icon} {intent}",
            f"🏢 {dept}",
        ]
        if flagged:
            title_parts.append("🔍 **Needs Review**")
        if is_meeting:
            title_parts.append("📅 Meeting")

        with st.expander(" · ".join(title_parts)):
            st.markdown(email.get("summary", "No summary available."))

            meta_cols = st.columns(3)
            with meta_cols[0]:
                st.caption(f"📧 ID: `{email.get('email_id', '')[:16]}...`")
            with meta_cols[1]:
                confidence = email.get("classification_confidence", 0)
                st.caption(f"🎯 Confidence: {confidence:.0%}")
            with meta_cols[2]:
                processed = email.get("processed_at", "")
                st.caption(f"🕐 {time_ago(processed)}")