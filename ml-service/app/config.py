"""Конфигурация ml-service. Всё настраивается переменными окружения (см. docker-compose)."""
from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path


def _int(name: str, default: int) -> int:
    return int(os.getenv(name, default))


def _float(name: str, default: float) -> float:
    return float(os.getenv(name, default))


def _bool(name: str, default: bool) -> bool:
    return os.getenv(name, str(default)).strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    # --- общее ---
    data_dir: Path               # общий том с Java (в Docker: /data)
    device: str                  # auto | cpu | cuda
    num_threads: int             # потоки PyTorch (оставляем 1 ядро свободным)
    keep_models_loaded: bool     # False = выгружать модели после каждой задачи (экономия RAM)
    keep_work_files: bool        # True = не удалять work/ после успеха (отладка)
    max_input_files: int
    max_image_side: int          # входные фото уменьшаются до этого размера (px)
    rembg_model: str             # u2net (качество) | u2netp (быстрее, легче)

    # --- 1 фото: TripoSR ---
    triposr_model: str
    triposr_chunk_size: int      # меньше -> меньше RAM, медленнее
    triposr_mc_resolution: int   # разрешение marching cubes: 128 / 192 / 256
    triposr_foreground_ratio: float

    # --- 2+ фото: VGGT + Open3D ---
    max_views: int               # больше кадров -> лучше, но дольше и тяжелее
    vggt_model: str
    vggt_resolution: int         # длинная сторона, кратно 14 (518 = родное; 336/392 быстрее)
    multiview_remove_bg: bool    # отфильтровать точки фона маской rembg
    conf_percentile: float       # отбросить N% наименее уверенных точек
    voxel_fraction: float        # размер вокселя = диагональ облака * это значение
    max_points: int              # потолок числа точек перед Poisson
    poisson_depth: int           # 8 (CPU) .. 10 (детальнее, тяжелее)
    density_trim: float          # доля вершин Poisson с низкой плотностью, которую срезаем
    target_triangles: int        # потолок числа треугольников итогового меша


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings(
        data_dir=Path(os.getenv("DATA_DIR", "/data")),
        device=os.getenv("DEVICE", "auto").strip().lower(),
        num_threads=_int("NUM_THREADS", max(1, (os.cpu_count() or 2) - 1)),
        keep_models_loaded=_bool("KEEP_MODELS_LOADED", False),
        keep_work_files=_bool("KEEP_WORK_FILES", False),
        max_input_files=_int("MAX_INPUT_FILES", 20),
        max_image_side=_int("MAX_IMAGE_SIDE", 1024),
        rembg_model=os.getenv("REMBG_MODEL", "u2net"),
        triposr_model=os.getenv("TRIPOSR_MODEL", "stabilityai/TripoSR"),
        triposr_chunk_size=_int("TRIPOSR_CHUNK_SIZE", 4096),
        triposr_mc_resolution=_int("TRIPOSR_MC_RESOLUTION", 192),
        triposr_foreground_ratio=_float("TRIPOSR_FOREGROUND_RATIO", 0.85),
        max_views=_int("MAX_VIEWS", 6),
        vggt_model=os.getenv("VGGT_MODEL", "facebook/VGGT-1B"),
        vggt_resolution=_int("VGGT_RESOLUTION", 518),
        multiview_remove_bg=_bool("MULTIVIEW_REMOVE_BG", True),
        conf_percentile=_float("CONF_PERCENTILE", 40.0),
        voxel_fraction=_float("VOXEL_FRACTION", 0.004),
        max_points=_int("MAX_POINTS", 250_000),
        poisson_depth=_int("POISSON_DEPTH", 8),
        density_trim=_float("DENSITY_TRIM", 0.08),
        target_triangles=_int("TARGET_TRIANGLES", 120_000),
    )
