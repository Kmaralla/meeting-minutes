#!/usr/bin/env python3
"""
meetingnotes.py — Live meeting assistant

Architecture:
  Mic → Whisper STT → Dispatcher → Claude agents (parallel)
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
import time
import queue
import shutil
import signal
import threading
import subprocess
import argparse
from pathlib import Path
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed

import numpy as np

# ─── Config ───────────────────────────────────────────────────────────────────

SAMPLE_RATE       = 16_000   # Hz — Whisper requires 16 kHz mono
BLOCK_SIZE        = 3_200    # frames per callback (~0.2 s)
SILENCE_THRESHOLD = 0.005    # RMS energy below this = silence
MIN_SPEECH_BLOCKS = 3        # ignore sounds shorter than ~0.6 s
SILENCE_BLOCKS    = 10       # consecutive silence blocks before flush (~2 s)
MAX_BUFFER_SECS   = 12       # force-flush if buffer grows beyond this
WHISPER_MODEL     = "base"   # tiny | base | small | medium | large-v3
CLAUDE_BIN        = shutil.which("claude") or "claude"

# Auto-stop controls
AUTO_STOP_SILENCE_MINS = 5    # stop if no speech for this many minutes (0 = disabled)
AUTO_STOP_MAX_MINS     = 120  # hard ceiling in minutes (0 = disabled)

OUTPUT_DIR = Path.home() / "Desktop" / "meeting-output"

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
- Output ONLY the markdown document — no meta-commentary or wrapping""",

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
You are a technical diagram creator for live meetings.
Analyze the full transcript and maintain a sketch.md with Mermaid diagrams.

Create appropriate diagrams:
- Processes or workflows described → flowchart LR or TD
- System interactions, APIs, request flows → sequenceDiagram
- Topic relationships or concept maps → mindmap
- Data or class relationships → classDiagram or erDiagram
- Timelines or project roadmaps → gantt

Rules:
- Use ```mermaid fenced code blocks, one per diagram, each with a ## heading
- Replace or improve diagrams as your understanding of the content deepens
- Only create diagrams where the transcript provides enough substance
- If nothing diagram-worthy yet, output exactly:

# Sketches
_Waiting for diagrammable content..._""",

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
  "context":     "1-2 sentence excerpt from transcript that triggered this item",
  "status":      "pending"
}

Type guide:
  email    — someone needs to send, share, or follow up in writing
  calendar — a meeting, sync, call, demo, or deadline to schedule
  notion   — a bug, ticket, task, feature, or work item to track
  research — a topic to investigate, look into, or compare

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
_last_speech_time = time.time()  # tracks silence for auto-stop
_dispatch_requested = False      # set by keyboard thread or flag file


def _full_transcript() -> str:
    with _lock:
        return "\n".join(_chunks)


def _append_chunk(text: str, timestamp: str) -> None:
    with _lock:
        _chunks.append(f"[{timestamp}] {text}")


# ─── Agent execution ──────────────────────────────────────────────────────────

def _run_agent(name: str, transcript: str) -> tuple[str, bool, str]:
    import json as _json

    out_file = FILES[name]
    current  = out_file.read_text() if out_file.exists() else ""
    system   = AGENT_PROMPTS[name]

    user_msg    = f"CURRENT FILE CONTENT:\n{current}\n\nFULL MEETING TRANSCRIPT:\n{transcript}"
    full_prompt = f"{system}\n\n---\n\n{user_msg}"

    try:
        proc = subprocess.run(
            [CLAUDE_BIN, "-p", full_prompt, "--tools", ""],
            capture_output=True, text=True, timeout=120, env={**os.environ},
        )
        if proc.returncode != 0 or not proc.stdout.strip():
            return name, False, (proc.stderr or "no output").strip()[:200]

        output = proc.stdout.strip()

        # Strip preamble prose and markdown code fences if agent wrapped its output
        import re as _re
        fence_match = _re.search(r"```(?:markdown|json|)?\n([\s\S]*?)```", output)
        if fence_match:
            output = fence_match.group(1).strip()
        elif output.startswith("```"):
            lines = output.splitlines()
            output = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:]).strip()

        # Validate JSON for the action-extractor; skip write if malformed
        if name == "action-extractor":
            try:
                _json.loads(output)
            except _json.JSONDecodeError as e:
                return name, False, f"invalid JSON: {e}"

        out_file.write_text(output + "\n")
        return name, True, ""

    except subprocess.TimeoutExpired:
        return name, False, "timed out after 120 s"
    except FileNotFoundError:
        return name, False, f"'{CLAUDE_BIN}' not found — is Claude Code installed?"


def dispatch(transcript: str) -> None:
    if not transcript.strip():
        return
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"\n[{ts}] ⟳ Dispatching to {len(AGENT_PROMPTS)} agents in parallel...", flush=True)

    with ThreadPoolExecutor(max_workers=len(AGENT_PROMPTS)) as pool:
        futures = {
            pool.submit(_run_agent, name, transcript): name
            for name in AGENT_PROMPTS
        }
        for fut in as_completed(futures):
            name, ok, err = fut.result()
            icon = "✓" if ok else "✗"
            detail = f" ({err})" if not ok else f" → {FILES[name]}"
            print(f"  {icon} {name}{detail}", flush=True)

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


def _transcribe_audio(model, audio: np.ndarray) -> str:
    segs, _ = model.transcribe(
        audio,
        beam_size=3,
        language="en",
        vad_filter=True,
        vad_parameters={"min_silence_duration_ms": 500},
    )
    return " ".join(s.text.strip() for s in segs).strip()


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


def record_loop(model, max_minutes: int, silence_minutes: int) -> None:
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

                text = _transcribe_audio(model, arr)
                if text:
                    ts = datetime.now().strftime("%H:%M")
                    _append_chunk(text, ts)
                    print(f"  ▶ [{ts}] {text}", flush=True)

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
    args = parser.parse_args()

    OUTPUT_DIR.mkdir(exist_ok=True)
    PID_FILE.write_text(str(os.getpid()))
    session_ts = datetime.now().strftime("%Y-%m-%d %H:%M")

    for name, path in FILES.items():
        if not path.exists():
            if name == "action-extractor":
                path.write_text("[]\n")
            else:
                title = name.replace("-", " ").title()
                path.write_text(f"# {title}\n_Session started: {session_ts}_\n\n")

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
        dispatch(transcript)
        return

    model = _load_whisper(args.model)

    def _handle_exit(sig, frame):
        global _running
        print("\n\nStopping — saving final state...", flush=True)
        _running = False

    signal.signal(signal.SIGINT, _handle_exit)
    signal.signal(signal.SIGTERM, _handle_exit)

    try:
        record_loop(model, args.max_minutes, args.silence_stop)
    finally:
        PID_FILE.unlink(missing_ok=True)
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
