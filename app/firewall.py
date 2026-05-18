"""SonnyLabs firewall wrapper (sonnylabs-sdk PyPI 0.2.x).

Enabled only when ``FIREWALL_ENABLED`` is truthy AND ``SONNYLABS_API_KEY``
is set. The SDK has a baked-in default ``base_url``
(``https://api.sonnylabs.ai``); only override via ``SONNYLABS_BASE_URL`` for
self-hosted deployments. No ``analysis_id`` is needed — the modern SDK
scopes per agent via the optional ``context.agent_id``.

The firewall wraps every user message, every assistant response, and any
text-extractable uploaded document. Decision comes from
``scan["decision"]["action"]``: ``allowed``, ``warned``, ``flagged``,
or ``blocked``. Only ``blocked`` short-circuits the request.

Fail-open behaviour (acceptable for a demo, fail-closed would be the right
call for a real deployment):

  - SDK not importable                              → firewall disabled
  - ``SonnyLabsClient`` init throws                 → firewall disabled
  - ``create_scan`` raises (network, auth, 5xx)     → treated as allowed
  - Decision ``warned`` / ``flagged``               → logged, passed through
  - Decision ``blocked``                            → :class:`FirewallBlock`
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


_AGENT_ID = "insecure-llm-app-b"


class FirewallBlock(Exception):
    """Raised when SonnyLabs returns ``decision.action == "blocked"``."""

    def __init__(self, surface: str, summary: str, scan_id: str | None = None):
        self.surface = surface
        self.summary = summary
        self.scan_id = scan_id
        super().__init__(f"Blocked by firewall ({surface}): {summary}")


# ---- Init ----------------------------------------------------------------
_client = None
_init_error: str | None = None


def _missing_env_reason() -> str | None:
    if not _SDK_AVAILABLE:
        return "sonnylabs SDK not importable"
    if not settings.firewall_enabled:
        return "FIREWALL_ENABLED env var is not set"
    if not settings.sonnylabs_api_key:
        return "SONNYLABS_API_KEY env var is empty"
    return None


_init_error = _missing_env_reason()
if _init_error is None:
    try:
        kwargs = {"api_key": settings.sonnylabs_api_key}
        if settings.sonnylabs_base_url:
            kwargs["base_url"] = settings.sonnylabs_base_url
        _client = SonnyLabsClient(**kwargs)
        print(
            f"[firewall] SonnyLabs firewall enabled "
            f"(base_url={settings.sonnylabs_base_url or 'default'})",
            flush=True,
        )
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
        "env_has_api_key": bool(settings.sonnylabs_api_key),
        "env_has_base_url_override": bool(settings.sonnylabs_base_url),
        "client_initialised": _client is not None,
        "init_error": _init_error,
    }


# ---- Scan ----------------------------------------------------------------
def check_or_raise(
    surface: str,
    text: str,
    session_id: str | None = None,
) -> dict | None:
    """Scan ``text`` and raise :class:`FirewallBlock` on a blocked decision.
    Returns a structured dict for non-blocked outcomes so the caller can
    surface scan state in the UI. Returns ``None`` when the firewall isn't
    enabled at all (v A behaviour).

    Dict shape on enabled paths:

        {
            "decision":  "allow" | "skip",   # "block" comes via FirewallBlock
            "scanned":   bool,
            "scan_id":   str | None,
            "action":    "allowed" | "warned" | "flagged" | None,
            "reason":    str | None,
        }
    """
    if _client is None:
        return None

    context: dict[str, str] = {"agent_id": _AGENT_ID}
    if session_id:
        context["session_id"] = session_id

    try:
        # capture=true asks SonnyLabs to retain the raw content for the
        # configured retention window so each scan is replayable in the
        # dashboard. Privacy trade-off: enabling capture means user
        # messages and document snippets are stored at SonnyLabs. Fine for
        # this research demo; flip to false (or expose as an env var) for
        # a real deployment where retention matters.
        result = _client.create_scan(
            surface=surface,
            content={"type": "text", "text": text},
            context=context,
            options={"capture": True},
        )
    except Exception as exc:  # noqa: BLE001
        print(
            f"[firewall] create_scan raised on {surface} (fail-open): {exc}",
            flush=True,
        )
        return {
            "decision": "skip",
            "scanned": False,
            "scan_id": None,
            "action": None,
            "reason": f"{type(exc).__name__}: {exc}",
        }

    decision = (result.get("decision") or {})
    action = decision.get("action")
    scan_id = result.get("id")
    summary = (
        decision.get("summary")
        or decision.get("reason")
        or decision.get("findings_summary")
    )

    if action == "blocked":
        raise FirewallBlock(
            surface=surface,
            summary=summary or "blocked by policy",
            scan_id=scan_id,
        )

    if action in ("warned", "flagged"):
        print(
            f"[firewall] {action} on {surface} (scan_id={scan_id}, summary={summary})",
            flush=True,
        )

    return {
        "decision": "allow",
        "scanned": True,
        "scan_id": scan_id,
        "action": action,
        "reason": summary,
    }
