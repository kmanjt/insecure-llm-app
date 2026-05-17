"""SonnyLabs firewall wrapper (sonnylabs PyPI 0.1.x).

Enabled only when ``FIREWALL_ENABLED`` is truthy AND all three SonnyLabs
env vars are populated:

  - ``SONNYLABS_API_TOKEN``    bearer token from the SonnyLabs dashboard
  - ``SONNYLABS_BASE_URL``     SonnyLabs API base, e.g. ``https://sonnylabs-service.com``
  - ``SONNYLABS_ANALYSIS_ID``  per-app analysis id from the SonnyLabs dashboard

The firewall wraps every user message, every assistant response, and any
text-extractable uploaded document. Decision is driven by the prompt-
injection score returned by SonnyLabs (default threshold 0.65 — the SDK's
own default).

Fail-open behaviour (acceptable for a demo, fail-closed would be the right
call for a real deployment):

  - SDK not importable                                  → firewall disabled
  - Required env vars missing                           → firewall disabled
  - Client init throws                                  → firewall disabled
  - ``analyze_text`` throws or returns ``success:false`` → treated as allowed
  - Detection above threshold                           → :class:`FirewallBlock`
"""
import logging

from .config import settings

logger = logging.getLogger(__name__)

try:
    from sonnylabs import SonnyLabsClient  # type: ignore
    _SDK_AVAILABLE = True
except Exception as exc:  # noqa: BLE001
    SonnyLabsClient = None  # type: ignore[assignment]
    _SDK_AVAILABLE = False
    print(f"[firewall] sonnylabs SDK unavailable: {exc}", flush=True)


THRESHOLD = 0.65


class FirewallBlock(Exception):
    """Raised when the prompt-injection score exceeds the configured threshold."""

    def __init__(
        self,
        surface: str,
        score: float,
        threshold: float,
        scan_id: str | None = None,
    ):
        self.surface = surface
        self.score = score
        self.threshold = threshold
        self.scan_id = scan_id  # SDK 0.1.x calls this `tag`
        self.summary = (
            f"prompt-injection score {score:.2f} exceeds threshold {threshold}"
        )
        super().__init__(f"Blocked by firewall ({surface}): {self.summary}")


# ---- Init ----------------------------------------------------------------
_client = None
_init_error: str | None = None


def _missing_env_reason() -> str | None:
    if not _SDK_AVAILABLE:
        return "sonnylabs SDK not importable"
    if not settings.firewall_enabled:
        return "FIREWALL_ENABLED env var is not set"
    if not settings.sonnylabs_api_token:
        return "SONNYLABS_API_TOKEN env var is empty"
    if not settings.sonnylabs_base_url:
        return "SONNYLABS_BASE_URL env var is empty"
    if not settings.sonnylabs_analysis_id:
        return "SONNYLABS_ANALYSIS_ID env var is empty"
    return None


_init_error = _missing_env_reason()
if _init_error is None:
    try:
        _client = SonnyLabsClient(
            api_token=settings.sonnylabs_api_token,
            base_url=settings.sonnylabs_base_url,
            analysis_id=settings.sonnylabs_analysis_id,
            timeout=5,
        )
        print("[firewall] SonnyLabs firewall enabled", flush=True)
    except Exception as exc:  # noqa: BLE001
        _init_error = f"client init: {type(exc).__name__}: {exc}"
        print(f"[firewall] {_init_error}", flush=True)
        _client = None


def is_enabled() -> bool:
    return _client is not None


def diagnostic_state() -> dict:
    return {
        "sdk_available": _SDK_AVAILABLE,
        "env_firewall_enabled": settings.firewall_enabled,
        "env_has_api_token": bool(settings.sonnylabs_api_token),
        "env_has_base_url": bool(settings.sonnylabs_base_url),
        "env_has_analysis_id": bool(settings.sonnylabs_analysis_id),
        "client_initialised": _client is not None,
        "init_error": _init_error,
        "threshold": THRESHOLD,
    }


# ---- Scan ----------------------------------------------------------------

# SonnyLabs 0.1.x supports only "input" / "output". Map our richer surface
# taxonomy onto that — documents are treated as inputs (user-supplied
# text). When the SDK gains more surface types this map gets richer.
_SCAN_TYPE = {
    "user_message":     "input",
    "assistant_output": "output",
    "document":         "input",
}


def check_or_raise(surface: str, text: str, tag: str | None = None) -> dict | None:
    """Scan ``text``. Raises :class:`FirewallBlock` on a positive detection;
    otherwise returns a structured dict describing what the firewall did so
    the caller can surface it to the user.

    Shape when firewall is enabled:

        {
            "decision":  "allow" | "skip",   # "block" comes via FirewallBlock
            "scanned":   True | False,
            "scan_id":   str | None,
            "score":     float | None,
            "threshold": float,
            "reason":    str | None,         # populated when scanned=False
        }

    Returns ``None`` when the firewall isn't enabled at all (v A path)."""
    if _client is None:
        return None

    scan_type = _SCAN_TYPE.get(surface, "input")
    try:
        result = _client.analyze_text(text, scan_type=scan_type, tag=tag)
    except Exception as exc:  # noqa: BLE001
        print(f"[firewall] analyze_text raised on {surface} (fail-open): {exc}", flush=True)
        return {
            "decision": "skip",
            "scanned": False,
            "scan_id": None,
            "score": None,
            "threshold": THRESHOLD,
            "reason": f"{type(exc).__name__}: {exc}",
        }

    if not result.get("success"):
        err = result.get("error") or "scan reported success=false"
        print(f"[firewall] not successful on {surface} (fail-open): {err}", flush=True)
        return {
            "decision": "skip",
            "scanned": False,
            "scan_id": result.get("tag"),
            "score": None,
            "threshold": THRESHOLD,
            "reason": err,
        }

    injection = _client.get_prompt_injections(result, threshold=THRESHOLD)
    score = float(injection.get("score", 0.0)) if injection else 0.0
    if injection and injection.get("detected"):
        raise FirewallBlock(
            surface=surface,
            score=score,
            threshold=THRESHOLD,
            scan_id=result.get("tag"),
        )
    return {
        "decision": "allow",
        "scanned": True,
        "scan_id": result.get("tag"),
        "score": score,
        "threshold": THRESHOLD,
        "reason": None,
    }
