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
MAX_OUTPUT_TOKENS = 4000

ALLOWED_PRIORITIES = ["Low", "Medium", "High", "Critical"]
ALLOWED_STATUSES = ["Not Started", "In Progress", "Blocked", "Completed"]
ALLOWED_SENTIMENTS = ["Positive", "Neutral", "Tense", "Mixed"]


def empty_action_dataframe() -> pd.DataFrame:
    return pd.DataFrame(
        columns=["Task", "Owner", "Deadline", "Priority", "Status"]
    )


def initialize_state() -> None:
    defaults = {
        "analysis": None,
        "action_items": empty_action_dataframe(),
        "meeting_history": [],
        "current_transcript": "",
        "selected_meeting_title": "",
        "raw_model_response": "",
    }

    for key, value in defaults.items():
        if key not in st.session_state:
            st.session_state[key] = value


def reset_state() -> None:
    keys_to_clear = [
        "analysis",
        "action_items",
        "meeting_history",
        "current_transcript",
        "selected_meeting_title",
        "raw_model_response",
        "sidebar_meeting_title",
        "action_editor",
    ]

    for key in keys_to_clear:
        if key in st.session_state:
            del st.session_state[key]

    initialize_state()


def get_api_key() -> str | None:
    env_key = os.getenv("OPENROUTER_API_KEY")

    if env_key:
        return env_key

    try:
        return st.secrets.get("OPENROUTER_API_KEY")
    except Exception:
        return None


def build_system_prompt(
    meeting_type: str,
    detail_level: str,
    deadline_horizon: int,
) -> str:

    current_date = datetime.now().strftime("%Y-%m-%d")

    return f"""
You are ActionOS Meeting Intelligence Engine.

CONTEXT
Meeting type: {meeting_type}
Detail level: {detail_level}
Current date: {current_date}
Default deadline horizon: {deadline_horizon} days

YOUR JOB
Convert a messy meeting transcript into precise structured meeting intelligence.

STRICT ACCURACY RULES
1. Never invent facts.
2. Never invent task owners.
3. Mentioning a task does NOT mean that speaker owns the task.
4. Assign an owner ONLY when the transcript explicitly says that person:
   - will do the task,
   - accepts the task,
   - owns the task,
   - or is directly assigned the task.
5. Otherwise set owner to "Unassigned".
6. Preserve participant names exactly as written.
7. If no deadline is explicitly stated, use "Not specified".
8. Do not create deadlines from the default horizon unless the transcript explicitly requests estimation.
9. Separate decisions from action items.
10. Identify blockers and launch risks.
11. Infer priority only when justified by urgency, blockers, deadlines, or explicit statements.
12. Every action item must start with status "Not Started".

OUTPUT LENGTH RULES
- meeting_summary: maximum 80 words.
- follow_up: maximum 50 words.
- each decision: maximum 25 words.
- each risk: maximum 25 words.
- each task: maximum 20 words.
- return at most 10 action items.
- avoid repeating the same information.

JSON RULES
- Return ONE valid JSON object.
- Return JSON only.
- No markdown.
- No code fences.
- No introduction.
- No explanation before or after JSON.
- Use normal JSON double quotes.
- Ensure every array and object is completely closed.

RETURN EXACTLY THIS STRUCTURE

{{
  "meeting_summary": "string",
  "sentiment": "Positive | Neutral | Tense | Mixed",
  "productivity_score": 0,
  "decisions": [
    "decision"
  ],
  "risks": [
    "risk"
  ],
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

productivity_score must be an integer from 0 to 100.
""".strip()


def call_openrouter(
    messages: list[dict[str, str]],
    model: str = OPENROUTER_MODEL,
) -> str:

    api_key = get_api_key()

    if not api_key:
        raise ValueError(
            "Missing OPENROUTER_API_KEY. Add it to .env or Streamlit Secrets."
        )

    payload = {
        "model": model,
        "messages": messages,
        "temperature": 0.1,
        "max_tokens": MAX_OUTPUT_TOKENS,
    }

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    try:
        response = requests.post(
            OPENROUTER_URL,
            headers=headers,
            json=payload,
            timeout=60,
        )

    except requests.exceptions.Timeout as exc:
        raise TimeoutError(
            "The AI request timed out. Please try again."
        ) from exc

    except requests.exceptions.ConnectionError as exc:
        raise ConnectionError(
            "Could not connect to OpenRouter. Check your internet connection."
        ) from exc

    except requests.exceptions.RequestException as exc:
        raise RuntimeError(
            "A network error occurred while contacting OpenRouter."
        ) from exc

    try:
        data = response.json()
    except ValueError as exc:
        raise ValueError(
            "OpenRouter returned an invalid server response."
        ) from exc

    if response.status_code >= 400:
        error_data = data.get("error", {}) if isinstance(data, dict) else {}

        message = error_data.get(
            "message",
            f"OpenRouter returned HTTP {response.status_code}.",
        )

        raise RuntimeError(message)

    if isinstance(data, dict) and data.get("error"):
        message = data["error"].get(
            "message",
            "OpenRouter returned an API error.",
        )

        raise RuntimeError(message)

    choices = data.get("choices", []) if isinstance(data, dict) else []

    if not choices:
        raise ValueError(
            "OpenRouter returned no model response."
        )

    choice = choices[0]

    finish_reason = choice.get("finish_reason")

    content = choice.get("message", {}).get("content", "")

    if not content or not isinstance(content, str):
        raise ValueError(
            "The AI returned an empty response."
        )

    if finish_reason == "length":
        raise ValueError(
            "The AI response was truncated because it reached the output limit. "
            "Please retry or use a slightly shorter transcript."
        )

    return content.strip()


def clean_json_response(raw_text: str) -> str:

    cleaned = raw_text.strip()

    if cleaned.startswith("```json"):
        cleaned = cleaned[7:]
    elif cleaned.startswith("```"):
        cleaned = cleaned[3:]

    if cleaned.endswith("```"):
        cleaned = cleaned[:-3]

    cleaned = cleaned.strip()

    start = cleaned.find("{")
    end = cleaned.rfind("}")

    if start != -1 and end != -1 and end > start:
        return cleaned[start : end + 1]

    return cleaned


def normalize_action_items(
    items: Any,
) -> list[dict[str, str]]:

    if not isinstance(items, list):
        return []

    normalized = []

    for raw_item in items:

        if not isinstance(raw_item, dict):
            continue

        task = str(
            raw_item.get("task", "")
        ).strip() or "Untitled task"

        owner = str(
            raw_item.get("owner", "")
        ).strip() or "Unassigned"

        deadline = str(
            raw_item.get("deadline", "")
        ).strip() or "Not specified"

        priority = str(
            raw_item.get("priority", "Medium")
        ).strip().title()

        if priority not in ALLOWED_PRIORITIES:
            priority = "Medium"

        raw_status = str(
            raw_item.get("status", "Not Started")
        ).strip().lower()

        status_lookup = {
            "not started": "Not Started",
            "in progress": "In Progress",
            "blocked": "Blocked",
            "completed": "Completed",
        }

        status = status_lookup.get(
            raw_status,
            "Not Started",
        )

        normalized.append(
            {
                "Task": task,
                "Owner": owner,
                "Deadline": deadline,
                "Priority": priority,
                "Status": status,
            }
        )

    return normalized[:10]


def parse_analysis(
    raw_response: str,
) -> dict[str, Any]:

    cleaned = clean_json_response(raw_response)

    if not cleaned.startswith("{") or not cleaned.endswith("}"):
        raise ValueError(
            "The AI response appears incomplete or truncated."
        )

    try:
        parsed = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"AI returned malformed JSON: {exc.msg}"
        ) from exc

    if not isinstance(parsed, dict):
        raise ValueError(
            "AI response is not a JSON object."
        )

    sentiment = str(
        parsed.get("sentiment", "Neutral")
    ).strip().title()

    if sentiment not in ALLOWED_SENTIMENTS:
        sentiment = "Neutral"

    try:
        score = int(
            parsed.get("productivity_score", 0)
        )
    except (TypeError, ValueError):
        score = 0

    score = max(0, min(score, 100))

    decisions = parsed.get("decisions", [])

    if not isinstance(decisions, list):
        decisions = []

    decisions = [
        str(item).strip()
        for item in decisions
        if str(item).strip()
    ]

    risks = parsed.get("risks", [])

    if not isinstance(risks, list):
        risks = []

    risks = [
        str(item).strip()
        for item in risks
        if str(item).strip()
    ]

    meeting_summary = str(
        parsed.get(
            "meeting_summary",
            "No summary provided.",
        )
    ).strip()

    follow_up = str(
        parsed.get(
            "follow_up",
            "No follow-up recommendation provided.",
        )
    ).strip()

    action_items = normalize_action_items(
        parsed.get("action_items", [])
    )

    return {
        "meeting_summary": (
            meeting_summary
            or "No summary provided."
        ),
        "sentiment": sentiment,
        "productivity_score": score,
        "decisions": decisions,
        "risks": risks,
        "follow_up": (
            follow_up
            or "No follow-up recommendation provided."
        ),
        "action_items": action_items,
    }


def analyze_transcript(
    transcript: str,
    meeting_type: str,
    detail_level: str,
    deadline_horizon: int,
) -> bool:

    system_prompt = build_system_prompt(
        meeting_type,
        detail_level,
        deadline_horizon,
    )

    user_prompt = f"""
Analyze the following meeting transcript.

TRANSCRIPT:
{transcript.strip()}

Return the requested JSON object only.
""".strip()

    messages = [
        {
            "role": "system",
            "content": system_prompt,
        },
        {
            "role": "user",
            "content": user_prompt,
        },
    ]

    try:

        raw_response = call_openrouter(messages)

        st.session_state.raw_model_response = (
            raw_response
        )

        analysis = parse_analysis(
            raw_response
        )

    except (
        ValueError,
        TimeoutError,
        ConnectionError,
        RuntimeError,
    ) as exc:

        st.error(str(exc))

        if st.session_state.raw_model_response:
            with st.expander(
                "Debug response",
                expanded=False,
            ):
                st.code(
                    st.session_state.raw_model_response,
                    language="json",
                )

        return False

    action_df = pd.DataFrame(
        analysis["action_items"]
    )

    if action_df.empty:
        action_df = empty_action_dataframe()

    st.session_state.analysis = analysis
    st.session_state.action_items = action_df

    st.toast(
        "Meeting analyzed successfully.",
        icon="✅",
    )

    return True


def render_kpis(
    action_df: pd.DataFrame,
    score: int,
) -> None:

    total_tasks = len(action_df)

    if action_df.empty:
        high_critical = 0
        unique_owners = 0
        unassigned_count = 0

    else:

        high_critical = int(
            action_df["Priority"]
            .isin(["High", "Critical"])
            .sum()
        )

        valid_owners = (
            action_df["Owner"]
            .replace("", "Unassigned")
        )

        unique_owners = int(
            valid_owners[
                valid_owners != "Unassigned"
            ].nunique()
        )

        unassigned_count = int(
            (
                valid_owners
                == "Unassigned"
            ).sum()
        )

    col1, col2, col3, col4 = st.columns(4)

    col1.metric(
        "Total Action Items",
        total_tasks,
    )

    col2.metric(
        "High/Critical Tasks",
        high_critical,
    )

    col3.metric(
        "Assigned Owners",
        unique_owners,
    )

    col4.metric(
        "Productivity Score",
        score,
        delta=score - 70,
        delta_color="normal",
    )

    st.caption(
        f"Unassigned tasks: {unassigned_count}"
    )


def render_summary(
    analysis: dict[str, Any],
) -> None:

    score = int(
        analysis.get(
            "productivity_score",
            0,
        )
    )

    sentiment = analysis.get(
        "sentiment",
        "Neutral",
    )

    if score >= 80:

        st.success(
            f"Meeting Health: Strong "
            f"({score}/100) | "
            f"Sentiment: {sentiment}"
        )

    elif score >= 60:

        st.info(
            f"Meeting Health: Stable "
            f"({score}/100) | "
            f"Sentiment: {sentiment}"
        )

    else:

        st.warning(
            f"Meeting Health: Needs Attention "
            f"({score}/100) | "
            f"Sentiment: {sentiment}"
        )

    st.subheader(
        "Meeting Summary"
    )

    st.write(
        analysis.get(
            "meeting_summary",
            "No summary available.",
        )
    )

    st.subheader(
        "Follow-Up Recommendation"
    )

    st.write(
        analysis.get(
            "follow_up",
            "No recommendation available.",
        )
    )


def render_action_editor(
    action_df: pd.DataFrame,
) -> pd.DataFrame:

    st.subheader(
        "Editable Action Items"
    )

    edited_df = st.data_editor(
        action_df,
        key="action_editor",
        use_container_width=True,
        num_rows="dynamic",
        column_config={
            "Priority": (
                st.column_config.SelectboxColumn(
                    "Priority",
                    options=ALLOWED_PRIORITIES,
                    required=True,
                )
            ),
            "Status": (
                st.column_config.SelectboxColumn(
                    "Status",
                    options=ALLOWED_STATUSES,
                    required=True,
                )
            ),
        },
    )

    required_columns = [
        "Task",
        "Owner",
        "Deadline",
        "Priority",
        "Status",
    ]

    for column in required_columns:
        if column not in edited_df.columns:
            edited_df[column] = ""

    edited_df = (
        edited_df[
            required_columns
        ]
        .fillna("")
        .copy()
    )

    st.session_state.action_items = (
        edited_df
    )

    return edited_df


def render_charts(
    action_df: pd.DataFrame,
) -> None:

    st.subheader(
        "Action Intelligence"
    )

    if action_df.empty:

        st.info(
            "No action items available "
            "for charting yet."
        )

        return

    col1, col2, col3 = st.columns(3)

    with col1:

        st.caption(
            "Priority Distribution"
        )

        priority_counts = (
            action_df["Priority"]
            .value_counts()
            .reindex(
                ALLOWED_PRIORITIES,
                fill_value=0,
            )
        )

        st.bar_chart(
            priority_counts
        )

    with col2:

        st.caption(
            "Owner Workload"
        )

        owners = (
            action_df["Owner"]
            .replace("", "Unassigned")
        )

        owner_counts = (
            owners.value_counts()
        )

        st.bar_chart(
            owner_counts
        )

    with col3:

        st.caption(
            "Status Distribution"
        )

        status_counts = (
            action_df["Status"]
            .value_counts()
            .reindex(
                ALLOWED_STATUSES,
                fill_value=0,
            )
        )

        st.bar_chart(
            status_counts
        )


def render_history() -> None:

    with st.sidebar.expander(
        "Recent Analyses",
        expanded=False,
    ):

        history = (
            st.session_state
            .meeting_history
        )

        if not history:

            st.caption(
                "No analyses yet in this session."
            )

            return

        for item in reversed(
            history[-5:]
        ):

            st.markdown(
                f"**{item['title']}**  \n"
                f"{item['timestamp']} | "
                f"Tasks: {item['total_tasks']} | "
                f"Score: "
                f"{item['productivity_score']}"
            )


def load_sample_transcript() -> str:

    file_path = (
        Path(__file__)
        .with_name(
            "sample_transcript.txt"
        )
    )

    if not file_path.exists():

        st.warning(
            "sample_transcript.txt "
            "not found."
        )

        return ""

    try:

        return file_path.read_text(
            encoding="utf-8"
        )

    except OSError:

        st.warning(
            "Could not read the "
            "sample transcript."
        )

        return ""


def main() -> None:

    st.set_page_config(
        page_title=(
            "ActionOS — "
            "AI Meeting Intelligence"
        ),
        page_icon="⚡",
        layout="wide",
    )

    initialize_state()

    st.markdown(
        """
        <style>
        .stApp {
            background:
                linear-gradient(
                    135deg,
                    #0f172a 0%,
                    #020617 60%,
                    #111827 100%
                );
            color: #e5e7eb;
        }

        div[data-testid="stMetricValue"] {
            color: #22d3ee;
        }

        div[data-testid="stSidebar"] {
            background: #0b1220;
        }
        </style>
        """,
        unsafe_allow_html=True,
    )

    st.sidebar.header(
        "⚙️ Meeting Controls"
    )

    meeting_type = (
        st.sidebar.selectbox(
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
            key="meeting_type",
        )
    )

    detail_level = (
        st.sidebar.selectbox(
            "AI Coaching Detail Level",
            [
                "Concise",
                "Balanced",
                "Detailed",
            ],
            index=1,
        )
    )

    deadline_horizon = (
        st.sidebar.slider(
            "Default Deadline Horizon (days)",
            min_value=1,
            max_value=30,
            value=7,
        )
    )

    sidebar_title = (
        st.sidebar.text_input(
            "Optional Meeting Title",
            value=(
                st.session_state
                .selected_meeting_title
            ),
            key="sidebar_meeting_title",
        )
    )

    st.session_state.selected_meeting_title = (
        sidebar_title
    )

    if st.sidebar.button(
        "Load Demo Transcript",
        use_container_width=True,
    ):

        sample_text = (
            load_sample_transcript()
        )

        if sample_text:

            st.session_state.current_transcript = (
                sample_text
            )

            st.toast(
                "Demo transcript loaded.",
                icon="📄",
            )

    if st.sidebar.button(
        "Clear/Reset Session",
        type="secondary",
        use_container_width=True,
    ):

        reset_state()

        st.toast(
            "Session reset complete.",
            icon="🧹",
        )

        st.rerun()

    st.sidebar.markdown("---")

    st.sidebar.caption(
        "System Status"
    )

    st.sidebar.write(
        "AI Provider: OpenRouter"
    )

    st.sidebar.write(
        f"Model: {OPENROUTER_MODEL}"
    )

    st.sidebar.write(
        "Session: Active"
    )

    render_history()

    st.title(
        "ActionOS — "
        "AI Meeting Intelligence"
    )

    st.caption(
        "Meeting transcript in. "
        "Structured accountability out."
    )

    if not st.session_state.analysis:

        st.info(
            "Paste a raw meeting transcript "
            "to generate a summary, action "
            "items, owners, deadlines, "
            "priorities, decisions, risks, "
            "and follow-up recommendations."
        )

    with st.form(
        "meeting_form"
    ):

        form_title = (
            st.text_input(
                "Meeting Title",
                value=(
                    st.session_state
                    .selected_meeting_title
                ),
                placeholder=(
                    "Example: Sprint 14 Planning"
                ),
            )
        )

        transcript = (
            st.text_area(
                "Raw Meeting Transcript",
                value=(
                    st.session_state
                    .current_transcript
                ),
                height=240,
                placeholder=(
                    "Rajnish: We need the "
                    "landing page finished "
                    "by Friday.\n"
                    "Priya: I'll complete "
                    "the API integration "
                    "by Thursday.\n"
                    "Aman: The database "
                    "migration is blocked "
                    "by missing credentials..."
                ),
            )
        )

        submitted = (
            st.form_submit_button(
                "Analyze Meeting",
                type="primary",
                use_container_width=True,
            )
        )

    if submitted:

        clean_transcript = (
            transcript.strip()
        )

        st.session_state.current_transcript = (
            clean_transcript
        )

        st.session_state.selected_meeting_title = (
            form_title.strip()
            or "Untitled Meeting"
        )

        if not clean_transcript:

            st.warning(
                "Please paste a transcript "
                "before analyzing."
            )

        elif (
            len(clean_transcript)
            < MIN_TRANSCRIPT_LENGTH
        ):

            st.warning(
                "Transcript is too short. "
                "Please provide more "
                "meeting context."
            )

        elif not get_api_key():

            st.error(
                "OPENROUTER_API_KEY "
                "not found. Add it in "
                ".env or Streamlit Secrets."
            )

        else:

            with st.spinner(
                "🧠 ActionOS is analyzing "
                "your meeting..."
            ):

                success = (
                    analyze_transcript(
                        transcript=(
                            clean_transcript
                        ),
                        meeting_type=(
                            meeting_type
                        ),
                        detail_level=(
                            detail_level
                        ),
                        deadline_horizon=(
                            deadline_horizon
                        ),
                    )
                )

            if success:

                st.session_state.meeting_history.append(
                    {
                        "title": (
                            st.session_state
                            .selected_meeting_title
                        ),
                        "timestamp": (
                            datetime.now()
                            .strftime(
                                "%Y-%m-%d %H:%M"
                            )
                        ),
                        "total_tasks": (
                            len(
                                st.session_state
                                .action_items
                            )
                        ),
                        "productivity_score": (
                            int(
                                st.session_state
                                .analysis
                                .get(
                                    "productivity_score",
                                    0,
                                )
                            )
                        ),
                    }
                )

    if st.session_state.analysis:

        analysis = (
            st.session_state.analysis
        )

        action_df = (
            st.session_state
            .action_items
            .copy()
        )

        render_kpis(
            action_df,
            int(
                analysis.get(
                    "productivity_score",
                    0,
                )
            ),
        )

        render_summary(
            analysis
        )

        with st.expander(
            "📌 Key Decisions",
            expanded=True,
        ):

            decisions = (
                analysis.get(
                    "decisions",
                    [],
                )
            )

            if decisions:

                for decision in decisions:
                    st.markdown(
                        f"- {decision}"
                    )

            else:

                st.caption(
                    "No explicit decisions "
                    "were detected."
                )

        with st.expander(
            "⚠️ Risks & Blockers",
            expanded=True,
        ):

            risks = (
                analysis.get(
                    "risks",
                    [],
                )
            )

            if risks:

                for risk in risks:
                    st.markdown(
                        f"- {risk}"
                    )

            else:

                st.caption(
                    "No explicit risks "
                    "were detected."
                )

        with st.expander(
            "🧠 AI Analysis Details",
            expanded=False,
        ):

            st.json(
                analysis
            )

            if (
                st.session_state
                .raw_model_response
            ):

                st.caption(
                    "Raw response (debug)"
                )

                st.code(
                    st.session_state
                    .raw_model_response,
                    language="json",
                )

        edited_df = (
            render_action_editor(
                action_df
            )
        )

        filter_col1, filter_col2, filter_col3 = (
            st.columns(3)
        )

        with filter_col1:

            owner_options = sorted(
                set(
                    edited_df["Owner"]
                    .replace(
                        "",
                        "Unassigned",
                    )
                    .tolist()
                )
            )

            selected_owners = (
                st.multiselect(
                    "Filter by Owner",
                    owner_options,
                    default=owner_options,
                )
            )

        with filter_col2:

            selected_priorities = (
                st.multiselect(
                    "Filter by Priority",
                    ALLOWED_PRIORITIES,
                    default=(
                        ALLOWED_PRIORITIES
                    ),
                )
            )

        with filter_col3:

            selected_statuses = (
                st.multiselect(
                    "Filter by Status",
                    ALLOWED_STATUSES,
                    default=(
                        ALLOWED_STATUSES
                    ),
                )
            )

        filtered_df = (
            edited_df.copy()
        )

        filtered_df["Owner"] = (
            filtered_df["Owner"]
            .replace(
                "",
                "Unassigned",
            )
        )

        filtered_df = (
            filtered_df[
                filtered_df[
                    "Owner"
                ].isin(
                    selected_owners
                )
                & filtered_df[
                    "Priority"
                ].isin(
                    selected_priorities
                )
                & filtered_df[
                    "Status"
                ].isin(
                    selected_statuses
                )
            ]
        )

        st.subheader(
            "Filtered Action Items"
        )

        if filtered_df.empty:

            st.info(
                "No tasks match the "
                "current filter settings."
            )

        else:

            st.dataframe(
                filtered_df,
                use_container_width=True,
            )

        render_charts(
            edited_df
        )

        total_tasks = len(
            edited_df
        )

        if total_tasks:

            completed = int(
                (
                    edited_df["Status"]
                    == "Completed"
                ).sum()
            )

            blocked = int(
                (
                    edited_df["Status"]
                    == "Blocked"
                ).sum()
            )

            unassigned = int(
                (
                    edited_df["Owner"]
                    .replace(
                        "",
                        "Unassigned",
                    )
                    == "Unassigned"
                ).sum()
            )

            completion_pct = int(
                (
                    completed
                    / total_tasks
                )
                * 100
            )

        else:

            completed = 0
            blocked = 0
            unassigned = 0
            completion_pct = 0

        st.subheader(
            "Accountability Metrics"
        )

        metric1, metric2, metric3 = (
            st.columns(3)
        )

        metric1.metric(
            "Completion %",
            f"{completion_pct}%",
            delta=(
                f"{completed}/{total_tasks} tasks"
            ),
        )

        metric2.metric(
            "Blocked Tasks",
            blocked,
            delta=(
                -blocked
                if blocked
                else 0
            ),
            delta_color="inverse",
        )

        metric3.metric(
            "Unassigned Tasks",
            unassigned,
            delta=(
                -unassigned
                if unassigned
                else 0
            ),
            delta_color="inverse",
        )

        st.progress(
            completion_pct / 100
            if total_tasks
            else 0.0
        )

        csv_data = (
            edited_df
            .to_csv(
                index=False
            )
            .encode(
                "utf-8"
            )
        )

        st.download_button(
            "Download Action Items CSV",
            data=csv_data,
            file_name=(
                "actionos_action_items.csv"
            ),
            mime="text/csv",
            use_container_width=True,
        )


if __name__ == "__main__":
    main()