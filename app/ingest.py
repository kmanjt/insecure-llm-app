from .blob_client import upload_blob
from .foundry_client import upload_to_vector_store
from .search_client import index_text_document

_TEXTUAL_EXTS = {".txt", ".md", ".csv", ".log", ".json", ".yaml", ".yml"}


def _is_textual(filename: str) -> bool:
    name = filename.lower()
    return any(name.endswith(ext) for ext in _TEXTUAL_EXTS)


def ingest_document(filename: str, content: bytes) -> dict:
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
