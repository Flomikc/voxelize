"""1 фото: (опционально) удаление фона -> TripoSR -> прореживание -> UV-текстура -> GLB (+ данные для пост-обработки)."""
from __future__ import annotations

import logging
import os
import sys
from contextlib import contextmanager
from pathlib import Path

import numpy as np
import trimesh
from PIL import Image

# путь к локальной копии TripoSR (ml-service/TripoSR)
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', 'TripoSR')))

from app.config import get_settings
from app.errors import PipelineError
from app.pipeline import postprocess
from app.schemas import MC_RESOLUTION, GenerateOptions, Job, Stage
from app.utils.background import rembg_session, remove_background
from app.utils.images import load_rgb
from app.utils.memory import free_memory, get_device, log_stage
from app.utils.progress import ProgressReporter

log = logging.getLogger(__name__)
_triposr = None  # кэш модели (только при KEEP_MODELS_LOADED=true)


def run(job: Job, progress: ProgressReporter) -> None:
    cfg, opts = get_settings(), job.options
    mc_resolution = MC_RESOLUTION[opts.detail_level]
    work = job.work_dir
    work.mkdir(parents=True, exist_ok=True)
    log.info("[%s] TripoSR: detail=%s (MC %d³), лимит граней=%s, rembg=%s", job.task_id,
             opts.detail_level.value, mc_resolution, opts.polygon_limit or "нет", opts.use_rembg)

    # ── этап 1: подготовка изображения и 3D-представление ──
    progress(Stage.DEPTH_ESTIMATION, 5, "Удаление фона..." if opts.use_rembg else "Подготовка изображения...")
    prepared = _prepare_image(job, cfg, opts)
    prepared.save(work / "preprocessed.png")

    progress(Stage.DEPTH_ESTIMATION, 18, "Загрузка модели TripoSR (первый запуск скачивает веса)...")
    import torch
    with _triposr_model() as model:
        device = get_device()
        progress(Stage.DEPTH_ESTIMATION, 30, "TripoSR: восстановление 3D-представления...")
        with log_stage("TripoSR inference"), torch.no_grad():
            scene_codes = model([prepared], device=device)

        # ── этап 2: меш ──
        progress(Stage.MESH_GENERATION, 50, f"Извлечение меша (marching cubes, {mc_resolution}³)...")
        with log_stage("marching cubes"), torch.no_grad():
            mesh = model.extract_mesh(scene_codes, True, resolution=mc_resolution)[0]
        del scene_codes
    free_memory()                                         # веса TripoSR больше не нужны

    _sanity_check_mesh(mesh)
    progress(Stage.MESH_GENERATION, 60, "Починка меша (слияние вершин, мусор, дыры, нормали)...")
    mesh = postprocess.clean(mesh)
    n_raw = len(mesh.faces)
    if opts.polygon_limit and n_raw > opts.polygon_limit:
        progress(Stage.MESH_GENERATION, 70, f"Упрощение меша: {n_raw:,} → {opts.polygon_limit:,} граней...".replace(",", " "))
        with log_stage("decimation"):
            mesh = postprocess.decimate(mesh, opts.polygon_limit)

    # ── этап 3: текстура и экспорт ──
    post_dir = job.output_path.parent / "post"            # переживает очистку work/ — нужен для сглаживания
    progress(Stage.TEXTURING, 80, "UV-развёртка и запекание текстуры...")
    with log_stage("UV + bake"):
        postprocess.prepare_post_assets(mesh, "triposr", post_dir)
    del mesh

    progress(Stage.TEXTURING, 94, "Экспорт в GLB...")
    postprocess.publish_base_model(post_dir, job.output_path)


def _sanity_check_mesh(mesh) -> None:
    """Ловит типичные поломки isosurface.py сразу и с понятным текстом, а не «серой» моделью на выходе."""
    if len(mesh.faces) == 0:
        raise PipelineError("Не удалось построить поверхность. Попробуйте другое фото или переключите удаление фона.")
    v = np.asarray(mesh.vertices)
    log.info("Меш: %d вершин, %d граней, вершины от %.3f до %.3f", len(v), len(mesh.faces), v.min(), v.max())
    if np.abs(v).max() > 2.0:      # у TripoSR вершины лежат в пределах ±radius (≈0.87)
        raise PipelineError("Вершины меша вне ожидаемого диапазона: проверьте tsr/models/isosurface.py — "
                            "вершины должны делиться на (resolution - 1). Иначе цвета запрашиваются вне triplane и модель серая.")
    if mesh.visual.kind == "vertex":
        spread = float(np.asarray(mesh.visual.vertex_colors)[:, :3].std(axis=0).mean())
        if spread < 2.0:
            log.warning("Цвета вершин почти одинаковые (std=%.1f) — модель будет однотонной", spread)


def _prepare_image(job: Job, cfg, opts: GenerateOptions) -> Image.Image:
    """Фото -> вход TripoSR."""
    image = load_rgb(job.input_files[0], cfg.max_image_side)
    if not opts.use_rembg:
        # Без удаления фона TripoSR получает картинку как есть. Модель растягивает вход до квадрата,
        # поэтому дополняем до квадрата нейтрально-серым (иначе объект исказится).
        side = max(image.size)
        canvas = Image.new("RGB", (side, side), (127, 127, 127))
        canvas.paste(image, ((side - image.width) // 2, (side - image.height) // 2))
        return canvas

    from tsr.utils import resize_foreground
    with rembg_session() as session:
        rgba = remove_background(image, session)
    if (np.asarray(rgba)[..., 3] > 127).mean() < 0.01:
        raise PipelineError("Не удалось выделить объект на фото. Используйте однотонный фон и хорошее освещение "
                            "или отключите удаление фона.")
    rgba = resize_foreground(rgba, cfg.triposr_foreground_ratio)
    arr = np.asarray(rgba).astype(np.float32) / 255.0
    rgb = arr[..., :3] * arr[..., 3:4] + (1.0 - arr[..., 3:4]) * 0.5
    return Image.fromarray((rgb * 255.0).astype(np.uint8))


@contextmanager
def _triposr_model():
    global _triposr
    cfg = get_settings()
    model = _triposr
    if model is None:
        try:
            from tsr.system import TSR
        except ImportError as e:
            raise PipelineError("TripoSR не установлен в ml-service") from e
        with log_stage("загрузка TripoSR"):
            model = TSR.from_pretrained(cfg.triposr_model, config_name="config.yaml", weight_name="model.ckpt")
            model.renderer.set_chunk_size(cfg.triposr_chunk_size)
            model.to(get_device())
            model.eval()
    try:
        yield model
    finally:
        if cfg.keep_models_loaded:
            _triposr = model
        else:
            _triposr = None
            del model
            free_memory()
