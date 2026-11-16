"""Управление памятью и устройством. torch импортируется лениво — /health не тормозит."""
from __future__ import annotations

import gc
import logging
import time
from contextlib import contextmanager

import psutil

from app.config import get_settings

log = logging.getLogger(__name__)
_torch_configured = False


def rss_mb() -> float:
    return psutil.Process().memory_info().rss / 1024 / 1024


def free_memory() -> None:
    """Вызывать после del модели/тензоров: возвращает память Python и кэш CUDA."""
    gc.collect()
    try:
        import torch
        if torch.cuda.is_available():
            torch.cuda.empty_cache()
    except ImportError:
        pass


def configure_torch() -> None:
    global _torch_configured
    if _torch_configured:
        return
    import torch
    torch.set_num_threads(get_settings().num_threads)
    _torch_configured = True


def get_device() -> str:
    import torch
    pref = get_settings().device
    if pref == "cpu":
        return "cpu"
    if torch.cuda.is_available():
        return "cuda:0"
    if pref == "cuda":
        log.warning("DEVICE=cuda, но CUDA недоступна — работаем на CPU")
    return "cpu"


@contextmanager
def log_stage(name: str):
    """Логирует длительность этапа и потребление RAM — удобно для отчёта по практике."""
    start = time.perf_counter()
    before = rss_mb()
    try:
        yield
    finally:
        log.info("Этап «%s»: %.1f с, RSS %.0f -> %.0f МБ",
                 name, time.perf_counter() - start, before, rss_mb())
