"""1 фото: удаление фона (rembg) -> TripoSR -> меш с vertex colors -> GLB."""
from __future__ import annotations
import sys
import os
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', 'TripoSR')))

import logging
from contextlib import contextmanager

import numpy as np
import trimesh
from PIL import Image

from app.config import get_settings
from app.errors import PipelineError
from app.pipeline import export_glb
from app.schemas import Job, Stage
from app.utils.background import rembg_session, remove_background
from app.utils.images import load_rgb
from app.utils.memory import free_memory, get_device, log_stage
from app.utils.progress import ProgressReporter

log = logging.getLogger(__name__)
_triposr = None  # кэш модели (используется только при KEEP_MODELS_LOADED=true)


def run(job: Job, progress: ProgressReporter) -> None:
    cfg = get_settings()
    work = job.work_dir
    work.mkdir(parents=True, exist_ok=True)

    # --- этап 1: подготовка и 3D-представление ---
    progress(Stage.DEPTH_ESTIMATION, 5, "Удаление фона...")
    prepared = _prepare_image(job, cfg)
    prepared.save(work / "preprocessed.png")

    progress(Stage.DEPTH_ESTIMATION, 18, "Загрузка модели TripoSR (первый запуск скачивает веса)...")
    import torch
    with _triposr_model() as model:
        device = get_device()
        progress(Stage.DEPTH_ESTIMATION, 30, "TripoSR: восстановление 3D-представления...")
        with log_stage("TripoSR inference"), torch.no_grad():
            scene_codes = model([prepared], device=device)

        # --- этап 2: меш ---
        progress(Stage.MESH_GENERATION, 55, f"Извлечение меша (marching cubes, {cfg.triposr_mc_resolution}³)...")
        with log_stage("marching cubes"), torch.no_grad():
            mesh = model.extract_mesh(scene_codes, True, resolution=cfg.triposr_mc_resolution)[0]
        del scene_codes

    raw = work / "mesh_raw.ply"
    mesh.export(raw)                                   # чекпоинт на диске; модель уже выгружена
    del mesh
    free_memory()

    # --- этап 3: экспорт ---
    progress(Stage.TEXTURING, 85, "Экспорт в GLB (vertex colors)...")
    mesh = trimesh.load(raw, process=False)
    mesh = export_glb.normalize_mesh(export_glb.triposr_to_gltf_axes(mesh))
    export_glb.export_glb(mesh, job.output_path)


def _prepare_image(job: Job, cfg) -> Image.Image:
    """Фото -> объект на нейтрально-сером фоне (формат, на котором обучен TripoSR)."""
    from tsr.utils import resize_foreground
    image = load_rgb(job.input_files[0], cfg.max_image_side)
    with rembg_session() as session:
        rgba = remove_background(image, session)
    if (np.asarray(rgba)[..., 3] > 127).mean() < 0.01:
        raise PipelineError("Не удалось выделить объект на фото. Используйте однотонный фон и хорошее освещение.")
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
            model.renderer.set_chunk_size(cfg.triposr_chunk_size)   # меньше чанк -> меньше пик RAM
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
