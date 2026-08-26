import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd
import requests
import streamlit as st
from dotenv import load_dotenv

load_dotenv()

OPENROUTER_MODEL = "openai/gpt-oss-20b"
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
MIN_TRANSCRIPT_LENGTH = 40
ALLOWED_PRIORITIES = ["Low", "Medium", "High", "Critical"]
ALLOWED_STATUSES = ["Not Started", "In Progress", "Blocked", "Completed"]
ALLOWED_SENTIMENTS = ["Positive", "Neutral", "Tense", "Mixed"]


def initialize_state() -> None:
    defaults = {
        "analysis": None,
        "action_items": pd.DataFrame(columns=["Task", "Owner", "Deadline", "Priority", "Status"]),
        "meeting_history": [],
        "current_transcript": "",
        "selected_meeting_title": "",
        "raw_model_response": "",
    }
    for key, value in defaults.items():
        if key not in st.session_state:
            st.session_state[key] = value


def reset_state() -> None:
    st.session_state.analysis = None
    st.session_state.action_items = pd.DataFrame(
        columns=["Task", "Owner", "Deadline", "Priority", "Status"]
    )
    st.session_state.meeting_history = []
    st.session_state.current_transcript = ""
    st.session_state.selected_meeting_title = ""
    st.session_state.sidebar_meeting_title = ""
    st.session_state.raw_model_response = ""


def get_api_key() -> str | None:
    env_key = os.getenv("OPENROUTER_API_KEY")
    if env_key:
        return env_key
    try:
        return st.secrets.get("OPENROUTER_API_KEY")
    except Exception:
        return None


def build_system_prompt(meeting_type: str, detail_level: str, deadline_horizon: int) -> str:
    current_date = datetime.utcnow().strftime("%Y-%m-%d")
    return f"""
You are ActionOS Meeting Intelligence Engine.

Context:
- Meeting type: {meeting_type}
- Detail level: {detail_level}
- Current date (UTC): {current_date}
- Default deadline horizon: {deadline_horizon} days

Instructions:
1. Read chaotic transcripts carefully.
2. Never invent owners if none are stated. Use "Unassigned".
3. Use "Not specified" when deadlines are unknown.
4. Keep action descriptions concise and outcome-focused.
5. Separate decisions from tasks.
6. Detect blockers and risks explicitly mentioned or strongly implied.
7. Infer priority only when reasonable from urgency words, deadlines, or blockers.
8. Preserve participant names exactly as written.
9. Avoid hallucinations and unsupported facts.
10. Return STRICT valid JSON only.
11. Never wrap JSON in markdown code fences.

Return this exact JSON structure:
{{
  "meeting_summary": "string",
  "sentiment": "Positive | Neutral | Tense | Mixed",
  "productivity_score": 0,
  "decisions": ["decision 1"],
  "risks": ["risk 1"],
  "follow_up": "string",
  "action_items": [
    {{
      "task": "string",
      "owner": "string",
      "deadline": "string",
      "priority": "Low | Medium | High | Critical",
      "status": "Not Started"
    }}
  ]
}}

productivity_score must be an integer between 0 and 100.
""".strip()


def call_openrouter(messages: list[dict[str, str]], model: str = OPENROUTER_MODEL) -> str:
    api_key = get_api_key()
    if not api_key:
        raise ValueError("Missing OPENROUTER_API_KEY. Add it in .env or Streamlit Secrets.")

    payload = {
        "model": model,
        "messages": messages,
        "temperature": 0.2,
        "max_tokens": 2000,
    }

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    try:
        response = requests.post(OPENROUTER_URL, headers=headers, json=payload, timeout=45)
    except requests.exceptions.Timeout as exc:
        raise TimeoutError("OpenRouter request timed out. Please try again.") from exc
    except requests.exceptions.ConnectionError as exc:
        raise ConnectionError("Connection failed. Check internet access and retry.") from exc
    except requests.exceptions.RequestException as exc:
        raise RuntimeError("Unexpected network error while contacting OpenRouter.") from exc

    try:
        data = response.json()
    except ValueError as exc:
        raise ValueError("OpenRouter returned non-JSON data.") from exc

    if response.status_code >= 400:
        err = data.get("error", {}) if isinstance(data, dict) else {}
        message = err.get("message") or f"HTTP {response.status_code} returned by OpenRouter."
        raise RuntimeError(message)

    if isinstance(data, dict) and data.get("error"):
        message = data["error"].get("message", "OpenRouter returned an API error.")
        raise RuntimeError(message)

    choices = data.get("choices", []) if isinstance(data, dict) else []
    if not choices:
        raise ValueError("OpenRouter returned an empty choices array.")

    content = choices[0].get("message", {}).get("content", "")
    if not content or not isinstance(content, str):
        raise ValueError("Model returned an empty response.")

    return content.strip()


def clean_json_response(raw_text: str) -> str:
    cleaned = raw_text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`")
        if cleaned.startswith("json"):
            cleaned = cleaned[4:]
    cleaned = cleaned.strip()

    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start != -1 and end != -1 and end > start:
        return cleaned[start : end + 1]
    return cleaned


def normalize_action_items(items: Any) -> list[dict[str, str]]:
    if not isinstance(items, list):
        return []

    normalized: list[dict[str, str]] = []
    for raw_item in items:
        if not isinstance(raw_item, dict):
            continue

        task = str(raw_item.get("task", "")).strip() or "Untitled task"
        owner = str(raw_item.get("owner", "")).strip() or "Unassigned"
        deadline = str(raw_item.get("deadline", "")).strip() or "Not specified"

        priority = str(raw_item.get("priority", "Medium")).strip().title()
        if priority not in ALLOWED_PRIORITIES:
            priority = "Medium"

        status = str(raw_item.get("status", "Not Started")).strip().title()
        status_map = {
            "Not Started": "Not Started",
            "In Progress": "In Progress",
            "Blocked": "Blocked",
            "Completed": "Completed",
        }
        status = status_map.get(status, "Not Started")

        normalized.append(
            {
                "Task": task,
                "Owner": owner,
                "Deadline": deadline,
                "Priority": priority,
                "Status": status,
            }
        )

    return normalized


def parse_analysis(raw_response: str) -> dict[str, Any]:
    cleaned = clean_json_response(raw_response)
    parsed = json.loads(cleaned)

    if not isinstance(parsed, dict):
        raise ValueError("AI response is not a valid JSON object.")

    sentiment = str(parsed.get("sentiment", "Neutral")).strip().title()
    if sentiment not in ALLOWED_SENTIMENTS:
        sentiment = "Neutral"

    try:
        score = int(parsed.get("productivity_score", 0))
    except (TypeError, ValueError):
        score = 0
    score = max(0, min(score, 100))

    decisions = parsed.get("decisions", [])
    if not isinstance(decisions, list):
        decisions = []
    decisions = [str(item).strip() for item in decisions if str(item).strip()]

    risks = parsed.get("risks", [])
    if not isinstance(risks, list):
        risks = []
    risks = [str(item).strip() for item in risks if str(item).strip()]

    analysis = {
        "meeting_summary": str(parsed.get("meeting_summary", "No summary provided.")).strip()
        or "No summary provided.",
        "sentiment": sentiment,
        "productivity_score": score,
        "decisions": decisions,
        "risks": risks,
        "follow_up": str(parsed.get("follow_up", "No follow-up recommendation provided.")).strip()
        or "No follow-up recommendation provided.",
        "action_items": normalize_action_items(parsed.get("action_items", [])),
    }
    return analysis


def analyze_transcript(
    transcript: str,
    meeting_type: str,
    detail_level: str,
    deadline_horizon: int,
) -> bool:
    system_prompt = build_system_prompt(meeting_type, detail_level, deadline_horizon)
    user_prompt = (
        "Extract structured meeting intelligence from this transcript:\n\n"
        f"{transcript.strip()}"
    )

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]

    try:
        raw_response = call_openrouter(messages)
        st.session_state.raw_model_response = raw_response
        analysis = parse_analysis(raw_response)
    except json.JSONDecodeError:
        st.error("AI returned invalid JSON. Please retry or adjust transcript quality.")
        if st.session_state.raw_model_response:
            with st.expander("Debug response"):
                st.code(st.session_state.raw_model_response)
        return False
    except ValueError as exc:
        st.error(f"Response parsing failed: {exc}")
        if st.session_state.raw_model_response:
            with st.expander("Debug response"):
                st.code(st.session_state.raw_model_response)
        return False
    except TimeoutError as exc:
        st.error(str(exc))
        return False
    except ConnectionError as exc:
        st.error(str(exc))
        return False
    except RuntimeError as exc:
        st.error(f"OpenRouter error: {exc}")
        return False
    action_df = pd.DataFrame(analysis["action_items"])
    if action_df.empty:
        action_df = pd.DataFrame(columns=["Task", "Owner", "Deadline", "Priority", "Status"])

    st.session_state.analysis = analysis
    st.session_state.action_items = action_df
    st.toast("Meeting analyzed successfully.")
    return True


def render_kpis(action_df: pd.DataFrame, score: int) -> None:
    total_tasks = int(len(action_df))
    high_critical = int(action_df["Priority"].isin(["High", "Critical"]).sum()) if not action_df.empty else 0
    unique_owners = int(action_df["Owner"].replace("", "Unassigned").nunique()) if not action_df.empty else 0
    unassigned_count = int((action_df["Owner"] == "Unassigned").sum()) if not action_df.empty else 0

    col1, col2, col3, col4 = st.columns(4)
    col1.metric("Total Action Items", total_tasks)
    col2.metric("High/Critical Tasks", high_critical)
    col3.metric("Unique Owners", unique_owners)
    col4.metric("Productivity Score", score, delta=score - 70, delta_color="normal")

    st.caption(f"Unassigned tasks: {unassigned_count}")


def render_summary(analysis: dict[str, Any]) -> None:
    score = int(analysis.get("productivity_score", 0))
    sentiment = analysis.get("sentiment", "Neutral")

    if score >= 80:
        st.success(f"Meeting Health: Strong ({score}/100) | Sentiment: {sentiment}")
    elif score >= 60:
        st.info(f"Meeting Health: Stable ({score}/100) | Sentiment: {sentiment}")
    else:
        st.warning(f"Meeting Health: Needs Attention ({score}/100) | Sentiment: {sentiment}")

    st.subheader("Meeting Summary")
    st.write(analysis.get("meeting_summary", "No summary available."))

    st.subheader("Follow-Up Recommendation")
    st.write(analysis.get("follow_up", "No recommendation available."))


def render_action_editor(action_df: pd.DataFrame) -> pd.DataFrame:
    st.subheader("Editable Action Items")

    edited_df = st.data_editor(
        action_df,
        key="action_editor",
        use_container_width=True,
        num_rows="dynamic",
        column_config={
            "Priority": st.column_config.SelectboxColumn(
                "Priority",
                options=ALLOWED_PRIORITIES,
                required=True,
            ),
            "Status": st.column_config.SelectboxColumn(
                "Status",
                options=ALLOWED_STATUSES,
                required=True,
            ),
        },
        disabled=False,
    )

    for col in ["Task", "Owner", "Deadline", "Priority", "Status"]:
        if col not in edited_df.columns:
            edited_df[col] = ""

    edited_df = edited_df[["Task", "Owner", "Deadline", "Priority", "Status"]].fillna("")
    st.session_state.action_items = edited_df
    return edited_df


def render_charts(action_df: pd.DataFrame) -> None:
    st.subheader("Action Intelligence")
    if action_df.empty:
        st.info("No action items available for charting yet.")
        return

    c1, c2, c3 = st.columns(3)
    with c1:
        st.caption("Priority Distribution")
        priority_counts = action_df["Priority"].value_counts().reindex(ALLOWED_PRIORITIES, fill_value=0)
        st.bar_chart(priority_counts)

    with c2:
        st.caption("Owner Workload")
        owner_counts = action_df["Owner"].replace("", "Unassigned").value_counts()
        st.bar_chart(owner_counts)

    with c3:
        st.caption("Status Distribution")
        status_counts = action_df["Status"].value_counts().reindex(ALLOWED_STATUSES, fill_value=0)
        st.bar_chart(status_counts)


def render_history() -> None:
    with st.sidebar.expander("Recent Analyses", expanded=False):
        history = st.session_state.meeting_history
        if not history:
            st.caption("No analyses yet in this session.")
            return

        for item in reversed(history[-5:]):
            st.markdown(
                f"**{item['title']}**  \n"
                f"{item['timestamp']} | Tasks: {item['total_tasks']} | Score: {item['productivity_score']}"
            )


def load_sample_transcript() -> str:
    file_path = Path(__file__).with_name("sample_transcript.txt")
    if not file_path.exists():
        st.warning("sample_transcript.txt not found.")
        return ""
    return file_path.read_text(encoding="utf-8")


def main() -> None:
    st.set_page_config(
        page_title="ActionOS — AI Meeting Intelligence",
        page_icon="⚡",
        layout="wide",
    )
    initialize_state()

    st.markdown(
        """
        <style>
        .stApp {background: linear-gradient(135deg, #0f172a 0%, #020617 60%, #111827 100%); color: #e5e7eb;}
        h1, h2, h3, .stMarkdown {color: #e5e7eb;}
        div[data-testid="stMetricValue"] {color: #22d3ee;}
        div[data-testid="stSidebar"] {background: #0b1220;}
        </style>
        """,
        unsafe_allow_html=True,
    )

    st.sidebar.header("⚙️ Meeting Controls")
    meeting_type = st.sidebar.selectbox(
        "Meeting Type",
        [
            "General",
            "Engineering",
            "Product",
            "Sprint Planning",
            "Standup",
            "Client Meeting",
            "Project Review",
        ],
        index=0,
        key="meeting_type",
    )
    detail_level = st.sidebar.selectbox("AI Coaching Detail Level", ["Concise", "Balanced", "Detailed"], index=1)
    deadline_horizon = st.sidebar.slider("Default Deadline Horizon (days)", min_value=1, max_value=30, value=7)

    sidebar_title = st.sidebar.text_input(
        "Optional Meeting Title",
        value=st.session_state.selected_meeting_title,
        key="sidebar_meeting_title",
    )
    st.session_state.selected_meeting_title = sidebar_title

    if st.sidebar.button("Load Demo Transcript", use_container_width=True):
        sample_text = load_sample_transcript()
        if sample_text:
            st.session_state.current_transcript = sample_text
            st.toast("Demo transcript loaded.")

    if st.sidebar.button("Clear/Reset Session", type="secondary", use_container_width=True):
        reset_state()
        st.toast("Session reset complete.")

    st.sidebar.markdown("---")
    st.sidebar.caption("System Status")
    st.sidebar.write("AI Provider: OpenRouter")
    st.sidebar.write(f"Model: {OPENROUTER_MODEL}")
    st.sidebar.write("Session: Active")

    render_history()

    st.title("ActionOS — AI Meeting Intelligence")
    st.caption("Meeting transcript in. Structured accountability out.")

    if not st.session_state.analysis:
        st.info(
            "Paste a raw meeting transcript to generate a summary, action items, owners, deadlines,"
            " priorities, decisions, risks, and follow-up recommendations."
        )

    with st.form("meeting_form"):
        form_title = st.text_input(
            "Meeting Title",
            value=st.session_state.selected_meeting_title,
            placeholder="Example: Sprint 14 Planning",
        )
        transcript = st.text_area(
            "Raw Meeting Transcript",
            value=st.session_state.current_transcript,
            height=240,
            placeholder=(
                "Rajnish: We need the landing page finished by Friday.\n"
                "Priya: I'll complete the API integration by Thursday.\n"
                "Aman: The database migration is blocked by missing credentials..."
            ),
        )
        submitted = st.form_submit_button("Analyze Meeting", type="primary", use_container_width=True)

    if submitted:
        clean_transcript = transcript.strip()
        st.session_state.current_transcript = clean_transcript
        st.session_state.selected_meeting_title = form_title.strip() or "Untitled Meeting"

        if not clean_transcript:
            st.warning("Please paste a transcript before analyzing.")
        elif len(clean_transcript) < MIN_TRANSCRIPT_LENGTH:
            st.warning("Transcript is too short. Please provide more meeting context.")
        elif not get_api_key():
            st.error("OPENROUTER_API_KEY not found. Add it in .env or Streamlit Secrets.")
        else:
            with st.spinner("🧠 ActionOS is analyzing your meeting..."):
                ok = analyze_transcript(
                    transcript=clean_transcript,
                    meeting_type=meeting_type,
                    detail_level=detail_level,
                    deadline_horizon=deadline_horizon,
                )
            if ok:
                st.session_state.meeting_history.append(
                    {
                        "title": st.session_state.selected_meeting_title,
                        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M"),
                        "total_tasks": int(len(st.session_state.action_items)),
                        "productivity_score": int(st.session_state.analysis.get("productivity_score", 0)),
                    }
                )

    if st.session_state.analysis:
        analysis = st.session_state.analysis
        action_df = st.session_state.action_items.copy()

        render_kpis(action_df, int(analysis.get("productivity_score", 0)))
        render_summary(analysis)

        with st.expander("📌 Key Decisions", expanded=True):
            decisions = analysis.get("decisions", [])
            if decisions:
                for decision in decisions:
                    st.markdown(f"- {decision}")
            else:
                st.caption("No explicit decisions were detected.")

        with st.expander("⚠️ Risks & Blockers", expanded=True):
            risks = analysis.get("risks", [])
            if risks:
                for risk in risks:
                    st.markdown(f"- {risk}")
            else:
                st.caption("No explicit risks were detected.")

        with st.expander("🧠 AI Analysis Details", expanded=False):
            st.json(analysis)
            if st.session_state.raw_model_response:
                st.caption("Raw response (debug)")
                st.code(st.session_state.raw_model_response, language="json")

        edited_df = render_action_editor(action_df)

        filter_col1, filter_col2, filter_col3 = st.columns(3)
        with filter_col1:
            owner_options = sorted(set(edited_df["Owner"].replace("", "Unassigned").tolist()))
            selected_owners = st.multiselect("Filter by Owner", owner_options, default=owner_options)
        with filter_col2:
            selected_priorities = st.multiselect(
                "Filter by Priority", ALLOWED_PRIORITIES, default=ALLOWED_PRIORITIES
            )
        with filter_col3:
            selected_statuses = st.multiselect("Filter by Status", ALLOWED_STATUSES, default=ALLOWED_STATUSES)

        filtered_df = edited_df.copy()
        filtered_df["Owner"] = filtered_df["Owner"].replace("", "Unassigned")
        filtered_df = filtered_df[
            filtered_df["Owner"].isin(selected_owners)
            & filtered_df["Priority"].isin(selected_priorities)
            & filtered_df["Status"].isin(selected_statuses)
        ]

        st.subheader("Filtered Action Items")
        if filtered_df.empty:
            st.info("No tasks match the current filter settings.")
        else:
            st.dataframe(filtered_df, use_container_width=True)

        render_charts(filtered_df)

        total_tasks = len(edited_df)
        completed = int((edited_df["Status"] == "Completed").sum()) if total_tasks else 0
        blocked = int((edited_df["Status"] == "Blocked").sum()) if total_tasks else 0
        unassigned = int((edited_df["Owner"].replace("", "Unassigned") == "Unassigned").sum()) if total_tasks else 0
        completion_pct = int((completed / total_tasks) * 100) if total_tasks else 0

        st.subheader("Accountability Metrics")
        m1, m2, m3 = st.columns(3)
        m1.metric("Completion %", f"{completion_pct}%")
        m2.metric("Blocked Tasks", blocked)
        m3.metric("Unassigned Tasks", unassigned, delta=-unassigned, delta_color="inverse")
        st.progress(completion_pct / 100 if total_tasks else 0.0)

        csv_data = edited_df.to_csv(index=False).encode("utf-8")
        st.download_button(
            "Download Action Items CSV",
            data=csv_data,
            file_name="actionos_action_items.csv",
            mime="text/csv",
            use_container_width=True,
        )


if __name__ == "__main__":
    main()