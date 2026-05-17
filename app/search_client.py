import uuid

from azure.core.credentials import AzureKeyCredential
from azure.search.documents import SearchClient
from azure.search.documents.indexes import SearchIndexClient
from azure.search.documents.indexes.models import (
    SearchableField,
    SearchFieldDataType,
    SearchIndex,
    SimpleField,
)

from .config import settings

# Text-only index. The agent's file_search tool handles vector retrieval
# from the Foundry-managed vector store; AI Search is wired so the agent's
# azure_ai_search tool can do keyword retrieval over the same documents.
_cred = AzureKeyCredential(settings.search_key)
_index_client = SearchIndexClient(endpoint=settings.search_endpoint, credential=_cred)
_search_client = SearchClient(
    endpoint=settings.search_endpoint,
    index_name=settings.search_index,
    credential=_cred,
)


def ensure_index() -> None:
    fields = [
        SimpleField(name="id", type=SearchFieldDataType.String, key=True),
        SimpleField(name="source", type=SearchFieldDataType.String, filterable=True),
        SearchableField(name="content", type=SearchFieldDataType.String),
    ]
    index = SearchIndex(name=settings.search_index, fields=fields)
    _index_client.create_or_update_index(index)


def index_text_document(filename: str, text: str) -> str:
    doc_id = uuid.uuid4().hex
    _search_client.upload_documents(
        documents=[{"id": doc_id, "source": filename, "content": text}]
    )
    return doc_id


def delete_documents_by_source(filename: str) -> int:
    """Delete every indexed document whose `source` field matches ``filename``."""
    safe = filename.replace("'", "''")
    results = _search_client.search(
        search_text="*",
        filter=f"source eq '{safe}'",
        select=["id"],
        top=1000,
    )
    ids = [r["id"] for r in results]
    if not ids:
        return 0
    _search_client.delete_documents(documents=[{"id": i} for i in ids])
    return len(ids)
