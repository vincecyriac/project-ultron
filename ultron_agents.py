"""
Background agent tier for Project Ultron.

Gemini Live is a conversational surface: it must stay responsive for barge-in,
so it never runs long multi-step work itself. Instead it acknowledges verbally
and dispatches a goal here, where a specialised model grinds through the tool
loop off the audio path.

Tiers
  os       gemini-3.1-pro-preview  — macOS automation, AppleScript, shell,
                                     multi-step GUI control and system logic.
  spatial  gemini-3.7-flash        — fast structured JSON: 3D scene deltas for
                                     the SVE, screen-coordinate denormalisation.

The agent reuses the hub's own tool implementations: execute_tool is injected
so there is exactly one definition of what every tool does.
"""

import asyncio
import json

from google.genai import types

OS_AGENT_MODEL = "gemini-3.1-pro-preview"
SVE_AGENT_MODEL = "gemini-3.7-flash"
SENTINEL_MODEL = "gemini-3.7-flash"      # fast, cheap, runs on every typing pause

MAX_STEPS = 12          # tool round-trips before the agent must conclude
STEP_TIMEOUT_S = 120.0  # per model call

_SHARED_RULES = (
    "You are a background execution agent for Ultron, operating on Vince's macOS machine. "
    "You were dispatched with one goal and you run without further user input, so never ask "
    "questions — decide and act. Chain as many tool calls as the goal needs, verifying results "
    "as you go. Never perform destructive actions (deleting files, sending messages or emails, "
    "purchases) unless the goal explicitly says to. "
    "When finished, reply with ONE short sentence describing the outcome, phrased so it can be "
    "read aloud to Vince. No markdown, no lists, no preamble."
)

TIERS = {
    "os": {
        "model": OS_AGENT_MODEL,
        "label": "OS automation",
        "system": _SHARED_RULES + (
            " You specialise in macOS control: shell commands, AppleScript, window and app "
            "management, and clicking/typing through the GUI. Prefer a shell command or "
            "AppleScript over GUI clicking when both would work. When you must use the GUI, "
            "call look_at_screen first, act, then look again to verify before continuing."
        ),
    },
    "spatial": {
        "model": SVE_AGENT_MODEL,
        "label": "spatial visualisation",
        "system": _SHARED_RULES + (
            " You specialise in the Spatial Visualization Engine: building and editing live 3D "
            "scenes. To change a scene that already exists, always emit update_3d_scene "
            "operations — never recreate it.\n"
            "COMPOSITION RULES, follow all of them:\n"
            "1. LABEL SPARINGLY. Label only the 3-6 parts that actually matter to the "
            "explanation. Labelling every part buries the model in floating text and is the "
            "single worst thing you can do to a scene. Everything else gets an id but NO label.\n"
            "2. Build real shape, not a stick figure. Use 20-40 primitives for a detailed "
            "subject, grouped into logical assemblies via parent. Overlap and inset parts so "
            "they read as one solid object rather than floating blocks.\n"
            "3. Proportions from life. Work out real relative dimensions first, then scale the "
            "whole scene to a comfortable viewing size (roughly 2-6 units tall).\n"
            "4. Materials carry the read: metal metalness 0.9 / roughness 0.3, plastic 0.1/0.5, "
            "glass low opacity, anything that emits light gets an emissive colour.\n"
            "5. Vary colour between adjacent parts so silhouettes are legible. Never leave "
            "everything the default colour.\n"
            "6. Frame it: set environment.camera position and target so the whole subject fills "
            "the view, and pick a background that contrasts with the subject."
        ),
    },
}

DEFAULT_TIER = "os"


def resolve_tier(name: str) -> str:
    return name if name in TIERS else DEFAULT_TIER


async def run_agent(client, tier, goal, tool_decls, execute_tool,
                    context="", on_step=None):
    """Drive one goal to completion on the given tier.

    on_step(kind, name, payload) is called for observability:
      ("tool", name, args) before a tool runs, ("result", name, output) after.
    Returns a one-sentence outcome suitable for speaking aloud.
    """
    spec = TIERS[resolve_tier(tier)]

    prompt = goal if not context else f"{context}\n\nGoal: {goal}"
    contents = [types.Content(role="user", parts=[types.Part.from_text(text=prompt)])]

    config = types.GenerateContentConfig(
        system_instruction=spec["system"],
        tools=[types.Tool(function_declarations=tool_decls)],
        # The hub owns tool execution; the SDK must not call anything itself.
        automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
    )

    for _ in range(MAX_STEPS):
        response = await asyncio.wait_for(
            client.aio.models.generate_content(
                model=spec["model"], contents=contents, config=config
            ),
            timeout=STEP_TIMEOUT_S,
        )

        calls = response.function_calls or []
        if not calls:
            return (response.text or "").strip() or "Done."

        # Keep the model's own turn in history, then answer every call it made.
        if response.candidates and response.candidates[0].content:
            contents.append(response.candidates[0].content)

        reply_parts = []
        for call in calls:
            args = dict(call.args or {})
            if on_step:
                on_step("tool", call.name, args)
            try:
                output, _image = await execute_tool(call.name, args)
            except Exception as e:
                output = f"[Tool error] {call.name} failed: {e}"
            if on_step:
                on_step("result", call.name, output)
            reply_parts.append(types.Part.from_function_response(
                name=call.name, response={"output": str(output)}
            ))
        contents.append(types.Content(role="user", parts=reply_parts))

    return "I reached my step limit on that task before finishing it."


def summarise_goal(goal: str, limit: int = 60) -> str:
    goal = " ".join(str(goal).split())
    return goal if len(goal) <= limit else goal[: limit - 1] + "…"


# ---------- Ambient Screen Sentinel ----------
# Watches what Vince is writing and speaks up only for things a good pair would
# mention. The bar is deliberately high: a wrong nudge costs more attention than
# a missed one, so anything below CONFIDENCE_FLOOR is dropped without a word.

CONFIDENCE_FLOOR = 0.85
SENTINEL_TIMEOUT_S = 20.0

SENTINEL_INSTRUCTION = (
    "You are Ultron's Ambient Screen Sentinel. You watch over Vince's workspace like an "
    "observant peer and pair-programmer.\n"
    "RULES:\n"
    "1. ONLY flag a clear typo, glaring grammatical error, broken syntax, or a missed "
    "contextual detail (e.g. 'attached is the file' with no attachment).\n"
    "2. Ignore minor subjective style preferences, work-in-progress draft lines, half-typed "
    "words, and anything that merely reflects where the cursor happens to be.\n"
    "3. Spoken guidance must be under 8 words, natural and casual: 'Typo in the second "
    "paragraph.', 'Missing semicolon on line 42.'\n"
    "4. Always give the exact text to replace and what to replace it with, copied verbatim "
    "from what you were shown — never paraphrased, never re-indented.\n"
    "5. If nothing meets that bar, return issue_type 'none' with confidence 0. Saying nothing "
    "is the correct answer most of the time.\n"
    "WHAT COUNTS, concretely:\n"
    "- syntax: an unclosed bracket, paren, quote or block that is already settled code rather "
    "than a line still being typed; a stray comma; an obviously wrong keyword. A missing "
    "semicolon only matters in a language that needs one.\n"
    "- context: the text claims something the window does not support — 'attached is the file' "
    "or 'see enclosed' with no attachment visible, a subject line that contradicts the body, a "
    "greeting addressed to a different person than the recipient. Only raise this when you were "
    "given an image and can actually see the compose window; never infer it from text alone.\n"
    "- Do not flag the last line if it looks mid-thought. Do not flag placeholder text, lorem "
    "ipsum, commented-out code, or a search box."
)

SENTINEL_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "issue_type": {"type": "STRING", "enum": ["none", "spelling", "grammar", "syntax", "context"]},
        "confidence": {"type": "NUMBER"},
        "spoken_nudge": {"type": "STRING"},
        "original_snippet": {"type": "STRING"},
        "suggested_snippet": {"type": "STRING"},
        "explanation": {"type": "STRING"},
    },
    "required": ["issue_type", "confidence"],
}


async def evaluate_workspace(client, *, app_name, text=None, image_jpeg=None, title=None):
    """Judge one snapshot of what Vince is working on.

    Returns a hint dict worth showing, or None. Either `text` (from the
    accessibility tree) or `image_jpeg` (the cropped window) must be supplied.
    """
    if not text and not image_jpeg:
        return None

    context = f"Application: {app_name}"
    if title:
        context += f"\nWindow: {title}"

    parts = [types.Part.from_text(text=context)]
    if text:
        parts.append(types.Part.from_text(
            text="Here is the exact content of the field Vince is typing in:\n\n" + text))
    if image_jpeg:
        parts.append(types.Part.from_text(
            text="The accessibility tree gave no text, so here is the window itself:"))
        parts.append(types.Part.from_bytes(data=image_jpeg, mime_type="image/jpeg"))

    config = types.GenerateContentConfig(
        system_instruction=SENTINEL_INSTRUCTION,
        response_mime_type="application/json",
        response_schema=SENTINEL_SCHEMA,
        temperature=0.1,
        # This runs on every typing pause and the answer is usually "nothing".
        # Measured: reasoning off cuts a text pass 1.85s -> 1.43s and an image
        # pass 4.14s -> 2.15s, with no change in what it catches.
        thinking_config=types.ThinkingConfig(thinking_budget=0),
    )

    try:
        response = await asyncio.wait_for(
            client.aio.models.generate_content(
                model=SENTINEL_MODEL,
                contents=[types.Content(role="user", parts=parts)],
                config=config,
            ),
            timeout=SENTINEL_TIMEOUT_S,
        )
        hint = json.loads(response.text or "{}")
    except Exception:
        return None

    if hint.get("issue_type") in (None, "none"):
        return None
    try:
        confidence = float(hint.get("confidence", 0))
    except (TypeError, ValueError):
        return None
    if confidence < CONFIDENCE_FLOOR:
        return None
    if not hint.get("spoken_nudge"):
        return None

    # A replacement we cannot locate in the source is not actionable, and the
    # model does drift on whitespace, so verify rather than trust.
    original = hint.get("original_snippet") or ""
    if text and original and original not in text:
        hint["original_snippet"] = ""
        hint["suggested_snippet"] = ""

    hint["confidence"] = confidence
    hint["app_name"] = app_name
    return hint
