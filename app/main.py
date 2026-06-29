import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from .config import load_config
from .routes.chunks import router as chunks_router
from .routes.splits import router as splits_router

config = load_config()

logging.basicConfig(
    level=config.log_level.upper(),
    format='%(asctime)s %(levelname)s %(name)s %(message)s',
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.config = config
    app.state.pdf_semaphore = asyncio.Semaphore(max(1, config.worker_concurrency))
    yield


app = FastAPI(lifespan=lifespan)


@app.middleware('http')
async def auth_middleware(request: Request, call_next):
    if request.url.path in ('/healthz', '/'):
        return await call_next(request)
    if request.headers.get('authorization', '') != f'Bearer {config.api_key}':
        return JSONResponse(status_code=401, content={'error': 'Unauthorized'})
    return await call_next(request)


@app.get('/healthz')
async def healthz():
    return {'ok': True, 'name': 'pdf-lib-service', 'version': '0.1.0'}


@app.get('/')
async def root():
    return {'name': 'pdf-lib-service', 'endpoints': ['/v1/chunks', '/v1/splits', '/healthz']}


app.include_router(chunks_router)
app.include_router(splits_router)
