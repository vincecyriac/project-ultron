"""asset_generator.py - Text-to-3D asset generation via Tripo3D, for Project FRIDAY.

Tripo3D turns a prompt into a single textured .glb. A generation takes tens of
seconds, so nothing here may ever be awaited inline on the Live audio path —
friday_hub runs it as a background task and patches the card when it lands, the
same shape as the widget generator.
"""

import os
import asyncio

# pyrefly: ignore [missing-import]
import httpx

# Resolved lazily rather than bound at import: friday_hub imports its modules
# before it calls load_dotenv(), so reading the key here at import time would
# always see None. Module-level value stays as an override hook for tests.
TRIPO_API_KEY = None
BASE_URL = "https://api.tripo3d.ai/v2/openapi"


def _api_key() -> str:
    return TRIPO_API_KEY or os.getenv("TRIPO_API_KEY") or ""

POLL_INTERVAL_S = 2.0
POLL_ATTEMPTS = 45              # 45 x 2s = 90s ceiling
REQUEST_TIMEOUT_S = 60.0

# Terminal states Tripo can report. Anything else means "still working".
FAILED_STATES = {"failed", "cancelled", "banned", "expired", "unknown"}

# Preferred first: the PBR model carries the textures. Some task types only
# return a bare mesh, so fall back rather than throwing away a usable result.
MODEL_KEYS = ("pbr_model", "model", "base_model")


def _extract_model_url(output) -> str:
    """Pull the .glb URL out of a finished task's output block."""
    if not isinstance(output, dict):
        return ""
    for key in MODEL_KEYS:
        value = output.get(key)
        if isinstance(value, str) and value.startswith(("http://", "https://")):
            return value
        # Newer responses nest the url one level down: {"model": {"url": ...}}
        if isinstance(value, dict):
            url = value.get("url")
            if isinstance(url, str) and url.startswith(("http://", "https://")):
                return url
    return ""


async def generate_mesh_asset(prompt: str, on_progress=None) -> str:
    """Dispatch a text-to-3D task to Tripo3D and resolve the .glb URL.

    on_progress, if given, is called with an int 0-100 as the task advances so
    the HUD card can show something better than an indefinite shimmer.
    """
    api_key = _api_key()
    if not api_key:
        raise ValueError(
            "TRIPO_API_KEY is not set. Add it to .env and restart FRIDAY.")

    prompt = (prompt or "").strip()
    if not prompt:
        raise ValueError("A prompt is required to generate a 3D asset.")

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT_S) as client:
        # --- Step 1: create the generation task --------------------------
        init_res = await client.post(
            f"{BASE_URL}/task",
            headers=headers,
            json={"type": "text_to_model", "prompt": prompt},
        )
        init_res.raise_for_status()
        init_body = init_res.json()
        # Tripo answers 200 with a non-zero code on rejection, so status alone
        # is not enough to know the task was accepted.
        if init_body.get("code") not in (0, None):
            raise RuntimeError(
                f"Tripo3D rejected the request: {init_body.get('message') or init_body.get('code')}")

        task_id = (init_body.get("data") or {}).get("task_id")
        if not task_id:
            raise RuntimeError("Tripo3D did not return a task_id.")

        # --- Step 2: poll until it resolves -------------------------------
        for _ in range(POLL_ATTEMPTS):
            await asyncio.sleep(POLL_INTERVAL_S)

            check_res = await client.get(f"{BASE_URL}/task/{task_id}", headers=headers)
            check_res.raise_for_status()
            payload = check_res.json().get("data") or {}

            status = str(payload.get("status", "")).lower()

            if on_progress is not None:
                try:
                    on_progress(int(payload.get("progress") or 0))
                except Exception:
                    pass                      # progress is cosmetic, never fatal

            if status == "success":
                url = _extract_model_url(payload.get("output"))
                if not url:
                    raise RuntimeError(
                        "Tripo3D reported success but returned no model URL.")
                return url

            if status in FAILED_STATES:
                reason = payload.get("error") or payload.get("message") or status
                raise RuntimeError(f"Tripo3D task {status}: {reason}")

    raise TimeoutError(
        f"3D model generation timed out after "
        f"{int(POLL_INTERVAL_S * POLL_ATTEMPTS)} seconds.")
