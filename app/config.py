import os


class Settings:
    basic_auth_username: str = os.environ.get("BASIC_AUTH_USERNAME", "demo")
    basic_auth_password: str = os.environ.get("BASIC_AUTH_PASSWORD", "change-me")
    max_upload_bytes: int = int(os.environ.get("MAX_UPLOAD_BYTES", "10485760"))

    storage_account: str = os.environ.get("AZURE_STORAGE_ACCOUNT", "")
    storage_container: str = os.environ.get("AZURE_STORAGE_CONTAINER", "documents")

    search_endpoint: str = os.environ.get("AZURE_SEARCH_ENDPOINT", "")
    search_index: str = os.environ.get("AZURE_SEARCH_INDEX", "documents")
    search_key: str = os.environ.get("AZURE_SEARCH_KEY", "")

    project_conn_str: str = os.environ.get("AZURE_AI_PROJECT_CONNECTION_STRING", "")
    agent_model: str = os.environ.get("AZURE_AI_AGENT_MODEL", "gpt-4o-mini")


settings = Settings()
