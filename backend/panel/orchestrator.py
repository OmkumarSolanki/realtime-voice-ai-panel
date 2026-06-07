"""PanelProcessor: the custom Pipecat frame processor that runs the expert meeting.

Flow (a Google-Meet-style voice meeting):
- The user states a problem. STT emits a final TranscriptionFrame.
- First substantive utterance => create 3-4 agents tailored to the problem, announce them to
  the UI as participant tiles, and each agent introduces itself in its own voice.
- Clarifying phase: ONE agent asks ONE clarifying question at a time, then hands back to the
  user. Agents share one history, so they "hear" each other.
- Once the panel understands enough (or after a few questions), the solving phase runs: each
  agent gives its recommendation, then hands back to the user.
- The user can interrupt at any time (VAD -> InterruptionFrame); the in-flight speaking task
  is cancelled so the agents go quiet immediately.
- Saying "I'm done" (or the End button) produces a written recap.

Strict one-at-a-time turn-taking: each utterance switches the Cartesia voice
(TTSUpdateSettingsFrame), speaks (TTSSpeakFrame), then WAITS for the real end of playback
(BotStoppedSpeakingFrame) before the next utterance — so voices never overlap.
"""

import asyncio

from loguru import logger

from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
    Frame,
    InterimTranscriptionFrame,
    InterruptionFrame,
    StartFrame,
    TranscriptionFrame,
    TTSSpeakFrame,
    TTSUpdateSettingsFrame,
    UserStartedSpeakingFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.processors.frameworks.rtvi.frames import (
    RTVIClientMessageFrame,
    RTVIServerMessageFrame,
)

from . import brain
from .voices import assign_voices

END_PHRASES = (
    "i'm done",
    "im done",
    "that's all",
    "thats all",
    "wrap up",
    "wrap it up",
    "let's wrap",
    "lets wrap",
    "summarize",
    "summary",
    "finish up",
    "that's enough",
    "thats enough",
)

# Stop asking clarifying questions after this many, even if the brain wants more.
MAX_CLARIFY = 3

# After the user's last word, wait this long before the panel responds. Lets the user pause
# mid-sentence and finish their thought; also merges chunked STT finals into one turn.
USER_DEBOUNCE_SECS = 3.0


def _estimate_secs(text: str) -> float:
    """Rough spoken duration — used only as a safety timeout for the playback gate."""
    words = max(1, len(text.split()))
    return max(1.2, words / 3.0 + 0.4)


class PanelProcessor(FrameProcessor):
    def __init__(self, voice_pool: list[dict], **kwargs):
        super().__init__(**kwargs)
        self._voice_pool = voice_pool or []
        self._voice_by_id: dict[str, dict] = {}
        self._experts: list[dict] = []
        self._facilitator_id: str = ""
        self._topic: str = ""
        self._problem: str = ""
        self._history: list[dict] = []
        # await_problem -> assembling -> intros -> clarifying -> solving -> summarizing -> ended
        self._phase = "await_problem"
        self._clarify_count = 0
        self._enough_info = False
        self._last_asker: str | None = None
        self._active: asyncio.Task | None = None
        self._summarized = False
        self._bg: set[asyncio.Task] = set()
        self._utterance_done = asyncio.Event()
        # Debounce buffer for spoken user input.
        self._user_buffer: list[str] = []
        self._user_flush_handle: asyncio.TimerHandle | None = None

    # ---- frame entry point -------------------------------------------------

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)

        if isinstance(frame, StartFrame):
            await self.push_frame(frame, direction)
            return

        # Real end of bot audio playback -> release the turn gate.
        if isinstance(frame, BotStoppedSpeakingFrame):
            self._utterance_done.set()
            await self.push_frame(frame, direction)
            return

        if isinstance(frame, BotStartedSpeakingFrame):
            await self.push_frame(frame, direction)
            return

        # User started talking -> stop the agents immediately (barge-in).
        if isinstance(frame, (InterruptionFrame, UserStartedSpeakingFrame)):
            self._cancel_active()
            # Unblock anything waiting on the playback gate.
            self._utterance_done.set()
            await self.push_frame(frame, direction)
            return

        # Live (partial) transcript of what the user is saying right now.
        if isinstance(frame, InterimTranscriptionFrame):
            text = (frame.text or "").strip()
            if text:
                await self._send_ui({"type": "user_interim", "text": text})
            await self.push_frame(frame, direction)
            return

        # Final user transcript -> buffer it; respond only after the user pauses
        # (USER_DEBOUNCE_SECS), so the panel never cuts the user off mid-sentence.
        if isinstance(frame, TranscriptionFrame):
            text = (frame.text or "").strip()
            if text:
                self._buffer_user_text(text)
            await self.push_frame(frame, direction)
            return

        # Client messages (End button / typed input) handled via the RTVI
        # on_client_message event (see bot.py). Just pass through.
        if isinstance(frame, RTVIClientMessageFrame):
            await self.push_frame(frame, direction)
            return

        await self.push_frame(frame, direction)

    # ---- public hooks called from bot.py event handlers --------------------

    async def handle_client_message(self, msg_type: str, data: dict | None):
        """Handle a message sent from the client (End button / typed input)."""
        data = data or {}
        if msg_type == "end_session":
            self._cancel_active()
            self._schedule(self._summarize())
        elif msg_type == "user_text":
            text = (data.get("text") or "").strip()
            if text:
                self._on_user_text(text)

    async def greet(self):
        """Once the client connects, prompt the user on-screen to state their problem.

        No voice speaks yet — there are no participant tiles, so an unseen voice would be
        confusing. The first agent voices come after the panel is assembled.
        """
        await self._send_ui({"type": "status", "phase": "await_problem"})
        await self._send_ui({"type": "your_turn"})

    # ---- user input handling ----------------------------------------------

    def _buffer_user_text(self, text: str):
        """Accumulate spoken finals; (re)start the debounce timer to flush as one turn."""
        self._user_buffer.append(text)
        if self._user_flush_handle:
            self._user_flush_handle.cancel()
        loop = asyncio.get_event_loop()
        self._user_flush_handle = loop.call_later(USER_DEBOUNCE_SECS, self._flush_user)

    def _flush_user(self):
        self._user_flush_handle = None
        combined = " ".join(self._user_buffer).strip()
        self._user_buffer = []
        if combined:
            self._on_user_text(combined)

    def _on_user_text(self, text: str):
        logger.info(f"User said: {text!r} (phase={self._phase})")
        lowered = text.lower()
        await_problem = self._phase == "await_problem"

        if any(p in lowered for p in END_PHRASES) and not await_problem:
            self._cancel_active()
            self._schedule(self._summarize())
            return

        # Echo the user's words to the UI transcript + shared history.
        self._history.append({"role": "user", "name": "You", "text": text})
        self._emit({"type": "user", "text": text})

        if await_problem:
            self._problem = text
            self._phase = "assembling"  # set now to block re-entry from a second message
            self._cancel_active()
            self._schedule(self._run_first())
            return

        # The panel doesn't exist yet (still assembling) — ignore stray input so a
        # duplicate "problem" message can't be mistaken for a follow-up.
        if not self._experts:
            return

        if self._phase == "clarifying":
            self._cancel_active()
            if self._enough_info or self._clarify_count >= MAX_CLARIFY:
                self._schedule(self._run_solutions())
            else:
                self._schedule(self._ask_clarifying())
        else:  # solving / ended -> treat as a follow-up
            self._cancel_active()
            self._schedule(self._run_solutions())

    # ---- the meeting phases -----------------------------------------------

    async def _run_first(self):
        """Create the panel, announce the tiles, do intros, then ask the first question."""
        try:
            self._phase = "assembling"
            await self._send_ui({"type": "status", "phase": "assembling"})
            logger.info(f"Creating panel for problem: {self._problem!r}")
            data = await brain.create_panel(self._problem)
            experts = data.get("experts") or []
            if len(experts) < 3:
                logger.error(f"Panel creation returned {len(experts)} experts: {data}")
                await self._send_ui({"type": "error", "message": "I couldn't form the panel — say the problem once more?"})
                await self._send_ui({"type": "status", "phase": "await_problem"})
                await self._send_ui({"type": "your_turn"})
                self._phase = "await_problem"
                return

            self._experts = experts
            self._topic = data.get("topic", "")
            self._voice_by_id = assign_voices([e["id"] for e in experts], self._voice_pool)
            self._facilitator_id = next(
                (e["id"] for e in experts if e.get("facilitator")), experts[0]["id"]
            )
            logger.info(
                f"Panel created — topic={self._topic!r} "
                f"agents={[(e['id'], e.get('facilitator')) for e in experts]}"
            )

            # Tell the UI who's on the call (with their assigned voices).
            await self._send_ui({
                "type": "panel",
                "topic": self._topic,
                "experts": [
                    {
                        "id": e["id"],
                        "name": e.get("name", e["id"]),
                        "role": e.get("role", ""),
                        "personality": e.get("personality", ""),
                        "facilitator": bool(e.get("facilitator")),
                        "voice": self._voice_by_id.get(e["id"], {}).get("label", ""),
                    }
                    for e in experts
                ],
            })

            # One-line self-introductions, each in the agent's own voice.
            self._phase = "intros"
            await self._send_ui({"type": "status", "phase": "intros"})
            for e in experts:
                intro = (e.get("intro") or "").strip() or f"I'm {e.get('name', '')}, {e.get('role', '')}."
                await self._speak(e["id"], intro)

            # First clarifying question.
            self._phase = "clarifying"
            await self._send_ui({"type": "status", "phase": "clarifying"})
            await self._ask_clarifying()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.exception(f"_run_first failed: {e}")

    async def _ask_clarifying(self):
        """One agent asks one clarifying question, then hand back to the user."""
        try:
            self._phase = "clarifying"
            await self._send_ui({"type": "status", "phase": "clarifying"})
            reply = await brain.next_clarifying(
                self._problem, self._experts, self._history, self._last_asker
            )
            self._enough_info = bool(reply.get("enough_info"))
            agent_id = reply.get("agent_id") or self._facilitator_id
            question = (reply.get("text") or "").strip()

            if self._enough_info or self._clarify_count >= MAX_CLARIFY or not question:
                await self._run_solutions()
                return

            await self._speak(agent_id, question)
            self._last_asker = agent_id
            self._clarify_count += 1
            await self._send_ui({"type": "your_turn"})
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.exception(f"_ask_clarifying failed: {e}")

    async def _run_solutions(self):
        """Each agent gives its recommendation in turn, then hand back to the user."""
        try:
            self._phase = "solving"
            await self._send_ui({"type": "status", "phase": "solving"})
            reply = await brain.run_solutions(self._problem, self._experts, self._history)
            utterances = reply.get("utterances") or []
            for utt in utterances:
                eid = utt.get("agent_id")
                text = (utt.get("text") or "").strip()
                if not text:
                    continue
                if eid not in self._voice_by_id:
                    eid = self._facilitator_id
                await self._speak(eid, text)

            closing = (reply.get("closing_question") or "").strip()
            if closing:
                await self._speak(self._facilitator_id, closing)
            await self._send_ui({"type": "your_turn"})
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.exception(f"_run_solutions failed: {e}")

    async def _summarize(self):
        if self._summarized:
            return
        self._summarized = True
        try:
            self._phase = "summarizing"
            await self._send_ui({"type": "status", "phase": "summarizing"})
            summary = await brain.summarize(self._problem, self._experts, self._history)
            await self._send_ui({"type": "summary", **summary})
            closing = summary.get("spoken_closing") or "That's the panel's take. The written recap is on your screen."
            await self._speak(self._facilitator_id, closing)
            self._phase = "ended"
            await self._send_ui({"type": "status", "phase": "ended"})
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.exception(f"_summarize failed: {e}")

    # ---- speaking + UI helpers --------------------------------------------

    async def _speak(self, agent_id: str, text: str):
        """Switch to the agent's voice, light up its tile, speak, and wait for playback end."""
        voice = self._voice_by_id.get(agent_id)
        voice_id = voice.get("id") if voice else None
        if voice_id:
            await self.push_frame(TTSUpdateSettingsFrame(settings={"voice": voice_id}))

        name = self._expert_name(agent_id)
        await self._send_ui({"type": "speaking", "role": agent_id, "name": name, "text": text})
        self._history.append({"role": agent_id, "name": name, "text": text})

        # Strict turn-taking: speak, then wait for the real end of playback so the next
        # utterance can't start mid-sentence. A generous timeout prevents a deadlock if the
        # BotStoppedSpeakingFrame is ever missed.
        self._utterance_done.clear()
        await self.push_frame(TTSSpeakFrame(text))
        try:
            await asyncio.wait_for(
                self._utterance_done.wait(), timeout=_estimate_secs(text) * 2 + 5
            )
        except asyncio.TimeoutError:
            logger.warning(f"Playback gate timed out for {agent_id!r}; continuing.")

    def _expert_name(self, eid: str) -> str:
        for e in self._experts:
            if e.get("id") == eid:
                return e.get("name", eid)
        return eid.capitalize()

    async def _send_ui(self, data: dict):
        await self.push_frame(RTVIServerMessageFrame(data=data))

    def _emit(self, data: dict):
        """Fire-and-forget UI message (does NOT touch the active speaking task)."""
        t = asyncio.create_task(self._send_ui(data))
        self._bg.add(t)
        t.add_done_callback(self._bg.discard)

    # ---- task management ---------------------------------------------------

    def _schedule(self, coro):
        """Run a coroutine as the active cancellable task."""
        self._cancel_active()
        self._active = asyncio.create_task(coro)

    def _cancel_active(self):
        if self._active and not self._active.done():
            self._active.cancel()
        self._active = None
