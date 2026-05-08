"""Сервис индексирования документов базы знаний.

Жизненный цикл документа: pending → indexing → ready (или error).
Вызывается через asyncio.create_task() сразу после создания документа.
"""
import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.chunker import (
    extract_text_from_docx,
    extract_text_from_pdf,
    extract_text_from_url,
    split_into_chunks,
)
from bot.crypto import decrypt_token
from bot.embedding_client import get_embedding
from db.models import AssistantConfig, KnowledgeChunk, KnowledgeDocument
from db.session import AsyncSessionLocal

logger = logging.getLogger(__name__)


async def index_document(document_id: int, raw_content: bytes | str) -> None:
    """Индексирует документ: извлекает текст, нарезает чанки, создаёт эмбеддинги."""
    async with AsyncSessionLocal() as session:
        await _index_document_inner(document_id, raw_content, session)


async def _index_document_inner(
    document_id: int, raw_content: bytes | str, session: AsyncSession
) -> None:
    result = await session.execute(
        select(KnowledgeDocument).where(KnowledgeDocument.id == document_id)
    )
    doc = result.scalar_one_or_none()
    if not doc:
        logger.error("KnowledgeDocument id=%s не найден", document_id)
        return

    cfg_result = await session.execute(
        select(AssistantConfig).where(AssistantConfig.id == doc.assistant_config_id)
    )
    config = cfg_result.scalar_one_or_none()
    if not config or not config.embedding_model:
        doc.status = "error"
        doc.error_message = "Embedding-модель не настроена в конфиге ассистента"
        await session.commit()
        return

    doc.status = "indexing"
    await session.commit()

    try:
        text = _extract_text(doc, raw_content)
        chunks = split_into_chunks(text)

        api_key = decrypt_token(config.embedding_api_key) if config.embedding_api_key else ""
        api_url = config.embedding_api_url or ""

        for idx, chunk_text in enumerate(chunks):
            embedding = await get_embedding(
                chunk_text, config.embedding_model, api_url, api_key
            )
            session.add(
                KnowledgeChunk(
                    document_id=doc.id,
                    content=chunk_text,
                    chunk_index=idx,
                    embedding=embedding,
                )
            )

        doc.status = "ready"
        doc.chunk_count = len(chunks)
        doc.error_message = None
        await session.commit()
        logger.info("Документ id=%s проиндексирован: %s чанков", document_id, len(chunks))

    except Exception as exc:
        logger.exception("Ошибка индексирования документа id=%s", document_id)
        doc.status = "error"
        doc.error_message = str(exc)[:1000]
        await session.commit()


def _extract_text(doc: KnowledgeDocument, raw_content: bytes | str) -> str:
    if doc.source_type == "url":
        return extract_text_from_url(doc.source_hint or str(raw_content))
    if isinstance(raw_content, str):
        return raw_content
    hint = (doc.source_hint or "").lower()
    if hint.endswith(".pdf"):
        return extract_text_from_pdf(raw_content)
    if hint.endswith(".docx"):
        return extract_text_from_docx(raw_content)
    return raw_content.decode("utf-8", errors="replace")
