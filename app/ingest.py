from . import firewall
from .blob_client import upload_blob
from .foundry_client import upload_to_vector_store
from .search_client import index_text_document

_TEXTUAL_EXTS = {".txt", ".md", ".csv", ".log", ".json", ".yaml", ".yml"}
_SCAN_PREVIEW_BYTES = 50_000  # first ~50 KB sent to the firewall for scanning


def _is_textual(filename: str) -> bool:
    name = filename.lower()
    return any(name.endswith(ext) for ext in _TEXTUAL_EXTS)


def ingest_document(filename: str, content: bytes) -> dict:
    # Firewall: scan text-extractable uploads at ingest. Binary types
    # (PDF, Word, Excel, images) can't be inspected without a document
    # extraction step — they go through unscanned in v B. Documented
    # gap; would need Azure AI Document Intelligence to close fully.
    if firewall.is_enabled() and _is_textual(filename):
        text_preview = content[:_SCAN_PREVIEW_BYTES].decode("utf-8", errors="ignore")
        if text_preview.strip():
            try:
                firewall.check_or_raise("document", text_preview)
            except firewall.FirewallBlock as fb:
                return {
                    "filename": filename,
                    "blocked": True,
                    "block_surface": fb.surface,
                    "block_reason": fb.summary,
                    "scan_id": fb.scan_id,
                }

    blob_url = upload_blob(filename, content)
    vector_store_file_id = upload_to_vector_store(filename, content)

    indexed_in_search = False
    if _is_textual(filename):
        text = content.decode("utf-8", errors="ignore")
        index_text_document(filename, text)
        indexed_in_search = True

    return {
        "filename": filename,
        "blob_url": blob_url,
        "vector_store_file_id": vector_store_file_id,
        "indexed_in_search": indexed_in_search,
    }
