"""Загрузка и подготовка фотографий."""
from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageOps, UnidentifiedImageError

from app.errors import PipelineError

Image.MAX_IMAGE_PIXELS = 100_000_000   # защита от «бомб декомпрессии»
MIN_SIDE = 64


def load_rgb(path: Path, max_side: int) -> Image.Image:
    """Открывает фото, учитывает EXIF-поворот телефона, уменьшает до max_side."""
    try:
        with Image.open(path) as img:
            if img.format == "JPEG":
                img.draft("RGB", (max_side, max_side))   # JPEG декодируется сразу в меньшем размере (меньше RAM)
            img = ImageOps.exif_transpose(img).convert("RGB")
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as e:
        raise PipelineError(f"Не удалось прочитать изображение «{path.name}»") from e
    if min(img.size) < MIN_SIDE:
        raise PipelineError(f"Изображение «{path.name}» слишком маленькое (минимум {MIN_SIDE} px)")
    if max(img.size) > max_side:
        img.thumbnail((max_side, max_side), Image.LANCZOS)
    return img


def prepare_views(paths: list[Path], out_dir: Path, max_side: int) -> list[Path]:
    """Out-of-core: уменьшенные копии кадров сразу пишутся на диск, дальше работаем с путями."""
    out_dir.mkdir(parents=True, exist_ok=True)
    result = []
    for i, p in enumerate(paths, start=1):
        img = load_rgb(p, max_side)
        target = out_dir / f"view_{i:03d}.jpg"
        img.save(target, quality=95)
        result.append(target)
        del img
    return result
