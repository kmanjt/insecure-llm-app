from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import firewall
from .blob_client import delete_blob, ensure_container, list_blobs
from .chat import answer
from .config import settings
from .foundry_client import (
    create_custom_agent,
    delete_agent_by_id,
    delete_files_by_name,
    ensure_agent,
    list_agents,
    list_models,
    list_vector_store_files,
)
from .ingest import ingest_document
from .middleware import BasicAuthMiddleware, MaxBodySizeMiddleware
from .search_client import delete_documents_by_source, ensure_index


@asynccontextmanager
async def lifespan(app: FastAPI):
    for fn in (ensure_container, ensure_index, ensure_agent):
        try:
            fn()
        except Exception as exc:  # noqa: BLE001
            print(f"startup init failed ({fn.__name__}):", exc)
    yield


app = FastAPI(title="insecure-llm-app (version A)", lifespan=lifespan)
app.add_middleware(BasicAuthMiddleware)
app.add_middleware(MaxBodySizeMiddleware)


class ChatRequest(BaseModel):
    message: str
    thread_id: str | None = None
    agent_id: str | None = None
    model: str | None = None


class CreateAgentRequest(BaseModel):
    name: str
    instructions: str
    model: str


@app.get("/health")
def health():
    fw = firewall.is_enabled()
    return {
        "ok": True,
        "version": "B" if fw else "A",
        "firewall": fw,
        "firewall_debug": firewall.diagnostic_state(),
    }


# ---------------------------------------------------------------------------
# Models + agents
# ---------------------------------------------------------------------------
@app.get("/api/models")
def models():
    return {"models": list_models()}


@app.get("/api/agents")
def agents():
    return {"agents": list_agents()}


@app.post("/api/agents")
def post_agent(req: CreateAgentRequest):
    try:
        return create_custom_agent(req.name, req.instructions, req.model)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.delete("/api/agents/{agent_id}")
def delete_agent(agent_id: str):
    try:
        ok = delete_agent_by_id(agent_id)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc))
    if not ok:
        raise HTTPException(status_code=404, detail="agent not found")
    return {"ok": True}


# ---------------------------------------------------------------------------
# Chat
# ---------------------------------------------------------------------------
@app.post("/api/chat")
def chat(req: ChatRequest):
    return answer(req.message, thread_id=req.thread_id, agent_id=req.agent_id, model=req.model)


# ---------------------------------------------------------------------------
# Upload + documents
# ---------------------------------------------------------------------------
@app.post("/api/upload")
async def upload(file: UploadFile = File(...)):
    data = await file.read()
    if len(data) > settings.max_upload_bytes:
        return JSONResponse(
            {"detail": f"file exceeds limit of {settings.max_upload_bytes} bytes"},
            status_code=413,
        )
    return ingest_document(file.filename, data)


@app.get("/api/documents")
def documents():
    blobs = {b["name"]: b for b in list_blobs()}
    vs_files = list_vector_store_files()
    by_name: dict[str, dict] = {}
    for name, b in blobs.items():
        by_name[name] = {"name": name, "size": b["size"], "in_vector_store": False, "file_id": None}
    for f in vs_files:
        name = f.get("filename")
        if not name:
            continue
        entry = by_name.setdefault(
            name, {"name": name, "size": f["bytes"], "in_vector_store": False, "file_id": None}
        )
        entry["in_vector_store"] = True
        entry["file_id"] = f["file_id"]
    return {"documents": sorted(by_name.values(), key=lambda d: d["name"].lower())}


@app.delete("/api/documents/{name:path}")
def delete_document(name: str):
    if "/" in name or "\\" in name or ".." in name:
        raise HTTPException(status_code=400, detail="invalid filename")
    blob_deleted = delete_blob(name)
    vs_deleted = delete_files_by_name(name)
    try:
        search_deleted = delete_documents_by_source(name)
    except Exception as exc:  # noqa: BLE001
        search_deleted = 0
        print("search delete failed:", exc)
    return {
        "name": name,
        "blob_deleted": blob_deleted,
        "vector_store_files_deleted": vs_deleted,
        "search_docs_deleted": search_deleted,
    }


_static_dir = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=str(_static_dir)), name="static")


@app.get("/")
def root():
    return FileResponse(str(_static_dir / "index.html"))
