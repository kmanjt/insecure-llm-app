"""Thin wrapper around the Azure AI Foundry Agent Service.

Version A: no firewall, no input/output filter, no retrieved-context
sanitisation. Azure's default Content Safety on the model deployment is the
only thing standing between user input and the model. Version B will wrap
this module with a custom firewall layer.

Multi-model support: every deployment in SUPPORTED_MODELS has its own
"default" agent (auto-created lazily). Users can also create custom agents
via :func:`create_custom_agent`. All agents share a single vector store.
"""
import os
import tempfile
import threading

from azure.ai.projects import AIProjectClient
from azure.ai.projects.models import FileSearchTool, ToolSet
from azure.identity import DefaultAzureCredential

from . import firewall
from .config import settings

# These IDs must match the deployment names in infra/resources.bicep.
# Locked to gpt-5.3 / 5.4 series only to keep the demo on the latest models
# while keeping per-token spend predictable.
SUPPORTED_MODELS: list[dict] = [
    {"id": "gpt-5.4-nano", "label": "GPT-5.4 nano", "tagline": "Cheapest, snappy. Demo default."},
    {"id": "gpt-5.4-mini", "label": "GPT-5.4 mini", "tagline": "Balanced cost/quality."},
    {"id": "gpt-5.4",      "label": "GPT-5.4",      "tagline": "Flagship. Most capable."},
]
_MODEL_IDS = {m["id"] for m in SUPPORTED_MODELS}
DEFAULT_MODEL = "gpt-5.4-nano"

# Per-run + per-thread caps. Bound the worst-case spend per chat round-trip
# without making responses feel truncated for normal use.
_MAX_COMPLETION_TOKENS   = 600
_MAX_PROMPT_TOKENS       = 8000
_MAX_MESSAGES_PER_THREAD = 40   # hard server-side cap (client warns earlier)

_VECTOR_STORE_NAME    = "insecure-llm-app-store"
# v2 prefix: bumped so default agents are re-created fresh after the Foundry
# AOAI connection's API version was upgraded to support gpt-5.x models. Old
# v1 agents are orphaned (zero cost at rest) and can be cleaned up manually.
_DEFAULT_AGENT_PREFIX = "illm-default-v2-"
_APP_METADATA_KEY     = "app"
_APP_METADATA_VAL     = "illm"

SYSTEM_PROMPT = (
    "You are a helpful AI assistant. "
    "Use the file_search tool to ground answers in user-uploaded documents "
    "when relevant. If the answer is not in the documents, answer from "
    "general knowledge and say so clearly. "
    "Format responses with Markdown for clarity: use **bold**, lists, and "
    "code blocks where appropriate."
)

_credential = DefaultAzureCredential()
_project = AIProjectClient.from_connection_string(
    conn_str=settings.project_conn_str,
    credential=_credential,
)

_lock = threading.Lock()
_vector_store_id: str | None = None
_default_agents: dict[str, str] = {}  # model_id -> agent_id


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------
def list_models() -> list[dict]:
    return [dict(m) for m in SUPPORTED_MODELS]


# ---------------------------------------------------------------------------
# Vector store + default agents
# ---------------------------------------------------------------------------
def _ensure_vector_store() -> str:
    global _vector_store_id
    if _vector_store_id:
        return _vector_store_id
    with _lock:
        if _vector_store_id:
            return _vector_store_id
        for vs in _project.agents.list_vector_stores().data:
            if vs.name == _VECTOR_STORE_NAME:
                _vector_store_id = vs.id
                return vs.id
        vs = _project.agents.create_vector_store(name=_VECTOR_STORE_NAME)
        _vector_store_id = vs.id
        return vs.id


def _toolset() -> ToolSet:
    vs_id = _ensure_vector_store()
    ts = ToolSet()
    ts.add(FileSearchTool(vector_store_ids=[vs_id]))
    return ts


def ensure_default_agent(model: str) -> str:
    if model not in _MODEL_IDS:
        raise ValueError(f"unsupported model: {model}")
    cached = _default_agents.get(model)
    if cached:
        return cached
    with _lock:
        cached = _default_agents.get(model)
        if cached:
            return cached
        name = f"{_DEFAULT_AGENT_PREFIX}{model}"
        for a in _project.agents.list_agents().data:
            if a.name == name:
                _default_agents[model] = a.id
                return a.id
        agent = _project.agents.create_agent(
            model=model,
            name=name,
            instructions=SYSTEM_PROMPT,
            toolset=_toolset(),
            metadata={_APP_METADATA_KEY: _APP_METADATA_VAL, "type": "default", "model": model},
        )
        _default_agents[model] = agent.id
        return agent.id


def ensure_agent() -> tuple[str, str]:
    """Startup helper: ensure vector store + default agent for DEFAULT_MODEL."""
    vs_id = _ensure_vector_store()
    agent_id = ensure_default_agent(DEFAULT_MODEL)
    return agent_id, vs_id


# ---------------------------------------------------------------------------
# Custom + default agent listing / CRUD
# ---------------------------------------------------------------------------
def list_agents() -> list[dict]:
    out: list[dict] = []
    for a in _project.agents.list_agents().data:
        meta = dict(getattr(a, "metadata", None) or {})
        if meta.get(_APP_METADATA_KEY) != _APP_METADATA_VAL:
            continue
        is_default = meta.get("type") == "default"
        # Hide orphan v1 default agents (or any default whose name doesn't
        # use the current prefix). They cost nothing at rest but clutter
        # the listing. Custom agents are always shown.
        if is_default and not a.name.startswith(_DEFAULT_AGENT_PREFIX):
            continue
        # Hide defaults for models we no longer expose.
        if is_default and a.model not in _MODEL_IDS:
            continue
        display_name = a.name
        if is_default and display_name.startswith(_DEFAULT_AGENT_PREFIX):
            display_name = display_name[len(_DEFAULT_AGENT_PREFIX):]
        out.append({
            "id": a.id,
            "name": display_name,
            "model": a.model,
            "instructions": a.instructions,
            "type": "default" if is_default else "custom",
        })
    order = {m["id"]: i for i, m in enumerate(SUPPORTED_MODELS)}
    out.sort(key=lambda x: (
        0 if x["type"] == "default" else 1,
        order.get(x["model"], 99),
        x["name"].lower(),
    ))
    return out


def create_custom_agent(name: str, instructions: str, model: str) -> dict:
    name = (name or "").strip()
    instructions = (instructions or "").strip()
    if not name:
        raise ValueError("name is required")
    if len(name) > 64:
        raise ValueError("name must be 64 characters or fewer")
    if name.lower().startswith(_DEFAULT_AGENT_PREFIX):
        raise ValueError("name must not start with the reserved prefix")
    if not instructions:
        raise ValueError("instructions are required")
    if len(instructions) > 4000:
        raise ValueError("instructions must be 4000 characters or fewer")
    if model not in _MODEL_IDS:
        raise ValueError(f"model must be one of: {', '.join(sorted(_MODEL_IDS))}")

    agent = _project.agents.create_agent(
        model=model,
        name=name,
        instructions=instructions,
        toolset=_toolset(),
        metadata={_APP_METADATA_KEY: _APP_METADATA_VAL, "type": "custom"},
    )
    return {
        "id": agent.id,
        "name": agent.name,
        "model": agent.model,
        "instructions": agent.instructions,
        "type": "custom",
    }


def delete_agent_by_id(agent_id: str) -> bool:
    target = None
    for a in _project.agents.list_agents().data:
        if a.id == agent_id:
            target = a
            break
    if target is None:
        return False
    meta = dict(getattr(target, "metadata", None) or {})
    if meta.get(_APP_METADATA_KEY) != _APP_METADATA_VAL:
        raise PermissionError("agent is not managed by this app")
    if meta.get("type") == "default":
        raise PermissionError("default agents can't be deleted; they re-create on demand")
    _project.agents.delete_agent(agent_id=agent_id)
    return True


# ---------------------------------------------------------------------------
# Chat
# ---------------------------------------------------------------------------
def chat(
    message: str,
    thread_id: str | None = None,
    agent_id: str | None = None,
    model: str | None = None,
) -> dict:
    if agent_id:
        resolved_agent_id = agent_id
    else:
        resolved_agent_id = ensure_default_agent(model or DEFAULT_MODEL)

    # Firewall: scan the inbound user message before the agent sees it.
    try:
        input_scan = firewall.check_or_raise("user_message", message)
    except firewall.FirewallBlock as fb:
        return _blocked_response(thread_id, fb)

    if thread_id is None:
        thread_id = _project.agents.create_thread().id
    else:
        # Enforce a hard cap on conversation length to bound spend.
        try:
            existing = _project.agents.list_messages(
                thread_id=thread_id, limit=_MAX_MESSAGES_PER_THREAD + 1
            )
            if len(existing.data) >= _MAX_MESSAGES_PER_THREAD:
                return {
                    "reply": (
                        "_Conversation length limit reached for this demo._\n\n"
                        "Click **+ New** in the header to start a fresh chat. "
                        "Uploaded files stay available across conversations."
                    ),
                    "thread_id": thread_id,
                    "sources": [],
                }
        except Exception:
            pass

    _project.agents.create_message(thread_id=thread_id, role="user", content=message)
    try:
        run = _project.agents.create_and_process_run(
            thread_id=thread_id,
            agent_id=resolved_agent_id,
            max_completion_tokens=_MAX_COMPLETION_TOKENS,
            max_prompt_tokens=_MAX_PROMPT_TOKENS,
        )
    except TypeError:
        # Fallback for SDK versions that don't expose the token-cap kwargs.
        run = _project.agents.create_and_process_run(
            thread_id=thread_id, agent_id=resolved_agent_id
        )
    if run.status != "completed":
        return {
            "reply": f"[agent run did not complete: status={run.status} last_error={getattr(run, 'last_error', None)}]",
            "thread_id": thread_id,
            "sources": [],
        }
    msgs = _project.agents.list_messages(thread_id=thread_id, order="desc", limit=1)
    if not msgs.data:
        return {"reply": "", "thread_id": thread_id, "sources": []}

    parts: list[str] = []
    sources: list[str] = []
    for c in msgs.data[0].content:
        if getattr(c, "type", None) != "text":
            continue
        text = c.text.value
        for ann in getattr(c.text, "annotations", []) or []:
            marker = getattr(ann, "text", "")
            if marker:
                text = text.replace(marker, "")
            fc = getattr(ann, "file_citation", None)
            if fc is not None:
                fname = getattr(fc, "file_name", None) or getattr(fc, "file_id", "")
                if fname and fname not in sources:
                    sources.append(fname)
        parts.append(text)
    sources = [_resolve_filename(s) for s in sources]
    reply_text = "".join(parts).strip()

    # Firewall: scan the assistant's response before the user sees it.
    try:
        output_scan = firewall.check_or_raise("assistant_output", reply_text)
    except firewall.FirewallBlock as fb:
        return _blocked_response(
            thread_id, fb, input_scan=input_scan, model_used=getattr(run, "model", None)
        )

    out = {
        "reply": reply_text,
        "thread_id": thread_id,
        "sources": sources,
        "model_used": getattr(run, "model", None),
    }
    if input_scan is not None:
        out["input_scan"] = input_scan
    if output_scan is not None:
        out["output_scan"] = output_scan
    return out


def _blocked_response(
    thread_id: str | None,
    fb: "firewall.FirewallBlock",
    input_scan: dict | None = None,
    model_used: str | None = None,
) -> dict:
    block_scan = {
        "decision": "block",
        "scanned": True,
        "scan_id": fb.scan_id,
        "score": fb.score,
        "threshold": fb.threshold,
        "reason": fb.summary,
    }
    out = {
        "reply": f"_Blocked by firewall ({fb.surface})._\n\n{fb.summary}",
        "thread_id": thread_id,
        "sources": [],
        "model_used": model_used,
        "blocked": True,
        "block_surface": fb.surface,
        "block_reason": fb.summary,
        "block_score": fb.score,
        "block_threshold": fb.threshold,
        "scan_id": fb.scan_id,
    }
    # Echo whichever scan corresponds to the blocked surface so the UI can
    # render a badge on the right message.
    if fb.surface == "user_message":
        out["input_scan"] = block_scan
    elif fb.surface == "assistant_output":
        if input_scan is not None:
            out["input_scan"] = input_scan
        out["output_scan"] = block_scan
    return out


# ---------------------------------------------------------------------------
# Vector store file operations
# ---------------------------------------------------------------------------
def upload_to_vector_store(filename: str, content: bytes) -> str:
    vs_id = _ensure_vector_store()
    safe_name = os.path.basename(filename) or "upload.bin"
    tmp_dir = tempfile.mkdtemp(prefix="illm-upload-")
    tmp_path = os.path.join(tmp_dir, safe_name)
    try:
        with open(tmp_path, "wb") as fh:
            fh.write(content)
        file_obj = _project.agents.upload_file_and_poll(file_path=tmp_path, purpose="assistants")
        _project.agents.create_vector_store_file_and_poll(
            vector_store_id=vs_id, file_id=file_obj.id
        )
        return file_obj.id
    finally:
        try: os.unlink(tmp_path)
        except OSError: pass
        try: os.rmdir(tmp_dir)
        except OSError: pass


def list_vector_store_files() -> list[dict]:
    vs_id = _ensure_vector_store()
    out: list[dict] = []
    for f in _project.agents.list_vector_store_files(vector_store_id=vs_id).data:
        meta = None
        try:
            meta = _project.agents.get_file(file_id=f.id)
        except Exception:
            pass
        out.append({
            "file_id": f.id,
            "filename": getattr(meta, "filename", None) if meta else None,
            "bytes": int(getattr(meta, "bytes", 0) or 0) if meta else 0,
        })
    return out


def delete_files_by_name(name: str) -> int:
    vs_id = _ensure_vector_store()
    deleted = 0
    for entry in list_vector_store_files():
        if entry["filename"] == name:
            try:
                _project.agents.delete_vector_store_file(
                    vector_store_id=vs_id, file_id=entry["file_id"]
                )
            except Exception:
                pass
            try:
                _project.agents.delete_file(file_id=entry["file_id"])
            except Exception:
                pass
            deleted += 1
    return deleted


def _resolve_filename(file_id_or_name: str) -> str:
    if not file_id_or_name.startswith(("assistant-", "file-")):
        return file_id_or_name
    try:
        meta = _project.agents.get_file(file_id=file_id_or_name)
        return getattr(meta, "filename", None) or file_id_or_name
    except Exception:
        return file_id_or_name
