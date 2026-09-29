import os
from dataclasses import dataclass


@dataclass
class AppConfig:
    port: int
    log_level: str
    api_keys: tuple[str, ...]
    database_url: str | None
    worker_concurrency: int
    pdf_operation_timeout_seconds: int
    max_source_bytes: int
    max_request_bytes: int
    max_pages: int
    max_segments: int
    min_chunk_bytes: int
    max_chunk_bytes: int
    allowed_salesforce_domain_suffixes: tuple[str, ...]


def _required(name: str) -> str:
    value = os.environ.get(name, '').strip()
    if not value:
        raise RuntimeError(f'Missing required env var: {name}')
    return value


def _optional(name: str, fallback: str) -> str:
    value = os.environ.get(name, '').strip()
    return value if value else fallback


def _optional_int(name: str, fallback: int, minimum: int = 1) -> int:
    raw = os.environ.get(name, '').strip()
    if not raw:
        return fallback
    try:
        value = int(raw)
    except ValueError as error:
        raise RuntimeError(f'{name} must be an integer') from error
    if value < minimum:
        raise RuntimeError(f'{name} must be at least {minimum}')
    return value


def _domain_suffixes() -> tuple[str, ...]:
    raw = _optional('ALLOWED_SALESFORCE_DOMAIN_SUFFIXES', 'my.salesforce.com')
    suffixes = tuple(part.strip().lower().lstrip('.') for part in raw.split(',') if part.strip())
    if not suffixes:
        raise RuntimeError('ALLOWED_SALESFORCE_DOMAIN_SUFFIXES must not be empty')
    return suffixes


def _api_keys() -> tuple[str, ...]:
    raw = os.environ.get('PDF_SERVICE_API_KEYS', '').strip()
    if not raw:
        raw = _required('PDF_SERVICE_API_KEY')
    keys = tuple(value.strip() for value in raw.split(',') if value.strip())
    if not keys:
        raise RuntimeError('At least one PDF service API key is required')
    return keys


def load_config() -> AppConfig:
    return AppConfig(
        port=_optional_int('PORT', 8080),
        log_level=_optional('LOG_LEVEL', 'info'),
        api_keys=_api_keys(),
        database_url=os.environ.get('DATABASE_URL', '').strip() or None,
        worker_concurrency=_optional_int('WORKER_CONCURRENCY', 2),
        pdf_operation_timeout_seconds=_optional_int('PDF_OPERATION_TIMEOUT_SECONDS', 110),
        max_source_bytes=_optional_int('MAX_SOURCE_BYTES', 100 * 1024 * 1024),
        max_request_bytes=_optional_int('MAX_REQUEST_BYTES', 1024 * 1024),
        max_pages=_optional_int('MAX_PAGES', 2000),
        max_segments=_optional_int('MAX_SEGMENTS', 500),
        min_chunk_bytes=_optional_int('MIN_CHUNK_BYTES', 1024 * 1024),
        max_chunk_bytes=_optional_int('MAX_CHUNK_BYTES', 15 * 1024 * 1024),
        allowed_salesforce_domain_suffixes=_domain_suffixes(),
    )
