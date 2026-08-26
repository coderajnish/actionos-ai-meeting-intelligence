# ActionOS Technical Design

## Problem Statement
Meeting transcripts are noisy and unstructured, which makes execution tracking difficult. Teams need fast conversion from conversation to accountable action plans.

## Functional Requirements
- Accept raw transcript input through Streamlit form.
- Generate summary, decisions, risks, follow-up, and structured action items.
- Display editable action items in `st.data_editor`.
- Persist analysis in `st.session_state` across reruns.
- Support local filtering, KPI metrics, charting, and CSV export.
- Handle API and parsing errors without exposing tracebacks.

## Architecture
- UI Layer: Streamlit components (`st.form`, sidebar controls, metrics, expanders, charts).
- Orchestration Layer: prompt builder, API caller, parser, normalizer.
- Data Layer: in-memory session state + Pandas DataFrame.
- AI Layer: OpenRouter Chat Completions API (`openai/gpt-oss-20b` by default).

## Data Flow
1. User submits transcript via form.
2. App builds context-aware system prompt.
3. Request sent to OpenRouter API.
4. Model returns JSON-like text.
5. App cleans fences, parses JSON, validates fields.
6. Action items normalized and loaded to DataFrame.
7. Data saved in session state for editing, filtering, charting, export.

## Session-State Strategy
Initialized keys:
- `analysis`
- `action_items`
- `meeting_history`
- `current_transcript`
- `selected_meeting_title`

This prevents expensive model calls during normal widget reruns and keeps edits stable.

## API Integration Strategy
- Endpoint: `https://openrouter.ai/api/v1/chat/completions`
- Auth: `Authorization: Bearer ${OPENROUTER_API_KEY}`
- Library: `requests`
- Timeout: 45 seconds
- Error branches: missing key, timeout, connection errors, HTTP errors, malformed JSON, empty choices/content

## Prompt Engineering Strategy
- Role fixed to `ActionOS Meeting Intelligence Engine`.
- Dynamic context: meeting type, detail level, current date, deadline horizon.
- Explicit anti-hallucination rules and fallback labels (`Unassigned`, `Not specified`).
- Strict JSON schema instructions with no markdown fences.

## JSON Schema (Conceptual)
```json
{
  "meeting_summary": "string",
  "sentiment": "Positive | Neutral | Tense | Mixed",
  "productivity_score": 0,
  "decisions": ["decision"],
  "risks": ["risk"],
  "follow_up": "string",
  "action_items": [
    {
      "task": "string",
      "owner": "string",
      "deadline": "string",
      "priority": "Low | Medium | High | Critical",
      "status": "Not Started"
    }
  ]
}
```

## Failure Handling
- Guardrails for empty/short transcript input.
- Fence-cleaning helper before `json.loads`.
- Safe defaults for missing fields.
- Debug expander for optional raw model response visibility.
- Streamlit-friendly error messages via `st.error`, `st.warning`, `st.toast`.

## Security
- API key loaded from environment/secrets only.
- `.env` and `.streamlit/secrets.toml` ignored via `.gitignore`.
- No key logging or hardcoding.

## Deployment Architecture
- Deploy directly to Streamlit Community Cloud.
- Secrets configured in Streamlit UI.
- Single-app Python entrypoint (`app.py`) with project-relative file access.

## Known Limitations
- No persistent DB in current scope.
- No user auth or multi-tenant boundaries.
- Priority/deadline inference may still require human review.

## Future Improvements
- Persistent storage and audit trails.
- API retry policy with exponential backoff.
- Multi-language transcript support.
- Integrations with task systems (Jira, Asana, ClickUp).