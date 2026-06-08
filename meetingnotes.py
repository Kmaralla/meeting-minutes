#!/usr/bin/env python3
"""
meetingnotes.py — Live meeting assistant

Architecture:
  Mic → Whisper STT → Dispatcher → AI agents (Claude or OpenAI, parallel)
      ├── transcriber     → output/transcription.md
      ├── note-taker      → output/meeting-notes.md
      ├── sketch-artist   → output/sketch.md  (Mermaid diagrams)
      └── interview-agent → output/interview-answers.md

Usage:
  python meetingnotes.py                  # live mic — press Enter to run agents
  python meetingnotes.py --dispatch-only  # re-run agents on saved transcript
"""

import os
import sys
import json
import time
import queue
import signal
import threading
import argparse
from pathlib import Path
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed

import numpy as np

from config import AGENT_STATUS_FILE
from llm import LLMError, generate_text, provider_label

# ─── Config ───────────────────────────────────────────────────────────────────

SAMPLE_RATE       = 16_000   # Hz — Whisper requires 16 kHz mono
BLOCK_SIZE        = 3_200    # frames per callback (~0.2 s)
SILENCE_THRESHOLD = 0.005    # RMS energy below this = silence
MIN_SPEECH_BLOCKS = 3        # ignore sounds shorter than ~0.6 s
SILENCE_BLOCKS    = 8        # consecutive silence blocks before flush (~1.6 s)
MAX_BUFFER_SECS   = 8        # force-flush if buffer grows beyond this
WHISPER_MODEL     = "base"   # tiny | base | small | medium | large-v3

# Auto-stop controls
AUTO_STOP_SILENCE_MINS = 5    # stop if no speech for this many minutes (0 = disabled)
AUTO_STOP_MAX_MINS     = 120  # hard ceiling in minutes (0 = disabled)

# Auto-dispatch: run agents automatically as speech accumulates
AUTO_DISPATCH_EVERY_CHUNKS = 3   # trigger after this many new transcribed chunks
AUTO_DISPATCH_MIN_INTERVAL = 60  # minimum seconds between auto-dispatches

OUTPUT_DIR         = Path.home() / "Desktop" / "meeting-output"
CUSTOM_AGENTS_FILE = Path.home() / ".config" / "meetingnotes" / "custom_agents.json"

FILES = {
    "transcriber":      OUTPUT_DIR / "transcription.md",
    "note-taker":       OUTPUT_DIR / "meeting-notes.md",
    "sketch-artist":    OUTPUT_DIR / "sketch.md",
    "interview-agent":  OUTPUT_DIR / "interview-answers.md",
    "action-extractor": OUTPUT_DIR / "actions.json",
}

# ─── Agent prompts ────────────────────────────────────────────────────────────

AGENT_PROMPTS = {
    "transcriber": """\
You are a precise transcriber for a live meeting recording.
You receive the full transcript accumulated so far and a CURRENT FILE showing what you've written.

Rules:
- Output the COMPLETE updated transcription.md content
- Preserve the existing file content verbatim, then append any new speech
- Add a [HH:MM] timestamp marker before each newly appended block
- Clean up filler words (um, uh, like) but preserve all meaning
- Format into natural paragraphs when there are natural pauses
- Output ONLY the markdown document — no meta-commentary or wrapping
- If a speaker is introduced by name (e.g. "Hi I'm Alex" or "Alex mentioned..."), prefix their speech segment with "**Alex:**"
- Format speaker turns as: **Name:** their words
- If speakers are unknown, use **Speaker:** or no prefix""",

    "note-taker": """\
You are a smart meeting note-taker maintaining a live structured summary.
You receive the full running transcript and the current notes file.

Output the COMPLETE updated meeting-notes.md using exactly these sections:

## Meeting Summary
(2–3 sentences describing the whole meeting so far)

## Key Points
(bullet list of important topics, insights, and statements)

## Decisions Made
(concrete decisions or conclusions reached — empty if none)

## Action Items
- [ ] Task description — Owner (if mentioned)

## Open Questions
(unresolved questions raised during the meeting)

Keep all sections current based on the full transcript. Be concise and scannable.""",

    "sketch-artist": """\
You are a meeting sketch artist for live meetings.
Analyze the full transcript and maintain a sketch.md with an evolving visual overview.

ALWAYS start with a nested markdown outline — this powers the live interactive mindmap:

# [Meeting Topic or Title]

## [Key Theme 1]
- Important point
- Important point
  - Sub-detail if needed

## [Key Theme 2]
- Important point

Keep the outline concise and scannable — 2–5 bullets per section, 2–4 sections max.

OPTIONALLY add Mermaid code blocks below the outline for genuine processes, flows, or sequences:

```mermaid
sequenceDiagram
  ...
```

STRICT OUTPUT RULES — violations break the rendering:
- Start with # (a heading). Zero words before it.
- End with the last bullet or mermaid block. Zero words after it.
- No "No updates", no "Sketch holds", no "Here is the current sketch", no status notes.
- The nested markdown outline is mandatory and always comes first.
- Mermaid diagrams are optional — only add them for genuine processes or flows.
- Update as the meeting evolves.
- If nothing notable yet, output exactly:

# Meeting Sketch
_Listening for meeting content..._""",

    "interview-agent": """\
You are a real-time interview and Q&A assistant monitoring a meeting.
You receive the full running transcript.

Identify every question asked — sentences ending in ? or phrased as questions.
For EVERY question, provide a concise, accurate, helpful answer.

Output the COMPLETE updated interview-answers.md:

# Q&A Log

**Q:** [exact question from transcript]
**A:** [clear answer — 1–4 sentences. Be direct and useful.]

---

List ALL questions found, oldest first.
If no questions have been asked yet, output:

# Q&A Log
_No questions detected yet..._""",

    "action-extractor": """\
You are an action-item extractor for a live meeting transcript.
Extract every commitment, task, scheduled meeting, research topic, or follow-up item.

Output ONLY a valid JSON array — no markdown fences, no explanation, no trailing text.

Each item must have these exact fields:
{
  "id":          "a<number>",
  "type":        "email" | "calendar" | "notion" | "research",
  "description": "clear, self-contained, actionable description",
  "owner":       "person who committed, or 'me' if unclear",
  "deadline":    "YYYY-MM-DD or empty string if not mentioned",
  "priority":    "high" | "medium" | "low",
  "context":     "1-2 sentence excerpt from transcript that triggered this item",
  "status":      "pending"
}

Type guide:
  email    — someone needs to send, share, or follow up in writing
  calendar — a meeting, sync, call, demo, or deadline to schedule
  notion   — a bug, ticket, task, feature, or work item to track
  research — a topic to investigate, look into, or compare (include specific comparison targets or metrics in context)

Priority guide:
  high   — blocking, urgent, time-sensitive (e.g. "needs to go out today", "before end of day")
  medium — important but not blocking (e.g. "next week", "when you get a chance")
  low    — nice-to-have, exploratory, or no deadline mentioned

Rules:
- If the CURRENT FILE contains existing items, preserve them (keep id and status unchanged).
- Add newly discovered items with the next available id number.
- Update description/context if more detail emerged, but never change status.
- If nothing actionable exists yet, output exactly: []""",
}

# ─── Global state ─────────────────────────────────────────────────────────────

PID_FILE      = Path("/tmp/meetingnotes.pid")
DISPATCH_FILE = Path("/tmp/meetingnotes.dispatch")  # touch to trigger dispatch

_lock = threading.Lock()
_chunks: list[str] = []   # accumulated transcript lines
_running = True


def _timed_queue_join(q: queue.Queue, timeout: float = 25.0) -> None:
    """queue.join() with a timeout so shutdown can never deadlock."""
    done = threading.Event()
    def _waiter():
        q.join()
        done.set()
    threading.Thread(target=_waiter, daemon=True).start()
    if not done.wait(timeout=timeout):
        # Force-drain any items the worker never picked up
        print(f"  [warn] Transcription queue drain timed out — discarding remaining items", flush=True)
        while not q.empty():
            try:
                q.get_nowait()
                q.task_done()
            except queue.Empty:
                break
_last_speech_time = time.time()  # tracks silence for auto-stop
_dispatch_requested = False      # set by keyboard thread or flag file
_transcribe_q: queue.Queue = queue.Queue()  # audio chunks pending transcription
_last_auto_dispatch_chunks = 0   # chunk count at last auto-dispatch
_last_auto_dispatch_time   = 0.0  # timestamp of last auto-dispatch


def _full_transcript() -> str:
    with _lock:
        return "\n".join(_chunks)


def _append_chunk(text: str, timestamp: str) -> None:
    with _lock:
        _chunks.append(f"[{timestamp}] {text}")


# ─── Custom agents ────────────────────────────────────────────────────────────

def _load_custom_agents() -> list:
    try:
        if CUSTOM_AGENTS_FILE.exists():
            return json.loads(CUSTOM_AGENTS_FILE.read_text())
    except Exception:
        pass
    return []


# ─── Agent execution ──────────────────────────────────────────────────────────

def _run_agent(name: str, transcript: str) -> tuple[str, bool, str]:
    import json as _json

    out_file = FILES[name]
    current  = out_file.read_text() if out_file.exists() else ""
    system   = AGENT_PROMPTS[name]

    user_msg    = f"CURRENT FILE CONTENT:\n{current}\n\nFULL MEETING TRANSCRIPT:\n{transcript}"
    full_prompt = f"{system}\n\n---\n\n{user_msg}"

    try:
        output = generate_text(full_prompt, timeout=120)

        # Strip markdown code fences if agent wrapped its output
        import re as _re
        fence_match = _re.search(r"```(?:markdown|json|)?\n([\s\S]*?)```", output)
        if fence_match:
            output = fence_match.group(1).strip()
        elif output.startswith("```"):
            lines = output.splitlines()
            output = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:]).strip()

        # Strip leading prose before the first # heading for markdown agents
        if name != "action-extractor":
            h = _re.search(r"^#", output, _re.MULTILINE)
            if h and h.start() > 0:
                output = output[h.start():]

        # For sketch: also strip trailing prose/commentary after the last markdown content line
        if name == "sketch-artist":
            lines = output.splitlines()
            while lines and not _re.match(r'^(#|-|\*|>|```|_|\s*$)', lines[-1].strip()):
                lines.pop()
            while lines and lines[-1].strip() in ('', '---', '===', '***'):
                lines.pop()
            output = '\n'.join(lines)

        # Validate JSON for the action-extractor; merge done statuses; skip write if malformed
        if name == "action-extractor":
            try:
                new_items = _json.loads(output)
            except _json.JSONDecodeError as e:
                return name, False, f"invalid JSON: {e}"
            # Robust merge: preserve done statuses + deduplicate by description
            if current.strip():
                try:
                    import re as _re2
                    prev_items = _json.loads(current)

                    def _norm(s: str) -> str:
                        return _re2.sub(r'\W+', ' ', (s or '').lower()).strip()[:60]

                    # Build lookup: normalized description → status from prev
                    prev_by_desc = {_norm(a.get("description","")): a.get("status","pending")
                                    for a in prev_items}
                    prev_done_ids = {a["id"] for a in prev_items if a.get("status") == "done"}

                    seen_descs: set[str] = set()
                    deduped: list = []
                    for a in new_items:
                        nd = _norm(a.get("description",""))
                        if nd in seen_descs:
                            continue  # duplicate — skip
                        seen_descs.add(nd)
                        # Restore done status from prev by ID or description match
                        if a.get("id") in prev_done_ids:
                            a["status"] = "done"
                        elif prev_by_desc.get(nd) == "done":
                            a["status"] = "done"
                        deduped.append(a)

                    output = _json.dumps(deduped)
                except Exception:
                    pass

        out_file.write_text(output + "\n")
        return name, True, ""

    except LLMError as e:
        err = str(e)[:200]
        if name != "action-extractor" and not current.strip():
            out_file.write_text(
                "# AI Agent Issue\n\n"
                f"_{name} could not generate content yet: {err}_\n"
            )
        return name, False, err


def _run_custom_agent(agent: dict, transcript: str) -> tuple[str, bool, str]:
    import re as _re
    agent_id = agent["id"]
    name     = agent["name"]
    out_file = OUTPUT_DIR / f"custom-{agent_id}.md"
    current  = out_file.read_text() if out_file.exists() else ""

    full_prompt = (
        f"{agent['prompt']}\n\n---\n\n"
        f"CURRENT FILE CONTENT:\n{current}\n\n"
        f"FULL MEETING TRANSCRIPT:\n{transcript}"
    )
    try:
        output = generate_text(full_prompt, timeout=120)
        fence_match = _re.search(r"```(?:markdown|json|)?\n([\s\S]*?)```", output)
        if fence_match:
            output = fence_match.group(1).strip()
        elif output.startswith("```"):
            lines  = output.splitlines()
            output = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:]).strip()
        # Strip leading prose before the first # heading
        h = _re.search(r"^#", output, _re.MULTILINE)
        if h and h.start() > 0:
            output = output[h.start():]
        out_file.write_text(output + "\n")
        return name, True, ""
    except LLMError as e:
        err = str(e)[:200]
        if not current.strip():
            out_file.write_text(
                "# AI Agent Issue\n\n"
                f"_{name} could not generate content yet: {err}_\n"
            )
        return name, False, err




def _write_agent_status(status: dict) -> None:
    try:
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        AGENT_STATUS_FILE.write_text(json.dumps(status, indent=2) + "\n")
    except Exception:
        pass

def dispatch(transcript: str, only_agent: str | None = None) -> None:
    if not transcript.strip():
        return
    custom = _load_custom_agents()
    selected_builtin = [name for name in AGENT_PROMPTS if not only_agent or name == only_agent]
    selected_custom = [ca for ca in custom if not only_agent or ca["name"] == only_agent or ca["id"] == only_agent]
    total = len(selected_builtin) + len(selected_custom)
    if total == 0:
        print(f"  [warn] No matching agent: {only_agent}", flush=True)
        return

    ts = datetime.now().strftime("%H:%M:%S")
    status = {
        "provider": provider_label(),
        "started_at": ts,
        "running": True,
        "agents": {name: {"state": "queued", "error": ""} for name in selected_builtin},
    }
    for ca in selected_custom:
        status["agents"][ca["name"]] = {"state": "queued", "error": ""}
    _write_agent_status(status)
    print(f"\n[{ts}] ⟳ Dispatching to {total} agents via {provider_label()} in parallel...", flush=True)

    with ThreadPoolExecutor(max_workers=max(total, 1)) as pool:
        futures: dict = {}
        for name in selected_builtin:
            status["agents"][name] = {"state": "running", "error": ""}
            futures[pool.submit(_run_agent, name, transcript)] = name
        for ca in selected_custom:
            status["agents"][ca["name"]] = {"state": "running", "error": ""}
            futures[pool.submit(_run_custom_agent, ca, transcript)] = ca["name"]
        _write_agent_status(status)

        for fut in as_completed(futures):
            name, ok, err = fut.result()
            icon = "✓" if ok else "✗"
            detail = f" ({err})" if not ok else ""
            status["agents"][name] = {"state": "ok" if ok else "error", "error": err}
            _write_agent_status(status)
            print(f"  {icon} {name}{detail}", flush=True)

    status["running"] = False
    status["completed_at"] = datetime.now().strftime("%H:%M:%S")
    _write_agent_status(status)
    print(flush=True)


# ─── Whisper STT ──────────────────────────────────────────────────────────────

def _load_whisper(model_name: str = WHISPER_MODEL):
    try:
        from faster_whisper import WhisperModel
    except ImportError:
        print(
            "ERROR: faster-whisper not installed.\n"
            "Run: pip install faster-whisper"
        )
        sys.exit(1)
    print(f"Loading Whisper model '{model_name}'...", flush=True)
    model = WhisperModel(model_name, device="cpu", compute_type="int8")
    print("Model ready.\n", flush=True)
    return model


def _transcribe_audio(model, audio: np.ndarray, timeout: float = 30.0, language: str = "en") -> str:
    """Transcribe one audio chunk. Returns empty string if Whisper hangs."""
    result: list[str] = [""]
    def _run():
        segs, _ = model.transcribe(
            audio,
            beam_size=3,
            language=language,
            vad_filter=True,
            vad_parameters={"min_silence_duration_ms": 500},
        )
        result[0] = " ".join(s.text.strip() for s in segs).strip()
    t = threading.Thread(target=_run, daemon=True)
    t.start()
    t.join(timeout=timeout)
    if t.is_alive():
        print("  [warn] Whisper timed out on chunk — skipping", flush=True)
    return result[0]


# ─── Main recording loop ───────────────────────────────────────────────────────

def _keyboard_listener() -> None:
    global _dispatch_requested, _running
    while _running:
        try:
            line = sys.stdin.readline()
            if line is not None:
                _dispatch_requested = True
        except Exception:
            break


def _write_raw_transcript() -> None:
    """Flush current chunks to transcription.md immediately so the UI shows live text."""
    try:
        with _lock:
            lines = list(_chunks)
        FILES["transcriber"].write_text("\n\n".join(lines) + "\n")
    except Exception:
        pass


def _maybe_auto_dispatch() -> None:
    """Fire agents automatically after enough new speech chunks accumulate."""
    global _last_auto_dispatch_chunks, _last_auto_dispatch_time
    with _lock:
        current = len(_chunks)
    new_chunks = current - _last_auto_dispatch_chunks
    time_since = time.time() - _last_auto_dispatch_time
    if new_chunks >= AUTO_DISPATCH_EVERY_CHUNKS and time_since >= AUTO_DISPATCH_MIN_INTERVAL:
        _last_auto_dispatch_chunks = current
        _last_auto_dispatch_time   = time.time()
        transcript = _full_transcript()
        threading.Thread(target=dispatch, args=(transcript,), daemon=True).start()
        print(f"  [auto] Dispatching after {new_chunks} new chunks...", flush=True)


def _transcription_worker(model, language: str = "en") -> None:
    """Background thread: drains _transcribe_q so the audio loop never blocks.
    After _running becomes False, finishes any items already in the queue
    so _transcribe_q.join() doesn't deadlock."""
    global _running
    while _running or not _transcribe_q.empty():
        try:
            arr, ts = _transcribe_q.get(timeout=0.5)
        except queue.Empty:
            if not _running:
                break   # queue empty and shutdown requested — we're done
            continue
        text = _transcribe_audio(model, arr, language=language)
        if text:
            _append_chunk(text, ts)
            _write_raw_transcript()   # live update — UI sees it within ~0.5s
            _maybe_auto_dispatch()    # auto-run agents every N chunks
            print(f"  ▶ [{ts}] {text}", flush=True)
        _transcribe_q.task_done()


def record_loop(model, max_minutes: int, silence_minutes: int, language: str = "en") -> None:
    global _running, _last_speech_time, _dispatch_requested
    try:
        import sounddevice as sd
    except ImportError:
        print("ERROR: sounddevice not installed.\nRun: pip install sounddevice")
        sys.exit(1)

    audio_q: queue.Queue[np.ndarray] = queue.Queue()

    def _callback(indata, frames, t, status):
        audio_q.put(indata[:, 0].copy())

    audio_buf:    list[np.ndarray] = []
    speech_blocks = 0
    silence_blocks = 0
    session_start  = time.time()
    _last_speech_time = session_start

    hard_stop  = session_start + max_minutes * 60 if max_minutes > 0 else None
    status_at  = session_start

    stop_lines = ["press Enter to run agents"]
    if max_minutes    > 0: stop_lines.append(f"max {max_minutes}m")
    if silence_minutes > 0: stop_lines.append(f"auto-stop after {silence_minutes}m silence")
    print(f"Mic active — {', '.join(stop_lines)}. Ctrl+C to stop.\n", flush=True)

    threading.Thread(target=_keyboard_listener, daemon=True).start()
    threading.Thread(target=_transcription_worker, args=(model, language), daemon=True).start()

    with sd.InputStream(
        samplerate=SAMPLE_RATE, channels=1, dtype="float32",
        blocksize=BLOCK_SIZE, callback=_callback,
    ):
        while _running:
            try:
                block = audio_q.get(timeout=0.5)
            except queue.Empty:
                _check_auto_stop(hard_stop, silence_minutes)
                if DISPATCH_FILE.exists():
                    try:
                        DISPATCH_FILE.unlink()
                    except Exception:
                        pass
                    _dispatch_requested = True
                if _dispatch_requested:
                    _dispatch_requested = False
                    transcript = _full_transcript()
                    threading.Thread(target=dispatch, args=(transcript,), daemon=True).start()
                continue

            now = time.time()
            rms = float(np.sqrt(np.mean(block ** 2)))
            is_speech = rms > SILENCE_THRESHOLD

            if is_speech:
                audio_buf.append(block)
                speech_blocks += 1
                silence_blocks = 0
                _last_speech_time = now
            else:
                silence_blocks += 1
                if audio_buf:
                    audio_buf.append(block)

            buf_secs = len(audio_buf) * BLOCK_SIZE / SAMPLE_RATE
            should_flush = (
                audio_buf
                and speech_blocks >= MIN_SPEECH_BLOCKS
                and (silence_blocks >= SILENCE_BLOCKS or buf_secs >= MAX_BUFFER_SECS)
            )

            if should_flush:
                arr = np.concatenate(audio_buf)
                audio_buf.clear()
                speech_blocks = 0
                silence_blocks = 0
                ts = datetime.now().strftime("%H:%M")
                _transcribe_q.put((arr, ts))

            if now - status_at >= 10:
                _print_status(session_start, hard_stop, silence_minutes)
                status_at = now

            if DISPATCH_FILE.exists():
                try:
                    DISPATCH_FILE.unlink()
                except Exception:
                    pass
                _dispatch_requested = True

            if _dispatch_requested:
                _dispatch_requested = False
                transcript = _full_transcript()
                threading.Thread(target=dispatch, args=(transcript,), daemon=True).start()

            _check_auto_stop(hard_stop, silence_minutes)


def _print_status(session_start: float, hard_stop: float | None, silence_minutes: int) -> None:
    elapsed   = int(time.time() - session_start)
    since_spk = int(time.time() - _last_speech_time)
    chunks    = len(_chunks)
    parts     = [
        f"elapsed {elapsed//60}m{elapsed%60:02d}s",
        f"chunks {chunks}",
        f"last speech {since_spk}s ago",
        "press Enter to run agents",
    ]
    if hard_stop:
        remaining = int(hard_stop - time.time())
        parts.append(f"hard stop in {remaining//60}m{remaining%60:02d}s")
    print(f"\r  ◉  {' · '.join(parts)}   ", end="", flush=True)


def _check_auto_stop(hard_stop: float | None, silence_minutes: int) -> None:
    global _running
    now = time.time()
    if hard_stop and now >= hard_stop:
        print(f"\n\nMax session time reached — stopping.", flush=True)
        _running = False
    elif silence_minutes > 0 and (now - _last_speech_time) >= silence_minutes * 60:
        print(f"\n\nNo speech for {silence_minutes} minutes — stopping.", flush=True)
        _running = False



# ─── Entry point ──────────────────────────────────────────────────────────────

def main() -> None:
    global _running

    parser = argparse.ArgumentParser(description="Live meeting assistant")
    parser.add_argument("--max-minutes",   type=int, default=AUTO_STOP_MAX_MINS,
        help=f"Hard stop after N minutes, 0=disabled (default: {AUTO_STOP_MAX_MINS})")
    parser.add_argument("--silence-stop",  type=int, default=AUTO_STOP_SILENCE_MINS,
        help=f"Stop after N minutes of silence, 0=disabled (default: {AUTO_STOP_SILENCE_MINS})")
    parser.add_argument("--dispatch-only", action="store_true",
        help="Skip recording; re-run all agents on saved transcription.md")
    parser.add_argument("--model",         default=WHISPER_MODEL,
        help=f"Whisper model size (default: {WHISPER_MODEL})")
    parser.add_argument("--language", default="en",
        help="Whisper transcription language code (default: en)")
    parser.add_argument("--agent", default="",
        help="Only run this specific agent name (use with --dispatch-only)")
    args = parser.parse_args()

    OUTPUT_DIR.mkdir(exist_ok=True)
    PID_FILE.write_text(str(os.getpid()))
    session_ts = datetime.now().strftime("%Y-%m-%d %H:%M")

    # Only pre-create the actions file (empty array); other files are written on first
    # transcription / dispatch so the UI doesn't show stale placeholder text.
    if not FILES["action-extractor"].exists():
        FILES["action-extractor"].write_text("[]\n")

    print("=" * 60)
    print("  Meeting Notes — Live Assistant")
    print(f"  Session: {session_ts}")
    print(f"  Output:  {OUTPUT_DIR.resolve()}/")
    print("=" * 60 + "\n")

    if args.dispatch_only:
        tx_file = FILES["transcriber"]
        if not tx_file.exists():
            print(f"ERROR: {tx_file} not found. Record a session first.")
            sys.exit(1)
        transcript = tx_file.read_text()
        dispatch(transcript, only_agent=args.agent or None)
        return

    model = _load_whisper(args.model)

    def _handle_exit(sig, frame):
        global _running
        print("\n\nStopping — saving final state...", flush=True)
        _running = False

    signal.signal(signal.SIGINT, _handle_exit)
    signal.signal(signal.SIGTERM, _handle_exit)

    try:
        record_loop(model, args.max_minutes, args.silence_stop, args.language)
    finally:
        PID_FILE.unlink(missing_ok=True)
        # Wait for any in-flight transcriptions, but not forever
        _timed_queue_join(_transcribe_q, timeout=25)
        # Final dispatch with everything accumulated
        final = _full_transcript()
        if final:
            print("Running final dispatch...", flush=True)
            dispatch(final)

        print("\n" + "=" * 60)
        print("  Session complete. Files saved:")
        for name, path in FILES.items():
            size = path.stat().st_size if path.exists() else 0
            print(f"    {path}  ({size} bytes)")
        print("=" * 60)


if __name__ == "__main__":
    main()
