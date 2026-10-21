"""Multi-role local Ollama client and humanizer."""

from __future__ import annotations

import json
import logging
import re
from typing import Any

import httpx
import numpy as np

from src.config import EnvSettings

logger = logging.getLogger("easyapply")

SYSTEM_PROMPT = """You are the job candidate filling a LinkedIn Easy Apply form.
Write answers in first person as if you typed them yourself in plain everyday English.

Rules:
1. Use ONLY facts from the provided Resume and Config context. Never invent skills, companies, degrees, or years.
2. Sound like a fluent non-native English-speaking engineer: simple, direct, natural. Not like a chatbot or resume parser.
3. Keep open-ended answers brief: 1 to 3 short sentences unless a shorter limit is given.
4. NEVER use em-dashes, en-dashes, semicolons, or markdown formatting.
5. NEVER use AI buzzwords: delve, testament, tapestry, furthermore, moreover, pivotal, spearheaded, unwavering, thrilled, synergy, landscape, cutting-edge, leverage, utilizing, orchestrated, passionate. Use plain words like built, worked on, used, developed.
6. Return ONLY the direct answer text. No preamble, no quotes, no labels.
7. NEVER start with or include phrases like: My resume details, Based on my resume, According to my profile, According to my resume, From my resume, As per my resume, As an AI, Here is my answer, Sure, or similar meta openings. Just answer the question."""

BANNED_REPLACEMENTS = [
    (r"\bspearheaded\b", "led"),
    (r"\bleveraged\b", "used"),
    (r"\bleveraging\b", "using"),
    (r"\bleverage\b", "use"),
    (r"\butilizing\b", "using"),
    (r"\butilize\b", "use"),
    (r"\bfurthermore\b", "Also"),
    (r"\bmoreover\b", "Also"),
    (r"\bdelve\b", "look"),
    (r"\btestament\b", "proof"),
    (r"\btapestry\b", "mix"),
    (r"\bpivotal\b", "important"),
    (r"\bunwavering\b", "strong"),
    (r"\bthrilled\b", "glad"),
    (r"\bsynergy\b", "teamwork"),
    (r"\blandscape\b", "field"),
    (r"\bcutting-edge\b", "modern"),
    (r"\borchestrated\b", "ran"),
    (r"\bpassionate\b", "interested"),
]

# Meta openings models sometimes prepend before the real answer
PREAMBLE_PATTERNS = [
    r"^(my\s+resume\s+details?\s*[:\-–—,]?\s*)",
    r"^(based\s+on\s+my\s+resume\s*[:\-–—,]?\s*)",
    r"^(according\s+to\s+my\s+(resume|profile|cv)\s*[:\-–—,]?\s*)",
    r"^(from\s+my\s+resume\s*[:\-–—,]?\s*)",
    r"^(as\s+per\s+my\s+(resume|profile|cv)\s*[:\-–—,]?\s*)",
    r"^(as\s+an\s+ai\s*[:\-–—,]?\s*)",
    r"^(here\s+is\s+my\s+answer\s*[:\-–—,]?\s*)",
    r"^(sure[!.,]?\s*)",
    r"^(of\s+course[!.,]?\s*)",
    r"^(answer\s*[:\-–—]\s*)",
]


def sanitize_human_text(text: str) -> str:
    if not text:
        return ""
    out = text.strip()
    out = out.replace("—", ", ").replace("–", ", ")
    out = out.replace(";", ".")
    out = out.replace("```", "").replace("**", "").replace("*", "")
    out = out.strip().strip('"').strip("'")
    # Strip meta preambles (repeat in case of stacked openers)
    for _ in range(3):
        cleaned = out
        for pattern in PREAMBLE_PATTERNS:
            cleaned = re.sub(pattern, "", cleaned, flags=re.IGNORECASE).strip()
        if cleaned == out:
            break
        out = cleaned
    for pattern, repl in BANNED_REPLACEMENTS:
        out = re.sub(pattern, repl, out, flags=re.IGNORECASE)
    out = re.sub(r"\s+", " ", out).strip()
    return out


def truncate_at_word(text: str, max_chars: int) -> str:
    """Hard-cap length; prefer cutting on a word boundary."""
    if max_chars is None or max_chars <= 0 or not text:
        return text or ""
    if len(text) <= max_chars:
        return text
    cut = text[:max_chars].rstrip()
    if " " in cut and max_chars > 12:
        maybe = cut.rsplit(" ", 1)[0].rstrip()
        if len(maybe) >= max(8, max_chars // 3):
            return maybe
    return cut


class OllamaClient:
    def __init__(self, env: EnvSettings, timeout: float = 120.0) -> None:
        self.env = env
        self.base = env.ollama_host.rstrip("/")
        self.timeout = timeout
        self._client = httpx.Client(timeout=timeout)

    def close(self) -> None:
        self._client.close()

    def ping(self) -> dict[str, Any]:
        r = self._client.get(f"{self.base}/api/tags")
        r.raise_for_status()
        return r.json()

    def list_models(self) -> list[str]:
        data = self.ping()
        return [m.get("name", "") for m in data.get("models", [])]

    def ensure_models(self) -> list[str]:
        available = self.list_models()
        required = [
            self.env.ollama_model,
            self.env.ollama_intent_model,
            self.env.ollama_embed_model,
        ]
        missing = [m for m in required if m not in available and not any(m in a for a in available)]
        if missing:
            # Allow fallback for chat model
            if self.env.ollama_model in missing and self.env.ollama_model_fallback in available:
                logger.warning(
                    "Primary model %s missing; will use fallback %s",
                    self.env.ollama_model,
                    self.env.ollama_model_fallback,
                )
                missing = [m for m in missing if m != self.env.ollama_model]
            if missing:
                raise RuntimeError(f"Missing Ollama models: {missing}. Available: {available}")
        return available

    def _chat(self, model: str, system: str, user: str) -> str:
        payload = {
            "model": model,
            "stream": False,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        }
        try:
            r = self._client.post(f"{self.base}/api/chat", json=payload)
            r.raise_for_status()
            content = r.json().get("message", {}).get("content", "")
            return sanitize_human_text(content)
        except Exception as exc:
            if model == self.env.ollama_model and self.env.ollama_model_fallback:
                logger.warning("Chat failed on %s (%s); trying fallback", model, exc)
                return self._chat(self.env.ollama_model_fallback, system, user)
            raise

    def answer_question(
        self,
        question: str,
        context: str,
        extra: str = "",
        max_chars: int | None = None,
        digits_only: bool = False,
    ) -> str:
        constraints: list[str] = []
        if digits_only:
            constraints.append(
                "The field accepts ONLY digits (a whole number). "
                "Return a single integer with no currency symbols, words, or ranges."
            )
        if max_chars and max_chars > 0:
            constraints.append(
                f"Keep the entire answer under {max_chars} characters. "
                "Prefer one short sentence if needed."
            )
        constraint_block = ("\n".join(constraints) + "\n") if constraints else ""
        user = (
            f"Context (use as grounding facts only, do not quote it back):\n{context}\n\n"
            f"{extra}\n\n"
            f"{constraint_block}"
            f"Question: {question}\n"
            f"Answer (first person, plain English, no preamble):"
        )
        answer = self._chat(self.env.ollama_model, SYSTEM_PROMPT, user)
        if digits_only:
            digits = re.sub(r"[^\d]", "", answer or "")
            answer = digits
        if max_chars and max_chars > 0:
            answer = truncate_at_word(answer, max_chars)
        return answer

    def match_option(self, question: str, options: list[str], hint: str = "") -> str:
        if not options:
            return ""
        if len(options) == 1:
            return options[0]
        opts = "\n".join(f"- {o}" for o in options)
        user = (
            f"Question: {question}\n"
            f"Hint: {hint}\n"
            f"Options:\n{opts}\n"
            "Return ONLY the exact option text that best matches. No explanation."
        )
        system = "You pick the best dropdown/radio option. Return only the option text."
        raw = self._chat(self.env.ollama_intent_model, system, user)
        # Prefer exact / substring match against options
        for o in options:
            if normalize_eq(raw, o):
                return o
        for o in options:
            if normalize_eq(raw) in normalize_eq(o) or normalize_eq(o) in normalize_eq(raw):
                return o
        return options[0]

    def embed(self, text: str) -> list[float]:
        payload = {"model": self.env.ollama_embed_model, "prompt": text}
        r = self._client.post(f"{self.base}/api/embeddings", json=payload)
        r.raise_for_status()
        return list(r.json().get("embedding", []))

    def cosine(self, a: list[float], b: list[float]) -> float:
        va = np.array(a, dtype=float)
        vb = np.array(b, dtype=float)
        denom = float(np.linalg.norm(va) * np.linalg.norm(vb))
        if denom == 0:
            return 0.0
        return float(np.dot(va, vb) / denom)


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip().lower())


def normalize_eq(a: str, b: str | None = None) -> str | bool:
    if b is None:
        return _norm(a)
    return _norm(a) == _norm(b)


def embedding_to_str(vec: list[float]) -> str:
    return json.dumps(vec)


def embedding_from_str(raw: str) -> list[float] | None:
    if not raw or not raw.strip():
        return None
    try:
        data = json.loads(raw)
        if isinstance(data, list):
            return [float(x) for x in data]
    except json.JSONDecodeError:
        return None
    return None
