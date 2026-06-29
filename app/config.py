import os
from dataclasses import dataclass


@dataclass
class AppConfig:
    port: int
    log_level: str
    api_key: str
    worker_concurrency: int
    max_source_bytes: int


def _required(name: str) -> str:
    value = os.environ.get(name, '').strip()
    if not value:
        raise RuntimeError(f'Missing required env var: {name}')
    return value


def _optional(name: str, fallback: str) -> str:
    value = os.environ.get(name, '').strip()
    return value if value else fallback


def _optional_int(name: str, fallback: int) -> int:
    raw = os.environ.get(name, '').strip()
    if not raw:
        return fallback
    try:
        return int(raw)
    except ValueError:
        return fallback


def load_config() -> AppConfig:
    return AppConfig(
        port=_optional_int('PORT', 8080),
        log_level=_optional('LOG_LEVEL', 'info'),
        api_key=_required('PDF_SERVICE_API_KEY'),
        worker_concurrency=_optional_int('WORKER_CONCURRENCY', 2),
        max_source_bytes=_optional_int('MAX_SOURCE_BYTES', 100 * 1024 * 1024),
    )
