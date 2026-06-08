import os
import shutil
import subprocess

import config


class LLMError(RuntimeError):
    pass


def _openai_key_set() -> bool:
    return bool(os.getenv("OPENAI_API_KEY", "").strip())


def provider_label() -> str:
    provider = config.ai_provider()
    if provider == "openai":
        return f"OpenAI ({config.openai_model()})"
    if provider == "claude":
        return "Claude CLI"
    return provider or "unknown"


def provider_status() -> dict:
    provider = config.ai_provider()
    if provider == "openai":
        try:
            import openai  # noqa: F401
        except ImportError:
            return {"provider": provider, "model": config.openai_model(), "ready": False,
                    "code": "openai_package_missing", "message": "OpenAI package is not installed. Run pip install -r requirements.txt."}
        if not _openai_key_set():
            return {"provider": provider, "model": config.openai_model(), "ready": False,
                    "code": "openai_key_missing", "message": "OpenAI API key is missing. Add OPENAI_API_KEY in Settings or .env."}
        return {"provider": provider, "model": config.openai_model(), "ready": True,
                "code": "ready", "message": "OpenAI provider is configured."}
    if provider == "claude":
        claude_bin = shutil.which("claude") or config.CLAUDE_BIN
        if not shutil.which("claude") and not os.path.exists(claude_bin):
            return {"provider": provider, "model": "", "ready": False,
                    "code": "claude_cli_missing", "message": "Claude Code CLI was not found on PATH."}
        return {"provider": provider, "model": "", "ready": True,
                "code": "ready", "message": "Claude CLI is available. If agents fail, run claude /login."}
    return {"provider": provider, "model": "", "ready": False,
            "code": "provider_invalid", "message": "AI_PROVIDER must be 'openai' or 'claude'."}


def generate_text(prompt: str, timeout: int = 120) -> str:
    provider = config.ai_provider()
    if provider == "claude":
        return _generate_with_claude(prompt, timeout)
    if provider == "openai":
        return _generate_with_openai(prompt, timeout)
    raise LLMError("Unsupported AI_PROVIDER '{}'. Use 'claude' or 'openai'.".format(provider))


def _generate_with_claude(prompt: str, timeout: int) -> str:
    try:
        proc = subprocess.run(
            [config.CLAUDE_BIN, "-p", prompt, "--tools", ""],
            capture_output=True, text=True, timeout=timeout,
            env={**os.environ}, stdin=subprocess.DEVNULL,
        )
    except subprocess.TimeoutExpired as exc:
        raise LLMError("timed out after {}s".format(timeout)) from exc
    except FileNotFoundError as exc:
        raise LLMError("'{}' not found - is Claude Code installed?".format(config.CLAUDE_BIN)) from exc

    output = proc.stdout.strip()
    if proc.returncode != 0 or not output:
        err = (proc.stderr or "").strip()
        if not err and not output:
            err = "Claude CLI returned no output. Run `claude /login`, or switch Settings to OpenAI."
        raise LLMError(_friendly_error(err))
    return output


def _generate_with_openai(prompt: str, timeout: int) -> str:
    if not _openai_key_set():
        raise LLMError("OpenAI API key is missing. Add OPENAI_API_KEY in Settings or .env.")
    try:
        from openai import OpenAI
    except ImportError as exc:
        raise LLMError("OpenAI package is not installed. Run: pip install -r requirements.txt") from exc

    try:
        client = OpenAI(timeout=timeout)
        response = client.responses.create(model=config.openai_model(), input=prompt)
    except Exception as exc:
        raise LLMError(_friendly_error(str(exc))) from exc

    text = getattr(response, "output_text", "") or ""
    if not text:
        text = _extract_response_text(response)
    if not text.strip():
        raise LLMError("no output")
    return text.strip()


def _extract_response_text(response) -> str:
    parts: list[str] = []
    for item in getattr(response, "output", []) or []:
        for content in getattr(item, "content", []) or []:
            text = getattr(content, "text", None)
            if text:
                parts.append(text)
    return "\n".join(parts)


def _friendly_error(raw: str) -> str:
    text = (raw or "").strip()
    low = text.lower()
    if "quota" in low or "exceeded your current quota" in low:
        return "OpenAI quota exceeded. Check API billing/credits for this project."
    if "rate limit" in low or "429" in low:
        return "AI provider rate limit hit. Wait a moment or reduce agent concurrency."
    if "not logged in" in low and "login" in low:
        return "Claude CLI is not logged in. Run: claude /login"
    if "api key" in low and ("invalid" in low or "incorrect" in low):
        return "OpenAI API key looks invalid. Check Settings or .env."
    return text[:300] or "no output"
