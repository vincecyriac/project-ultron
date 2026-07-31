"""
sentry_local.py - Local model backend for Project Ultron via LM Studio.

Talks to LM Studio's OpenAI-compatible server (default http://127.0.0.1:1234/v1).
Local models handle TEXT chat with full tool access (shell, GUI automation,
web fetch, memory, recognition); realtime voice remains a Gemini Live feature.

Tool declarations are converted from the Gemini format so the hub keeps a
single source of truth. Screenshots/webcam frames returned by tools are
attached as data-URI images for vision-capable local models, with a graceful
text-only fallback when the loaded model rejects images.
"""

import json
import base64
import aiohttp

MAX_HISTORY_MESSAGES = 30
MAX_TOOL_RESULT_CHARS = 6000

LOCAL_MODE_NOTE = (
    "\n\nNOTE: You are currently running as a LOCAL model (text chat mode). "
    "You cannot hear audio or speak; respond in text. All tools still work, "
    "including looking at the screen/webcam (images are attached to the chat), "
    "GUI control, shell commands, web fetching, and memory. The google_search "
    "tool is NOT available; use fetch_webpage with a search engine URL if needed."
)


def _convert_schema(schema):
    """Gemini parameter schema (UPPERCASE types) -> OpenAI JSON schema."""
    if not isinstance(schema, dict):
        return schema
    out = {}
    for k, v in schema.items():
        if k == "type" and isinstance(v, str):
            out[k] = v.lower()
        elif k == "properties" and isinstance(v, dict):
            out[k] = {pk: _convert_schema(pv) for pk, pv in v.items()}
        elif k == "items":
            out[k] = _convert_schema(v)
        else:
            out[k] = v
    if out.get("type") == "object" and "properties" not in out:
        out["properties"] = {}
    return out


def gemini_decls_to_openai_tools(decls: list) -> list:
    tools = []
    for d in decls:
        tools.append({
            "type": "function",
            "function": {
                "name": d["name"],
                "description": d.get("description", ""),
                "parameters": _convert_schema(d.get("parameters", {"type": "OBJECT", "properties": {}})),
            }
        })
    return tools


def _strip_images(messages: list) -> list:
    """Replaces image content parts with text placeholders (non-vision models)."""
    cleaned = []
    for m in messages:
        content = m.get("content")
        if isinstance(content, list):
            texts = [p.get("text", "") for p in content if p.get("type") == "text"]
            texts.append("[Image was captured but this model cannot view images. "
                         "Use read_ui_elements or shell commands to inspect instead.]")
            m = {**m, "content": "\n".join(t for t in texts if t)}
        cleaned.append(m)
    return cleaned


class LocalModelClient:
    def __init__(self, base_url: str, model: str, tool_decls: list):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.tools = gemini_decls_to_openai_tools(tool_decls)
        self.history = []
        self.vision_ok = True  # optimistic; flipped off on first image rejection

    def _trim(self):
        if len(self.history) > MAX_HISTORY_MESSAGES:
            self.history = self.history[-MAX_HISTORY_MESSAGES:]
            # never start history on a dangling tool response
            while self.history and self.history[0].get("role") == "tool":
                self.history.pop(0)

    async def _complete(self, http, messages):
        payload = {
            "model": self.model,
            "messages": messages,
            "tools": self.tools,
            "temperature": 0.7,
        }
        async with http.post(f"{self.base_url}/chat/completions", json=payload) as resp:
            body = await resp.text()
            if resp.status != 200:
                raise RuntimeError(f"LM Studio HTTP {resp.status}: {body[:300]}")
            return json.loads(body)

    async def chat(self, user_text: str, system_prompt: str, execute_tool,
                   on_tool=None, on_tool_done=None, max_steps: int = 8) -> str:
        """Runs one user turn through the local model, executing tools until a
        final text answer. execute_tool is the hub's async dispatcher."""
        self.history.append({"role": "user", "content": user_text})
        self._trim()
        messages = [{"role": "system", "content": system_prompt + LOCAL_MODE_NOTE}] + list(self.history)

        timeout = aiohttp.ClientTimeout(total=600, sock_connect=10)
        async with aiohttp.ClientSession(timeout=timeout) as http:
            for _ in range(max_steps):
                try:
                    data = await self._complete(http, messages)
                except RuntimeError as e:
                    # Vision fallback: model rejected image content
                    if "400" in str(e) and any(isinstance(m.get("content"), list) for m in messages):
                        self.vision_ok = False
                        messages = _strip_images(messages)
                        self.history = _strip_images(self.history)
                        data = await self._complete(http, messages)
                    else:
                        raise

                msg = data["choices"][0]["message"]
                tool_calls = msg.get("tool_calls")

                if not tool_calls:
                    text = msg.get("content") or ""
                    self.history.append({"role": "assistant", "content": text})
                    self._trim()
                    return text

                assistant_msg = {"role": "assistant", "content": msg.get("content") or "",
                                 "tool_calls": tool_calls}
                messages.append(assistant_msg)
                self.history.append(assistant_msg)

                for tc in tool_calls:
                    name = tc["function"]["name"]
                    try:
                        args = json.loads(tc["function"].get("arguments") or "{}")
                    except json.JSONDecodeError:
                        args = {}
                    if on_tool:
                        on_tool(name, args)
                    try:
                        result, image = await execute_tool(name, args)
                    except Exception as tool_err:
                        result, image = f"[Error]: Tool crashed: {tool_err}", None
                    if on_tool_done:
                        on_tool_done(name, result)

                    tool_msg = {
                        "role": "tool",
                        "tool_call_id": tc.get("id", ""),
                        "content": str(result)[:MAX_TOOL_RESULT_CHARS],
                    }
                    messages.append(tool_msg)
                    self.history.append(tool_msg)

                    if image and self.vision_ok:
                        b64 = base64.b64encode(image).decode()
                        img_msg = {
                            "role": "user",
                            "content": [
                                {"type": "text", "text": f"[Image captured by {name}:]"},
                                {"type": "image_url",
                                 "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
                            ],
                        }
                        messages.append(img_msg)
                        self.history.append(img_msg)

            return "I hit the tool-step limit for this request without reaching a final answer. Try breaking the task into smaller steps."
