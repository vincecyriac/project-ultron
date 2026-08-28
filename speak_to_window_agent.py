"""
Turns a spoken instruction plus the surrounding window into text to inject.

One model call takes the raw microphone audio and the window context together.
Transcribing first and transforming second would double the latency for a
feature the user is holding a key down through, and it would throw away the
context that makes an ambiguous instruction resolvable.
"""

import io
import re
import wave

from google.genai import types

WINDOW_MODEL = "gemini-3.1-flash-lite"
SAMPLE_RATE = 16000

SYSTEM_TRANSFORM_PROMPT = """You write text directly into whatever application the user is currently focused on.

The audio is a spoken instruction. You are also given the text already in that window. Carry out the instruction and return ONLY the text that should end up in the window.

ABSOLUTE OUTPUT RULE
Your entire response is inserted verbatim at the user's cursor. So:
- No preamble, no sign-off, no "Here is", no explanation of what you did.
- No markdown code fences unless the target document itself is markdown.
- No quotation marks around the result.
- No commentary about the instruction.
If you cannot carry out the instruction, return exactly: NO_ACTION

NEVER INVENT A DOCUMENT
If the instruction refers to text that should already be there — "fix this", "correct the spelling", "rewrite it", "make it shorter", "summarise this" — and the window text below is empty or missing, return exactly NO_ACTION. Do not write a placeholder, an example, an apology, or a hello-world. Whatever you return is typed straight into a real document, so inventing content there destroys the user's work. Only write from scratch when the instruction itself supplies what to write.

MATCH THE APPLICATION
- Code editors (VS Code, Cursor, Xcode, Zed, JetBrains): emit source code only. Match the indentation, quote style, and naming conventions visible in the surrounding text. Comments only where the file already uses them.
- Terminal (Terminal, iTerm, Warp, Ghostty): emit the raw shell command with no prompt symbol, no `$`, and no explanation. One command unless the user asked for several.
- Mail, Slack, Messages, Outlook, Teams, Discord: match the register of the existing thread. A terse channel gets a terse reply; a formal email gets full sentences. Never invent facts, names, dates, or commitments that are not in the context.
- Anything else: plain prose matching the surrounding document's tone and tense.

WORKING WITH THE CONTEXT
- If a selection is given, the instruction applies to that selection and your output replaces it. Preserve its leading indentation.
- If no selection is given, the context is what precedes the cursor. Your output continues from it — do not repeat what is already there.
- Rewrites keep the author's meaning. Fix what was asked; leave the rest.
"""


def pcm_to_wav(pcm: bytes, sample_rate: int = SAMPLE_RATE) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        w.writeframes(pcm)
    return buf.getvalue()


def build_context_block(app_name: str, kind: str, existing_context: str,
                        has_selection: bool, title: str = None) -> str:
    lines = [f"Focused application: {app_name} (treat as: {kind})"]
    if title:
        lines.append(f"Field or document: {title}")
    if has_selection:
        lines.append("The user has SELECTED the text below. Your output replaces it exactly.")
    elif existing_context:
        lines.append("Text already in the window, ending at the cursor. Continue from it.")
    else:
        lines.append("No window text could be read. If the instruction refers to existing "
                     "text, return NO_ACTION rather than inventing any.")
    if existing_context:
        lines.append("--- WINDOW TEXT ---")
        lines.append(existing_context)
        lines.append("--- END WINDOW TEXT ---")
    return "\n".join(lines)


def restore_base_indent(selection: str, replacement: str) -> str:
    """Re-apply the indentation the model dropped.

    A selection made by dragging from the start of a line carries its own
    leading whitespace, and that whitespace is part of what gets replaced. Every
    model tested rewrites the block flush-left instead, which lands `def` in
    column 0 inside a class body. Shifting the whole replacement keeps the
    model's relative structure and restores the absolute position.
    """
    if not selection or not replacement:
        return replacement
    base = re.match(r"[ \t]*", selection).group(0)
    if not base:
        return replacement
    if re.match(r"[ \t]*", replacement).group(0).startswith(base):
        return replacement                      # the model kept it
    return "\n".join(base + line if line.strip() else line
                     for line in replacement.split("\n"))


async def process_window_voice_action(client, app_name: str, kind: str,
                                      existing_context: str, has_selection: bool,
                                      audio_pcm: bytes = None, instruction: str = None,
                                      title: str = None) -> dict:
    """Returns {ok, text, reason}. `text` is ready to inject as-is."""
    if not audio_pcm and not instruction:
        return {"ok": False, "text": "", "reason": "no instruction captured"}

    parts = [types.Part.from_text(
        text=build_context_block(app_name, kind, existing_context, has_selection, title))]
    if audio_pcm:
        parts.append(types.Part.from_bytes(data=pcm_to_wav(audio_pcm), mime_type="audio/wav"))
    else:
        parts.append(types.Part.from_text(text=f"Spoken instruction: {instruction}"))

    try:
        response = await client.aio.models.generate_content(
            model=WINDOW_MODEL,
            contents=[types.Content(role="user", parts=parts)],
            config=types.GenerateContentConfig(
                system_instruction=SYSTEM_TRANSFORM_PROMPT,
                temperature=0.3,
                thinking_config=types.ThinkingConfig(thinking_budget=0),
            ),
        )
    except Exception as exc:
        return {"ok": False, "text": "", "reason": str(exc)[:160]}

    text = (response.text or "").strip()
    if not text or text == "NO_ACTION":
        return {"ok": False, "text": "", "reason": "the instruction could not be applied here"}

    # The model occasionally fences code even when told not to; the fence would
    # be pasted literally into the user's editor.
    if text.startswith("```"):
        body = text.split("\n", 1)[1] if "\n" in text else ""
        text = body.rsplit("```", 1)[0].rstrip() if "```" in body else body.rstrip()

    if has_selection:
        text = restore_base_indent(existing_context, text)

    return {"ok": True, "text": text, "reason": ""}
