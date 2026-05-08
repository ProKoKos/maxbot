"""Утилиты извлечения текста и разбивки на чанки для RAG-пайплайна."""
import io

import tiktoken


def extract_text_from_pdf(data: bytes) -> str:
    from pypdf import PdfReader
    reader = PdfReader(io.BytesIO(data))
    return "\n\n".join(page.extract_text() or "" for page in reader.pages)


def extract_text_from_docx(data: bytes) -> str:
    from docx import Document
    doc = Document(io.BytesIO(data))
    return "\n\n".join(p.text for p in doc.paragraphs if p.text.strip())


def extract_text_from_url(url: str) -> str:
    import trafilatura
    downloaded = trafilatura.fetch_url(url)
    if not downloaded:
        raise ValueError(f"Не удалось загрузить страницу: {url}")
    text = trafilatura.extract(downloaded)
    if not text:
        raise ValueError(f"Не удалось извлечь текст со страницы: {url}")
    return text


def split_into_chunks(text: str, max_tokens: int = 500, overlap: int = 50) -> list[str]:
    """Разбивает текст на чанки по параграфам с учётом лимита токенов."""
    enc = tiktoken.get_encoding("cl100k_base")

    paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]

    chunks: list[str] = []
    current_parts: list[str] = []
    current_tokens = 0

    for para in paragraphs:
        para_tokens = len(enc.encode(para))

        if para_tokens > max_tokens:
            # Длинный параграф — разбиваем по предложениям
            if current_parts:
                chunks.append("\n\n".join(current_parts))
                current_parts = []
                current_tokens = 0
            sentences = para.replace(". ", ".\n").split("\n")
            for sent in sentences:
                sent = sent.strip()
                if not sent:
                    continue
                sent_tokens = len(enc.encode(sent))
                if current_tokens + sent_tokens > max_tokens and current_parts:
                    chunks.append("\n\n".join(current_parts))
                    # Оставляем overlap: берём последние overlap токенов
                    overlap_parts: list[str] = []
                    overlap_total = 0
                    for part in reversed(current_parts):
                        t = len(enc.encode(part))
                        if overlap_total + t <= overlap:
                            overlap_parts.insert(0, part)
                            overlap_total += t
                        else:
                            break
                    current_parts = overlap_parts
                    current_tokens = overlap_total
                current_parts.append(sent)
                current_tokens += sent_tokens
        else:
            if current_tokens + para_tokens > max_tokens and current_parts:
                chunks.append("\n\n".join(current_parts))
                overlap_parts = []
                overlap_total = 0
                for part in reversed(current_parts):
                    t = len(enc.encode(part))
                    if overlap_total + t <= overlap:
                        overlap_parts.insert(0, part)
                        overlap_total += t
                    else:
                        break
                current_parts = overlap_parts
                current_tokens = overlap_total
            current_parts.append(para)
            current_tokens += para_tokens

    if current_parts:
        chunks.append("\n\n".join(current_parts))

    return [c for c in chunks if c.strip()]
