import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { PipecatClient } from "@pipecat-ai/client-js";
import { SmallWebRTCTransport } from "@pipecat-ai/small-webrtc-transport";

const OFFER_URL =
  import.meta.env.VITE_PANEL_API || "http://localhost:8000/api/offer";

const PHASE_LABEL = {
  await_problem: "Tell the panel your problem",
  assembling: "Assembling your panel…",
  intros: "Introductions",
  clarifying: "Getting to know your problem",
  solving: "Working on solutions",
  summarizing: "Writing the recap…",
  ended: "Meeting ended",
};

function initials(name = "") {
  const p = name.trim().split(/\s+/).filter(Boolean);
  if (p.length === 0) return "··";
  if (p.length === 1) return p[0].slice(0, 2).toUpperCase();
  return (p[0][0] + p[p.length - 1][0]).toUpperCase();
}

export default function App() {
  const clientRef = useRef(null);
  const audioRef = useRef(null);

  const [status, setStatus] = useState("idle"); // idle | connecting | connected | ended | error
  const [phase, setPhase] = useState("await_problem");
  const [topic, setTopic] = useState("");
  const [agents, setAgents] = useState([]); // [{id,name,role,personality,voice,facilitator}]
  const [activeId, setActiveId] = useState(null); // agent id | "user" | null
  const [caption, setCaption] = useState("");
  const [transcript, setTranscript] = useState([]);
  const [yourTurn, setYourTurn] = useState(false);
  const [summary, setSummary] = useState(null);
  const [error, setError] = useState("");
  const [draft, setDraft] = useState("");
  const [micMuted, setMicMuted] = useState(true); // muted by default — avoids mic-picks-up-speaker feedback
  const [audioMuted, setAudioMuted] = useState(false);
  const [problemText, setProblemText] = useState("");
  const [submittedProblem, setSubmittedProblem] = useState("");
  const pendingProblemRef = useRef("");

  const scriptEndRef = useRef(null);
  useEffect(() => {
    scriptEndRef.current?.scrollIntoView({ behavior: "smooth", block: "end" });
  }, [transcript, caption]);

  const handleServerMessage = useCallback((data) => {
    if (!data || typeof data !== "object") return;
    switch (data.type) {
      case "status":
        setPhase(data.phase);
        break;
      case "error":
        setError(data.message || "Something went wrong on the panel.");
        break;
      case "panel":
        setTopic(data.topic || "");
        setAgents(data.experts || []);
        break;
      case "speaking":
        setActiveId(data.role);
        setYourTurn(false);
        setCaption(data.text || "");
        setTranscript((t) => [...t, { role: data.role, name: data.name, text: data.text }]);
        break;
      case "user_interim":
        setActiveId("user");
        setCaption(data.text || "");
        break;
      case "user":
        setActiveId("user");
        setYourTurn(false);
        setCaption("");
        setTranscript((t) => [...t, { role: "user", name: "You", text: data.text }]);
        break;
      case "your_turn":
        setActiveId(null);
        setYourTurn(true);
        setCaption("");
        break;
      case "summary":
        setSummary(data);
        setYourTurn(false);
        break;
      default:
        break;
    }
  }, []);

  // Mute by disabling the local audio track (it stays in the peer connection and sends
  // silence). Do NOT use enableMic(false) — that stops/replaces the track, which the
  // backend sees as a media error and tears down the whole bot.
  const applyMicMute = useCallback((muted) => {
    try {
      const t = clientRef.current?.tracks?.()?.local?.audio;
      if (t) {
        t.enabled = !muted;
        return true;
      }
    } catch (e) {
      console.error(e);
    }
    return false;
  }, []);

  const start = useCallback(async () => {
    setError("");
    setStatus("connecting");
    setSummary(null);
    setTranscript([]);
    setAgents([]);
    setTopic("");
    setCaption("");
    setActiveId(null);
    setYourTurn(false);
    setSubmittedProblem("");
    setMicMuted(true);
    setAudioMuted(false);

    try {
      const transport = new SmallWebRTCTransport({ webrtcUrl: OFFER_URL });
      const client = new PipecatClient({
        transport,
        enableMic: true,
        enableCam: false,
        callbacks: {
          onConnected: () => {
            setStatus("connected");
            // Start muted (track stays alive, just disabled) so the bot's own voice
            // isn't captured + re-transcribed. Retry until the track exists.
            let tries = 0;
            const iv = setInterval(() => {
              if (applyMicMute(true) || ++tries > 25) clearInterval(iv);
            }, 150);
          },
          onDisconnected: () => {
            setStatus("ended");
            setActiveId(null);
            setYourTurn(false);
          },
          onServerMessage: handleServerMessage,
          onUserStartedSpeaking: () => {
            setActiveId("user");
            setYourTurn(false);
          },
          onBotStoppedSpeaking: () => setActiveId((r) => (r === "user" ? r : null)),
          onTrackStarted: (track, participant) => {
            if (!participant?.local && track.kind === "audio" && audioRef.current) {
              audioRef.current.srcObject = new MediaStream([track]);
              audioRef.current.play().catch(() => {});
            }
          },
        },
      });
      clientRef.current = client;
      await client.connect();
    } catch (e) {
      console.error(e);
      setError(String(e?.message || e));
      setStatus("error");
    }
  }, [handleServerMessage, applyMicMute]);

  const startMeeting = useCallback(
    (e) => {
      e?.preventDefault?.();
      const p = problemText.trim();
      if (!p) return;
      pendingProblemRef.current = p;
      start();
      setSubmittedProblem(p);
    },
    [problemText, start]
  );

  const sendText = useCallback(
    (e) => {
      e?.preventDefault?.();
      const text = draft.trim();
      if (!text) return;
      try {
        clientRef.current?.sendClientMessage("user_text", { text });
        setDraft("");
      } catch (err) {
        console.error(err);
      }
    },
    [draft]
  );

  const endAndSummarize = useCallback(() => {
    try {
      clientRef.current?.sendClientMessage("end_session");
      setPhase("summarizing");
    } catch (e) {
      console.error(e);
    }
  }, []);

  const toggleMic = useCallback(() => {
    setMicMuted((m) => {
      const next = !m;
      applyMicMute(next);
      return next;
    });
  }, [applyMicMute]);

  const toggleAudio = useCallback(() => {
    setAudioMuted((m) => {
      const next = !m;
      if (audioRef.current) audioRef.current.muted = next;
      return next;
    });
  }, []);

  const hangUp = useCallback(async () => {
    try {
      await clientRef.current?.disconnect();
    } catch (e) {
      console.error(e);
    }
    clientRef.current = null;
    setStatus("ended");
  }, []);

  useEffect(() => () => clientRef.current?.disconnect().catch(() => {}), []);

  const connected = status === "connected";
  const live = connected || status === "ended";
  const hasPanel = agents.length > 0;

  // Send the problem typed in the lobby once connected, retrying until the backend
  // actually moves past "await_problem" (the bot may not be ready on the first try).
  useEffect(() => {
    if (!connected) return;
    if (phase !== "await_problem") {
      pendingProblemRef.current = ""; // panel forming/formed — stop trying
      return;
    }
    if (!pendingProblemRef.current) return;

    const send = () => {
      const p = pendingProblemRef.current;
      if (!p) return;
      try {
        clientRef.current?.sendClientMessage("user_text", { text: p });
      } catch (e) {
        console.error(e);
      }
    };
    send();
    let tries = 0;
    const iv = setInterval(() => {
      if (++tries > 6 || phase !== "await_problem" || !pendingProblemRef.current) {
        clearInterval(iv);
        return;
      }
      send();
    }, 1200);
    return () => clearInterval(iv);
  }, [connected, phase]);

  // Total tiles = agents + your own tile; used to size the grid.
  const tileCount = (hasPanel ? agents.length : 0) + 1;

  const activeName =
    activeId === "user"
      ? "You"
      : agents.find((a) => a.id === activeId)?.name || "";

  return (
    <div className="studio">
      <div className="grain" aria-hidden />

      <header className="masthead">
        <div className="brand">
          <span className={`lamp ${connected ? "live" : ""}`} aria-hidden />
          <span className="wordmark">The&nbsp;Panel</span>
          <span className="brand-sub">live expert roundtable</span>
        </div>
        <div className="status-cluster">
          {activeId && (
            <span className="now">
              <span className="now-bars" aria-hidden><i /><i /><i /></span>
              <b>{activeId === "user" ? "You" : activeName}</b>
              <span className="now-label">on mic</span>
            </span>
          )}
          <span className={`phase ${connected ? "on" : ""}`}>
            {live ? PHASE_LABEL[phase] || "Live" : "Off air"}
          </span>
        </div>
      </header>

      {topic && (
        <div className="topic-strip">
          <span className="topic-kick">On the table</span>
          <span className="topic-text">{topic}</span>
        </div>
      )}

      {error && <div className="error">{error}</div>}

      <div className="body">
        <main className="stage">
          {!live && status !== "connecting" && (
            <div className="lobby">
              <p className="lobby-kick">A voice-first panel of experts</p>
              <h1>Bring a panel<br /><em>into the room.</em></h1>
              <p className="lobby-lede">
                Type the problem you're chewing on. A panel of experts joins the call,
                introduces themselves, asks you questions to understand it — then talks you
                through solutions, out loud.
              </p>
              <form className="lobby-form" onSubmit={startMeeting}>
                <textarea
                  value={problemText}
                  onChange={(e) => setProblemText(e.target.value)}
                  placeholder="e.g. I want to start a coffee shop near campus but I'm worried about money."
                  rows={3}
                  onKeyDown={(e) => {
                    if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) startMeeting(e);
                  }}
                />
                <button className="cta" type="submit" disabled={!problemText.trim()}>
                  <span className="cta-lamp" /> Start the session
                </button>
              </form>
              <p className="lobby-hint">After this you talk by voice — the mic turns on once the call starts.</p>
            </div>
          )}

          {status === "connecting" && (
            <div className="lobby waiting">
              <div className="spinner" />
              <h1>Going live…</h1>
            </div>
          )}

          {live && !hasPanel && (
            <div className="lobby waiting">
              <div className="onair-badge"><span className="lamp live" /> ON AIR</div>
              <h1>{phase === "assembling" ? "Assembling your panel…" : "Bringing your panel in…"}</h1>
              {submittedProblem ? (
                <p className="on-table"><span>On the table</span> “{submittedProblem}”</p>
              ) : (
                <p className="lobby-lede">Setting up the call — say your problem out loud or type it below.</p>
              )}
              <p className="waiting-sub">
                {phase === "assembling"
                  ? "Picking the right experts and seating them. One moment…"
                  : "Connecting you to the room…"}
              </p>
            </div>
          )}

          {live && hasPanel && (
            <section className={`grid tiles-${Math.min(tileCount, 6)}`}>
              {agents.map((a, i) => (
                <Tile
                  key={a.id}
                  index={i}
                  name={a.name}
                  role={a.role}
                  facilitator={a.facilitator}
                  active={activeId === a.id}
                  speaking={!!activeId}
                />
              ))}
              <Tile
                you
                index={agents.length}
                name="You"
                role="You"
                active={activeId === "user"}
                speaking={!!activeId}
                waiting={yourTurn}
                micMuted={micMuted}
              />
            </section>
          )}

          {caption && activeId && (
            <div className="lower-third">
              <span className={`lt-name ${activeId === "user" ? "you" : ""}`}>
                {activeId === "user" ? "You" : activeName}
              </span>
              <span className="lt-text">{caption}</span>
            </div>
          )}
        </main>

        <aside className="rail">
          <div className="rail-card">
            <h3 className="rail-title">Transcript</h3>
            <div className="script">
              {transcript.length === 0 && (
                <p className="rail-empty">The conversation appears here, line by line.</p>
              )}
              {transcript.map((line, i) => (
                <div key={i} className={`script-line ${line.role === "user" ? "user" : "agent"}`}>
                  <span className="spk">{line.name}</span>
                  <p>{line.text}</p>
                </div>
              ))}
              <div ref={scriptEndRef} />
            </div>
          </div>

          {summary && (
            <div className="rail-card notes">
              <h3 className="rail-title">Show notes</h3>
              <Notes summary={summary} />
            </div>
          )}
        </aside>
      </div>

      {/* control dock */}
      <div className="dock">
        {connected && (
          <form className="composer" onSubmit={sendText}>
            <input
              type="text"
              value={draft}
              onChange={(e) => setDraft(e.target.value)}
              placeholder={hasPanel ? "Type your reply…" : "Type your problem…"}
              aria-label="Type your problem or reply"
            />
            <button type="submit" className="send" disabled={!draft.trim()}>
              Send
            </button>
          </form>
        )}

        {!connected && status !== "connecting" && (
          <button className="cta dock-cta" onClick={start}>
            <span className="cta-lamp" /> {status === "ended" ? "Rejoin" : "Join"}
          </button>
        )}

        {connected && micMuted && (
          <span className="mic-hint">Muted — tap the mic to answer by voice, or type above. Headphones recommended.</span>
        )}

        {connected && (
          <div className="dock-controls">
            <button
              className={`round ${micMuted ? "off" : "on"} ${yourTurn && micMuted ? "nudge" : ""}`}
              onClick={toggleMic}
              title={micMuted ? "Unmute mic" : "Mute mic"}
              aria-label={micMuted ? "Unmute mic" : "Mute mic"}
            >
              <IconMic muted={micMuted} />
            </button>
            <button
              className={`round ${audioMuted ? "off" : "on"}`}
              onClick={toggleAudio}
              title={audioMuted ? "Unmute panel" : "Mute panel"}
              aria-label={audioMuted ? "Unmute panel" : "Mute panel"}
            >
              <IconSpeaker muted={audioMuted} />
            </button>
            <button className="round leave" onClick={hangUp} title="Leave" aria-label="Leave">
              <IconLeave />
            </button>
          </div>
        )}
      </div>

      <audio ref={audioRef} autoPlay playsInline hidden />
    </div>
  );
}

// Warm, cohesive avatar tint per name — espresso/amber/olive range, not the full rainbow.
const AVATAR_HUES = [26, 12, 44, 8, 38, 20, 50, 32];
function avatarColor(seed = "") {
  let h = 0;
  for (let i = 0; i < seed.length; i++) h = (h * 31 + seed.charCodeAt(i)) % 997;
  const hue = AVATAR_HUES[h % AVATAR_HUES.length];
  return `linear-gradient(150deg, hsl(${hue} 46% 44%), hsl(${hue} 42% 28%))`;
}

function Tile({ index, name, role, facilitator, active, speaking, you, waiting, micMuted }) {
  // When someone else is speaking, dim the rest (listening).
  const listening = speaking && !active;
  const cls = [
    "tile",
    you ? "tile-you" : "",
    active ? "active" : "",
    listening ? "listening" : "",
    !speaking && waiting && you ? "your-turn" : "",
  ]
    .filter(Boolean)
    .join(" ");
  return (
    <article className={cls} style={{ animationDelay: `${index * 130}ms` }}>
      <div className="tile-media">
        <div
          className="avatar"
          style={{ background: you ? "linear-gradient(150deg,#3a3128,#241d16)" : avatarColor(name) }}
        >
          {you ? <IconYou /> : initials(name)}
          {active && <span className="halo" aria-hidden />}
        </div>
        {active && (
          <span className="bars" aria-hidden>
            <i /><i /><i /><i /><i />
          </span>
        )}
      </div>

      {facilitator && <span className="host-chip">Host</span>}

      <div className="plate">
        {you && micMuted && <IconMicSmall />}
        <span className="plate-name">{name}</span>
        {role && !you && <span className="plate-role">{role}</span>}
      </div>

      {active && <span className="state speaking">On mic</span>}
      {listening && <span className="state listen">listening</span>}
      {!speaking && waiting && you && <span className="state mine">your turn — unmute</span>}
    </article>
  );
}

/* ---- icons (inline SVG, currentColor) ---- */
function IconMic({ muted }) {
  return muted ? (
    <svg viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <path d="M1 1l22 22" /><path d="M9 9v3a3 3 0 0 0 5.12 2.12M15 9.34V4a3 3 0 0 0-5.94-.6" />
      <path d="M17 16.95A7 7 0 0 1 5 12v-2m14 0v2a7 7 0 0 1-.11 1.23" /><path d="M12 19v3" />
    </svg>
  ) : (
    <svg viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <rect x="9" y="2" width="6" height="12" rx="3" /><path d="M5 10v2a7 7 0 0 0 14 0v-2" /><path d="M12 19v3" />
    </svg>
  );
}
function IconMicSmall() {
  return (
    <svg viewBox="0 0 24 24" width="13" height="13" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round" className="plate-mic">
      <path d="M1 1l22 22" /><path d="M9 9v3a3 3 0 0 0 5.12 2.12M15 9.34V5a3 3 0 0 0-5.94-.6" /><path d="M12 19v3" />
    </svg>
  );
}
function IconSpeaker({ muted }) {
  return (
    <svg viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <path d="M11 5L6 9H2v6h4l5 4V5z" />
      {muted ? <path d="M23 9l-6 6M17 9l6 6" /> : <path d="M15.5 8.5a5 5 0 0 1 0 7M19 5a9 9 0 0 1 0 14" />}
    </svg>
  );
}
function IconLeave() {
  return (
    <svg viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <path d="M10.68 13.31a16 16 0 0 0 3.41 2.6l1.27-1.27a2 2 0 0 1 2.11-.45 12.8 12.8 0 0 0 2.5.7A2 2 0 0 1 22 16.92v2a2 2 0 0 1-2.18 2A19.8 19.8 0 0 1 3.07 5.18 2 2 0 0 1 5 3h2a2 2 0 0 1 2 1.72c.13.91.36 1.79.7 2.5a2 2 0 0 1-.45 2.11L8 10.5" />
      <path d="M23 1L1 23" />
    </svg>
  );
}
function IconYou() {
  return (
    <svg viewBox="0 0 24 24" width="30" height="30" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round">
      <circle cx="12" cy="8" r="4" /><path d="M4 20a8 8 0 0 1 16 0" />
    </svg>
  );
}

function Notes({ summary }) {
  const copy = () => {
    const text = [
      `Topic: ${summary.topic || ""}`,
      "",
      "Key points:",
      ...(summary.key_points || []).map((p) => `- ${p}`),
      "",
      "Disagreements:",
      ...(summary.disagreements || []).map((p) => `- ${p}`),
      "",
      "Next steps:",
      ...(summary.next_steps || []).map((p) => `- ${p}`),
    ].join("\n");
    navigator.clipboard?.writeText(text).catch(() => {});
  };
  return (
    <div className="notes-body">
      <NoteBlock title="Key points" items={summary.key_points} />
      <NoteBlock title="Where they disagreed" items={summary.disagreements} />
      <NoteBlock title="Next steps" items={summary.next_steps} />
      <button className="ctl tiny" onClick={copy}>Copy recap</button>
    </div>
  );
}

function NoteBlock({ title, items }) {
  if (!items || items.length === 0) return null;
  return (
    <div className="note-block">
      <h4>{title}</h4>
      <ul>
        {items.map((it, i) => (
          <li key={i}>{it}</li>
        ))}
      </ul>
    </div>
  );
}
