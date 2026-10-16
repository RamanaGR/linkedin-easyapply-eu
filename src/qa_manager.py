"""Fuzzy + embedding Q&A memory backed by CSV."""

from __future__ import annotations

import csv
import logging
from pathlib import Path

from rapidfuzz import fuzz

from src.config import AppConfig
from src.ollama_client import (
    OllamaClient,
    embedding_from_str,
    embedding_to_str,
    sanitize_human_text,
)
from src.utils import project_path

logger = logging.getLogger("easyapply")

MEMORY_FIELDS = ["question", "answer", "embedding"]


class QAMemoryManager:
    def __init__(self, config: AppConfig, ollama: OllamaClient | None = None) -> None:
        self.config = config
        self.ollama = ollama
        self.path = project_path("output", "qa_memory.csv")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.rows: list[dict[str, str]] = []
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            self._write()
            return
        with self.path.open(newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            self.rows = list(reader)

    def _write(self) -> None:
        with self.path.open("w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=MEMORY_FIELDS)
            writer.writeheader()
            for row in self.rows:
                writer.writerow({k: row.get(k, "") for k in MEMORY_FIELDS})

    def remember(self, question: str, answer: str) -> None:
        answer = sanitize_human_text(answer)
        emb = ""
        if self.ollama and self.config.qa_memory.use_embeddings:
            try:
                emb = embedding_to_str(self.ollama.embed(question))
            except Exception as exc:
                logger.warning("Embed failed for memory: %s", exc)
        # Update existing
        for row in self.rows:
            if row.get("question", "").strip().lower() == question.strip().lower():
                row["answer"] = answer
                if emb:
                    row["embedding"] = emb
                self._write()
                return
        self.rows.append({"question": question, "answer": answer, "embedding": emb})
        self._write()

    def lookup(self, question: str) -> str | None:
        if not question.strip():
            return None
        high = self.config.qa_memory.fuzzy_high_threshold
        low = self.config.qa_memory.fuzzy_low_threshold

        best_score = 0
        best_answer: str | None = None
        for row in self.rows:
            score = fuzz.token_set_ratio(question, row.get("question", ""))
            if score > best_score:
                best_score = score
                best_answer = row.get("answer") or None

        if best_score >= high and best_answer:
            return best_answer

        if (
            self.ollama
            and self.config.qa_memory.use_embeddings
            and self.rows
        ):
            try:
                q_emb = self.ollama.embed(question)
                best_sim = -1.0
                best_emb_answer: str | None = None
                updated = False
                for row in self.rows:
                    emb = embedding_from_str(row.get("embedding", ""))
                    if emb is None:
                        emb = self.ollama.embed(row.get("question", ""))
                        row["embedding"] = embedding_to_str(emb)
                        updated = True
                    sim = self.ollama.cosine(q_emb, emb)
                    if sim > best_sim:
                        best_sim = sim
                        best_emb_answer = row.get("answer") or None
                if updated:
                    self._write()
                # cosine ~0.75+ is a reasonable soft match for nomic
                if best_sim >= 0.78 and best_emb_answer:
                    return best_emb_answer
            except Exception as exc:
                logger.warning("Embedding lookup failed: %s", exc)

        if best_score >= low and best_answer:
            return best_answer
        return None


def append_unanswered(question: str, error: str, job_id: str = "") -> None:
    path = project_path("output", "unanswered_questions.csv")
    path.parent.mkdir(parents=True, exist_ok=True)
    exists = path.exists()
    with path.open("a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["job_id", "question", "error"])
        if not exists:
            writer.writeheader()
        writer.writerow({"job_id": job_id, "question": question, "error": error})
