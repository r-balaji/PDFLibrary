import asyncio
import logging
from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from ..pdf_ops import chunk_pdf, compute_chunks, compute_chunks_by_size, get_page_count
from ..sf_client import SalesforceContext, SalesforceFilesClient, UploadOptions

router = APIRouter()
log = logging.getLogger(__name__)


class ChunkBody(BaseModel):
    jobId: str
    sourceContentVersionId: str
    sfInstanceUrl: str
    sfAccessToken: str
    sfApiVersion: Optional[str] = '60.0'
    libraryId: Optional[str] = None
    maxChunkBytes: Optional[int] = None
    chunkSize: Optional[int] = 8
    overlap: Optional[int] = None


class ChunkOutput(BaseModel):
    chunkIndex: int
    contentDocumentId: str
    contentVersionId: str
    pageOffset: int
    pageCount: int


@router.post('/v1/chunks')
async def chunks_route(body: ChunkBody, request: Request):
    config = request.app.state.config
    job_log = log.getChild(body.jobId)

    ctx = SalesforceContext(
        instance_url=body.sfInstanceUrl,
        access_token=body.sfAccessToken,
        api_version=body.sfApiVersion or '60.0',
    )
    sf = SalesforceFilesClient(ctx, job_log)

    try:
        source_bytes = await sf.download_version_data(body.sourceContentVersionId)

        if len(source_bytes) > config.max_source_bytes:
            raise HTTPException(
                status_code=413,
                detail=f'Source file too large ({len(source_bytes)} bytes; max {config.max_source_bytes})',
            )

        async with request.app.state.pdf_semaphore:
            total_pages = await asyncio.to_thread(get_page_count, source_bytes)
        if total_pages < 1:
            raise HTTPException(status_code=422, detail='Source PDF has no pages')

        if body.maxChunkBytes is not None:
            overlap = body.overlap if body.overlap is not None else 0
            chunk_specs = compute_chunks_by_size(source_bytes, body.maxChunkBytes, overlap)
            job_log.info(
                f'computed {len(chunk_specs)} byte-sized chunks from '
                f'{total_pages} pages with maxChunkBytes={body.maxChunkBytes}'
            )
        else:
            overlap = body.overlap if body.overlap is not None else 2
            chunk_size = body.chunkSize if body.chunkSize is not None else 8
            chunk_specs = compute_chunks(total_pages, chunk_size, overlap)
            job_log.info(f'computed {len(chunk_specs)} page-sized chunks from {total_pages} pages')

        async with request.app.state.pdf_semaphore:
            chunk_bytes_list = await asyncio.to_thread(chunk_pdf, source_bytes, chunk_specs)

        outputs = []
        for spec, chunk_bytes in zip(chunk_specs, chunk_bytes_list):
            up = await sf.upload_content_version(chunk_bytes, UploadOptions(
                title=f'bundle_chunk_{spec.chunk_index}',
                file_name=f'bundle_chunk_{spec.chunk_index}.pdf',
                first_publish_location_id=body.libraryId,
            ))
            outputs.append(ChunkOutput(
                chunkIndex=spec.chunk_index,
                contentDocumentId=up.content_document_id,
                contentVersionId=up.content_version_id,
                pageOffset=spec.start_page,
                pageCount=spec.end_page - spec.start_page + 1,
            ))

        job_log.info(f'{len(outputs)} chunks uploaded')
        return {'totalPages': total_pages, 'chunks': [o.model_dump() for o in outputs]}

    except HTTPException:
        raise
    except Exception as exc:
        job_log.error(f'chunks failed: {exc}')
        raise HTTPException(status_code=500, detail=str(exc))
