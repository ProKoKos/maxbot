"""REST API базы знаний: CRUD-эндпоинты /kb/*."""
import asyncio
import logging

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from pydantic import BaseModel
from sqlalchemy import select

from bot.kb_indexer import index_document
from db.models import AssistantConfig, KnowledgeChunk, KnowledgeDocument
from web.deps import CurrentUser, DBSession, RateLimit

logger = logging.getLogger(__name__)
router = APIRouter()

MAX_FILE_SIZE = 10 * 1024 * 1024  # 10 МБ


class DocumentCreate(BaseModel):
    config_id: int
    title: str
    source_type: str  # "text" | "url"
    content: str      # текст или URL


@router.get("/kb/documents")
async def list_documents(
    config_id: int,
    current_user: CurrentUser,
    session: DBSession,
    _: RateLimit,
):
    cfg = await _get_config(config_id, current_user, session)
    result = await session.execute(
        select(KnowledgeDocument)
        .where(KnowledgeDocument.assistant_config_id == cfg.id)
        .order_by(KnowledgeDocument.created_at.desc())
    )
    docs = result.scalars().all()
    return [
        {
            "id": d.id,
            "title": d.title,
            "source_type": d.source_type,
            "source_hint": d.source_hint,
            "status": d.status,
            "error_message": d.error_message,
            "chunk_count": d.chunk_count,
            "created_at": d.created_at.isoformat(),
        }
        for d in docs
    ]


@router.post("/kb/documents", status_code=201)
async def create_document(
    body: DocumentCreate,
    current_user: CurrentUser,
    session: DBSession,
    _: RateLimit,
):
    cfg = await _get_config(body.config_id, current_user, session)

    if body.source_type not in ("text", "url"):
        raise HTTPException(400, "source_type должен быть 'text' или 'url'")

    hint = body.content if body.source_type == "url" else None
    doc = KnowledgeDocument(
        assistant_config_id=cfg.id,
        title=body.title,
        source_type=body.source_type,
        source_hint=hint,
        status="pending",
    )
    session.add(doc)
    await session.commit()
    await session.refresh(doc)

    raw: bytes | str = body.content
    asyncio.create_task(index_document(doc.id, raw))
    return {"id": doc.id}


@router.post("/kb/documents/upload", status_code=201)
async def upload_document(
    current_user: CurrentUser,
    session: DBSession,
    _: RateLimit,
    config_id: int = Form(...),
    title: str = Form(...),
    file: UploadFile = File(...),
):
    cfg = await _get_config(config_id, current_user, session)

    data = await file.read()
    if len(data) > MAX_FILE_SIZE:
        raise HTTPException(400, "Файл превышает лимит 10 МБ")

    filename = file.filename or ""
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if ext not in ("pdf", "docx", "txt", "md"):
        raise HTTPException(400, "Поддерживаемые форматы: PDF, DOCX, TXT, MD")

    doc = KnowledgeDocument(
        assistant_config_id=cfg.id,
        title=title,
        source_type="file",
        source_hint=filename,
        status="pending",
    )
    session.add(doc)
    await session.commit()
    await session.refresh(doc)

    asyncio.create_task(index_document(doc.id, data))
    return {"id": doc.id}


@router.delete("/kb/documents/{doc_id}", status_code=204)
async def delete_document(
    doc_id: int,
    current_user: CurrentUser,
    session: DBSession,
    _: RateLimit,
):
    doc = await _get_document(doc_id, current_user, session)
    await session.delete(doc)
    await session.commit()


@router.get("/kb/documents/{doc_id}/chunks")
async def get_chunks_preview(
    doc_id: int,
    current_user: CurrentUser,
    session: DBSession,
    _: RateLimit,
):
    doc = await _get_document(doc_id, current_user, session)
    result = await session.execute(
        select(KnowledgeChunk)
        .where(KnowledgeChunk.document_id == doc.id)
        .order_by(KnowledgeChunk.chunk_index)
        .limit(5)
    )
    chunks = result.scalars().all()
    return [{"index": c.chunk_index, "content": c.content} for c in chunks]


async def _get_config(config_id: int, current_user, session) -> AssistantConfig:
    result = await session.execute(
        select(AssistantConfig).where(
            AssistantConfig.id == config_id,
            AssistantConfig.user_id == current_user.id,
        )
    )
    cfg = result.scalar_one_or_none()
    if not cfg:
        raise HTTPException(404, "AssistantConfig не найден")
    return cfg


async def _get_document(doc_id: int, current_user, session) -> KnowledgeDocument:
    result = await session.execute(
        select(KnowledgeDocument)
        .join(AssistantConfig)
        .where(
            KnowledgeDocument.id == doc_id,
            AssistantConfig.user_id == current_user.id,
        )
    )
    doc = result.scalar_one_or_none()
    if not doc:
        raise HTTPException(404, "Документ не найден")
    return doc
