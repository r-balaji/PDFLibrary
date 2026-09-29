import asyncio
import hmac
import logging
import secrets
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from .config import load_config
from .routes.chunks import router as chunks_router
from .routes.splits import router as splits_router
from .state_store import create_idempotency_store

config = load_config()

logging.basicConfig(
    level=config.log_level.upper(),
    format='%(asctime)s %(levelname)s %(name)s %(message)s',
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.config = config
    app.state.processing_semaphore = asyncio.Semaphore(config.worker_concurrency)
    app.state.idempotency_store = create_idempotency_store(config.database_url)
    if app.state.idempotency_store:
        await app.state.idempotency_store.start()
    try:
        yield
    finally:
        if app.state.idempotency_store:
            await app.state.idempotency_store.close()


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)


@app.exception_handler(RequestValidationError)
async def request_validation_error(request: Request, _error: RequestValidationError):
    # Do not reflect field inputs because a rejected value could be a token.
    request_id = getattr(request.state, 'request_id', secrets.token_hex(8))
    return _json_response(422, {'detail': 'Request validation failed'}, request_id)


@app.middleware('http')
async def auth_middleware(request: Request, call_next):
    request_id = secrets.token_hex(8)
    request.state.request_id = request_id
    started_at = time.perf_counter()

    def finish(response):
        response.headers['Cache-Control'] = 'no-store'
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['X-Request-ID'] = request_id
        logging.getLogger('request').info(
            'requestComplete requestId=%s cfRay=%s method=%s path=%s status=%s durationMs=%s',
            request_id,
            request.headers.get('cf-ray', '-'),
            request.method,
            request.url.path,
            response.status_code,
            round((time.perf_counter() - started_at) * 1000),
        )
        return response

    if request.url.path in ('/healthz', '/readyz', '/'):
        return finish(await call_next(request))
    supplied = request.headers.get('authorization', '')
    key_matches = [
        hmac.compare_digest(supplied, f'Bearer {api_key}')
        for api_key in config.api_keys
    ]
    if not any(key_matches):
        return finish(_json_response(401, {'error': 'Unauthorized'}, request_id))
    content_type = request.headers.get('content-type', '').split(';', 1)[0].strip().lower()
    if request.method in ('POST', 'PUT', 'PATCH') and content_type != 'application/json':
        return finish(_json_response(415, {'error': 'Content-Type must be application/json'}, request_id))
    content_length = request.headers.get('content-length')
    if request.method in ('POST', 'PUT', 'PATCH') and content_length is None:
        return finish(_json_response(411, {'error': 'Content-Length is required'}, request_id))
    if content_length is not None:
        try:
            declared_size = int(content_length)
        except ValueError:
            return finish(_json_response(400, {'error': 'Invalid Content-Length'}, request_id))
        if declared_size < 0 or declared_size > config.max_request_bytes:
            return finish(_json_response(413, {'error': 'Request body is too large'}, request_id))

    response = await call_next(request)
    return finish(response)


def _json_response(
    status_code: int,
    content: dict,
    request_id: str,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    response = JSONResponse(
        status_code=status_code,
        content=content,
        headers=headers,
    )
    response.headers['Cache-Control'] = 'no-store'
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['X-Request-ID'] = request_id
    return response


@app.get('/healthz')
async def healthz():
    return {'ok': True, 'name': 'pdf-lib-service', 'version': '1.0.0'}


@app.get('/readyz')
async def readyz(request: Request):
    store = request.app.state.idempotency_store
    if store is None or await store.ready():
        return {'ok': True}
    return JSONResponse(status_code=503, content={'ok': False})


@app.get('/')
async def root():
    return {'ok': True, 'name': 'pdf-lib-service'}


app.include_router(chunks_router)
app.include_router(splits_router)
