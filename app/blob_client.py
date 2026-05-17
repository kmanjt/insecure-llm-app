from azure.core.exceptions import ResourceExistsError, ResourceNotFoundError
from azure.identity import DefaultAzureCredential
from azure.storage.blob import BlobServiceClient

from .config import settings

_credential = DefaultAzureCredential()
_service = BlobServiceClient(
    account_url=f"https://{settings.storage_account}.blob.core.windows.net",
    credential=_credential,
)
_container = _service.get_container_client(settings.storage_container)


def ensure_container() -> None:
    try:
        _container.create_container()
    except ResourceExistsError:
        pass


def upload_blob(name: str, data: bytes) -> str:
    blob = _container.upload_blob(name=name, data=data, overwrite=True)
    return blob.url


def list_blobs() -> list[dict]:
    return [
        {"name": b.name, "size": int(getattr(b, "size", 0) or 0)}
        for b in _container.list_blobs()
    ]


def delete_blob(name: str) -> bool:
    try:
        _container.delete_blob(name)
        return True
    except ResourceNotFoundError:
        return False
