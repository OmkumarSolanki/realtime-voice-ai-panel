"""The 'brain': OpenAI calls that assemble the panel, ask clarifying questions, propose
solutions, and summarize.

The meeting runs in phases:
1. create_panel  -> 3-4 agents tailored to the problem, each a distinct personality.
2. next_clarifying -> ONE agent asks ONE clarifying question (turn-taking, not all at once).
3. run_solutions -> each agent gives its recommendation, building on the shared discussion.
4. summarize     -> the written recap shown on screen.

All calls return parsed JSON so the orchestrator can drive voices, tiles, and the UI.
Agents share one conversation history, so they "hear" each other.
"""

import json
import re

from loguru import logger
from openai import AsyncOpenAI

from .config import config

_client: AsyncOpenAI | None = None


def _get_client() -> AsyncOpenAI:
    """Create the OpenAI client on first use (avoids import-time crashes)."""
    global _client
    if _client is None:
        _client = AsyncOpenAI(api_key=config.OPENAI_API_KEY)
    return _client


PANEL_SYSTEM = """You assemble a small panel of AI experts who join a live voice meeting to
help a user think through a problem out loud — like people on a Google Meet call.

Pick **3 or 4** experts chosen specifically to fit THIS problem (e.g. a chef, a finance
person, a growth marketer, a lawyer, a structural engineer). Choose the number based on the
problem's complexity: simpler problems get 3, richer problems get 4.

Each expert must have a CLEARLY DIFFERENT personality and speaking style, so they feel like
distinct people. Mix temperaments — e.g. a blunt skeptic, a warm encourager, a numbers-driven
analyst, a big-picture visionary. They should be able to disagree with each other.

Exactly ONE expert is the "facilitator": the one who warmly runs the meeting, greets the
user, and tends to ask the clarifying questions. The facilitator is still a real expert with
a personality — not a neutral robot.

For each expert produce:
- a short, human first name,
- a 3-5 word role/specialty label,
- a one-sentence personality / speaking style (make each distinct),
- a spoken first-person introduction ("intro") of 1-2 short sentences that starts with
  "I'm <name>" and says who they are and what they'll bring to this problem.

Return STRICT JSON:
{
  "topic": "<5-8 word title of the problem>",
  "experts": [
    {"id":"<lowercase-slug-of-name>","name":"...","role":"...","personality":"...",
     "intro":"I'm ...","facilitator": true},
    {"id":"...","name":"...","role":"...","personality":"...","intro":"I'm ...","facilitator": false}
  ]
}
Use 3 or 4 experts. Exactly one has "facilitator": true. Plain speech only — no markdown.
"""

CLARIFY_SYSTEM = """You are the DIRECTOR of a live spoken expert-panel meeting — one connected
group conversation focused entirely on the USER. You decide who speaks next and what they say.

READ THE WHOLE CONVERSATION FIRST. Every agent has heard everything: the problem, what each
agent already asked, and how the user answered. This is ONE coherent discussion, NOT separate
interviewers firing unrelated questions.

Think like a smart facilitator. Decide the single most useful NEXT step:

1. CONTINUITY (important): The agent who just asked should usually ask the FOLLOW-UP too, so
   one expert digs into a topic coherently instead of the conversation jumping around. Stay
   with "LAST AGENT" and build directly on the user's last answer — UNLESS the most important
   open question clearly belongs to a different expert's domain (e.g. money question -> the
   finance agent). Only then hand off, and have the new agent briefly acknowledge the thread
   first. Never bounce between agents on unrelated topics.
2. ONE focused question at a time. Pick the single most important thing still unknown that
   would actually change the advice. Never repeat a question or re-ask what was answered.
3. STOP EARLY. The goal is NOT to ask many questions. As soon as you understand enough to give
   genuinely useful advice, set "enough_info": true and stop. Two or three good questions is
   usually plenty. Don't pad. If the user sounds unsure or vague, also stop and move on.

The spoken line MUST: briefly acknowledge the user's last answer in a few words, then ask ONE
short question, in the chosen agent's personality. Plain speech, 1-2 short sentences total.

You are given the experts, who asked last, and the full conversation. Return STRICT JSON:
{"agent_id":"<one of the expert ids>","text":"<short acknowledgement + one question>","enough_info":false}
"""

SOLUTIONS_SYSTEM = """You run a live spoken expert-panel meeting — a connected group discussion
focused on the USER. The clarifying phase is done; now the agents give recommendations.

READ THE WHOLE CONVERSATION FIRST. The agents have heard the user's problem and EVERY answer
the user gave. Their advice must be SPECIFIC to what the user actually said (budget,
constraints, goals) — not generic.

Only the agents who actually have something RELEVANT and useful to say should speak. Do NOT
force every agent to talk. Often just 1 or 2 agents are relevant to a given point — let the
others stay quiet rather than padding with generic filler. Pick the agents whose expertise
genuinely fits what the user said.

Each speaking agent:
- speaks 2-3 SHORT spoken sentences, plain speech, no lists,
- gives a concrete recommendation grounded in the user's specific answers,
- builds on the discussion: explicitly reference the user's earlier answer and/or another
  agent by name, and genuinely agree or push back, so it feels like people in one room
  reacting to each other while helping the user.

End by handing the conversation back to the user with one short spoken question (asked by the
facilitator) so the meeting stays centered on them.

You are given the experts and the full conversation. Return STRICT JSON:
{
  "utterances": [
    {"agent_id":"<expert id>","text":"<2-3 spoken sentences>"}
  ],
  "closing_question":"<one short spoken question handing back to the user>"
}
Include 1-3 utterances — only from the agents who are genuinely relevant, in a natural order.
"closing_question" is required.
"""

SUMMARY_SYSTEM = """Summarize a spoken expert-panel meeting for the user to read afterward.
Return STRICT JSON:
{
  "topic":"<short title>",
  "key_points":["...", "..."],
  "disagreements":["<where experts disagreed>", "..."],
  "next_steps":["<concrete next action>", "..."],
  "spoken_closing":"<one or two short sentences to say out loud to wrap up>"
}
Keep each list to 2-4 crisp items. Base it only on what was actually discussed.
"""


async def _json_call(system: str, user: str, *, max_tokens: int = 700) -> dict:
    """Call the LLM and parse a JSON object response."""
    resp = await _get_client().chat.completions.create(
        model=config.LLM_MODEL,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        response_format={"type": "json_object"},
        temperature=0.8,
        max_tokens=max_tokens,
    )
    content = resp.choices[0].message.content or "{}"
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        logger.error(f"LLM returned non-JSON: {content[:300]}")
        return {}


def _slug(text: str, fallback: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return s or fallback


def _normalize_experts(experts: list[dict]) -> list[dict]:
    """Ensure 3-4 experts with unique ids and exactly one facilitator."""
    experts = [e for e in (experts or []) if isinstance(e, dict)][:4]
    seen: set[str] = set()
    for i, e in enumerate(experts):
        base = _slug(e.get("id") or e.get("name", ""), f"agent{i+1}")
        sid = base
        n = 2
        while sid in seen:
            sid = f"{base}-{n}"
            n += 1
        seen.add(sid)
        e["id"] = sid
        e["facilitator"] = bool(e.get("facilitator"))
    # Exactly one facilitator: keep the first flagged, else promote the first agent.
    facs = [e for e in experts if e["facilitator"]]
    if not facs and experts:
        experts[0]["facilitator"] = True
    elif len(facs) > 1:
        for e in facs[1:]:
            e["facilitator"] = False
    return experts


async def create_panel(problem: str) -> dict:
    """Create a 3-4 expert panel tailored to the given problem."""
    data = await _json_call(
        PANEL_SYSTEM, f'The user\'s problem, spoken out loud:\n"{problem}"'
    )
    data["experts"] = _normalize_experts(data.get("experts") or [])
    return data


def _history_text(history: list[dict]) -> str:
    lines = []
    for turn in history:
        who = turn.get("name") or turn.get("role", "?")
        lines.append(f"{who}: {turn['text']}")
    return "\n".join(lines)


def _experts_desc(experts: list[dict]) -> str:
    return "\n".join(
        f"- {e['id']} ({e.get('name', e['id'])}, {e.get('role', '')}): "
        f"{e.get('personality', '')}"
        + (" [facilitator]" if e.get("facilitator") else "")
        for e in experts
    )


async def next_clarifying(
    problem: str, experts: list[dict], history: list[dict], last_asker: str | None = None
) -> dict:
    """Pick one agent to ask the next clarifying question (favouring continuity)."""
    last_name = next((e.get("name", last_asker) for e in experts if e.get("id") == last_asker), last_asker)
    user = (
        f"PROBLEM: {problem}\n\n"
        f"EXPERTS:\n{_experts_desc(experts)}\n\n"
        f"LAST AGENT WHO ASKED: {last_asker or 'none'} ({last_name or '-'})\n\n"
        f"CONVERSATION SO FAR:\n{_history_text(history)}\n\n"
        "Decide the next step now (continue with the last agent unless a handoff is clearly better)."
    )
    data = await _json_call(CLARIFY_SYSTEM, user, max_tokens=300)
    # Guard against a bad/missing agent id.
    ids = {e["id"] for e in experts}
    if data.get("agent_id") not in ids and experts:
        # Prefer continuity, then facilitator.
        data["agent_id"] = last_asker if last_asker in ids else (
            next((e["id"] for e in experts if e.get("facilitator")), experts[0]["id"])
        )
    return data


async def run_solutions(problem: str, experts: list[dict], history: list[dict]) -> dict:
    """Each agent gives its recommendation, then hands back to the user."""
    user = (
        f"PROBLEM: {problem}\n\n"
        f"EXPERTS:\n{_experts_desc(experts)}\n\n"
        f"CONVERSATION SO FAR:\n{_history_text(history)}\n\n"
        "Give the panel's recommendations now — one utterance per expert."
    )
    return await _json_call(SOLUTIONS_SYSTEM, user, max_tokens=900)


async def summarize(problem: str, experts: list[dict], history: list[dict]) -> dict:
    """Summarize the whole meeting."""
    user = (
        f"PROBLEM: {problem}\n\n"
        f"FULL CONVERSATION:\n{_history_text(history)}\n\n"
        "Summarize now."
    )
    return await _json_call(SUMMARY_SYSTEM, user, max_tokens=600)
