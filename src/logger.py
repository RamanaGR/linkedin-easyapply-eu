"""Structured logging helpers."""

from __future__ import annotations

import logging
import sys
from typing import Any


def setup_logger(name: str = "easyapply", level: int = logging.INFO) -> logging.Logger:
    logger = logging.getLogger(name)
    if logger.handlers:
        return logger
    logger.setLevel(level)
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        logging.Formatter("%(asctime)s | %(levelname)s | %(message)s", "%H:%M:%S")
    )
    logger.addHandler(handler)
    return logger


def log_job(
    logger: logging.Logger,
    status: str,
    *,
    job_id: str = "",
    title: str = "",
    company: str = "",
    extra: str = "",
) -> None:
    parts = [f"status={status}"]
    if job_id:
        parts.append(f"job_id={job_id}")
    if company:
        parts.append(f"company={company}")
    if title:
        parts.append(f"title={title}")
    if extra:
        parts.append(extra)
    logger.info(" | ".join(parts))


def ctx(**kwargs: Any) -> str:
    return " ".join(f"{k}={v}" for k, v in kwargs.items() if v is not None)
