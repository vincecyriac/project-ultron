"""
Background agent tier for Project FRIDAY.

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

MAX_STEPS = 12          # tool round-trips before the agent must conclude
STEP_TIMEOUT_S = 120.0  # per model call

_SHARED_RULES = (
    "You are a background execution agent for FRIDAY, operating on Vince's macOS machine. "
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
