"""Удаление фона (rembg, ONNX на CPU). Сессия создаётся на время этапа и затем освобождается."""
from __future__ import annotations

from contextlib import contextmanager

import numpy as np
from PIL import Image

from app.config import get_settings
from app.errors import PipelineError
from app.utils.memory import free_memory


@contextmanager
def rembg_session():
    try:
        from rembg import new_session
    except ImportError as e:
        raise PipelineError("Библиотека rembg не установлена в ml-service") from e
    session = new_session(get_settings().rembg_model)
    try:
        yield session
    finally:
        del session
        free_memory()


def remove_background(image: Image.Image, session) -> Image.Image:
    """RGB -> RGBA, где альфа-канал — маска объекта."""
    from rembg import remove
    return remove(image, session=session).convert("RGBA")


def foreground_alpha(image: Image.Image, session) -> np.ndarray:
    return np.asarray(remove_background(image, session))[..., 3]
