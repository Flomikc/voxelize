"""2+ фото: VGGT (позы камер + глубина) -> облако точек -> Open3D Poisson -> цветной GLB."""
from __future__ import annotations

import logging
from contextlib import contextmanager, nullcontext
from pathlib import Path

import numpy as np
from PIL import Image

from app.config import get_settings
from app.errors import PipelineError
from app.pipeline import export_glb, meshing, texturing
from app.schemas import Job, Stage
from app.utils.background import foreground_alpha, rembg_session
from app.utils.images import prepare_views
from app.utils.memory import free_memory, get_device, log_stage
from app.utils.progress import ProgressReporter

log = logging.getLogger(__name__)
PATCH = 14   # VGGT работает с патчами 14x14 — размеры кадров должны быть кратны 14

_vggt = None  # кэш модели (используется только при KEEP_MODELS_LOADED=true)


def run(job: Job, progress: ProgressReporter) -> None:
    cfg = get_settings()
    work = job.work_dir
    work.mkdir(parents=True, exist_ok=True)

    # --- этап 1: подготовка кадров и оценка геометрии (VGGT) ---
    progress(Stage.DEPTH_ESTIMATION, 3, "Подготовка изображений...")
    selected = _select_views(job.input_files, cfg.max_views)
    views = prepare_views(selected, work / "views", cfg.max_image_side)

    masks = _compute_masks(views, work / "masks", progress) if cfg.multiview_remove_bg else None

    cloud_raw = work / "cloud_raw.npz"
    _reconstruct_point_cloud(views, masks, cloud_raw, progress)

    # --- этап 2: меш (Open3D) ---
    mesh_path, cloud_clean = meshing.build_mesh(cloud_raw, work, progress)

    # --- этап 3: цвет и экспорт ---
    progress(Stage.TEXTURING, 86, "Перенос цветов на меш...")
    mesh = texturing.colorize_mesh(mesh_path, cloud_clean)
    mesh = export_glb.normalize_mesh(export_glb.opencv_to_gltf_axes(mesh))

    progress(Stage.TEXTURING, 94, "Экспорт в GLB...")
    export_glb.export_glb(mesh, job.output_path)


def _select_views(files: list[Path], max_views: int) -> list[Path]:
    """Если кадров больше лимита, равномерно прореживаем (сохраняя охват ракурсов)."""
    if len(files) <= max_views:
        return sorted(files)
    idx = np.unique(np.linspace(0, len(files) - 1, max_views).round().astype(int))
    log.info("Кадров %d > MAX_VIEWS=%d: используем %d", len(files), max_views, len(idx))
    ordered = sorted(files)
    return [ordered[i] for i in idx]


def _compute_masks(views: list[Path], out_dir: Path, progress: ProgressReporter) -> list[Path]:
    """Маски объекта (rembg) — нужны только чтобы отфильтровать точки фона; VGGT получает оригинальные кадры."""
    out_dir.mkdir(parents=True, exist_ok=True)
    paths = []
    with rembg_session() as session:
        for i, v in enumerate(views, start=1):
            progress(Stage.DEPTH_ESTIMATION, 6 + int(12 * i / len(views)), f"Выделение объекта: кадр {i}/{len(views)}...")
            with Image.open(v) as img:
                alpha = foreground_alpha(img.convert("RGB"), session)
            p = out_dir / f"mask_{i:03d}.png"
            Image.fromarray(alpha).save(p)
            paths.append(p)
    return paths


def _load_tensor(paths: list[Path], size: int, pad_value: float):
    """
    Кадры -> тензор (S,3,H,W) в [0,1] + булева карта «реальные пиксели» (S,H,W).
    Длинная сторона = size, обе стороны кратны 14; кадры разной формы добиваются padding'ом до общего размера.
    """
    import torch
    size = max(PATCH, (size // PATCH) * PATCH)
    arrays = []
    for p in paths:
        with Image.open(p) as im:
            img = im.convert("RGB")
        w, h = img.size
        if w >= h:
            nw, nh = size, max(PATCH, round(h * size / w / PATCH) * PATCH)
        else:
            nw, nh = max(PATCH, round(w * size / h / PATCH) * PATCH), size
        arrays.append(np.asarray(img.resize((nw, nh), Image.BICUBIC), dtype=np.float32) / 255.0)

    H = max(a.shape[0] for a in arrays)
    W = max(a.shape[1] for a in arrays)
    batch = np.full((len(arrays), H, W, 3), pad_value, dtype=np.float32)
    valid = np.zeros((len(arrays), H, W), dtype=bool)
    for i, a in enumerate(arrays):
        y0, x0 = (H - a.shape[0]) // 2, (W - a.shape[1]) // 2
        batch[i, y0:y0 + a.shape[0], x0:x0 + a.shape[1]] = a
        valid[i, y0:y0 + a.shape[0], x0:x0 + a.shape[1]] = True
    return torch.from_numpy(batch).permute(0, 3, 1, 2).contiguous(), valid


@contextmanager
def _vggt_model():
    global _vggt
    cfg = get_settings()
    model = _vggt
    if model is None:
        try:
            from vggt.models.vggt import VGGT
        except ImportError as e:
            raise PipelineError("VGGT не установлен в ml-service") from e
        with log_stage("загрузка VGGT"):
            model = VGGT.from_pretrained(cfg.vggt_model).to(get_device()).eval()
    try:
        yield model
    finally:
        if cfg.keep_models_loaded:
            _vggt = model
        else:
            _vggt = None
            del model
            free_memory()


def _reconstruct_point_cloud(views: list[Path], masks: list[Path] | None, out_path: Path,
                             progress: ProgressReporter) -> None:
    """Запускает VGGT и сохраняет отфильтрованное облако точек на диск. Модель выгружается до возврата."""
    import torch
    from vggt.utils.geometry import unproject_depth_map_to_point_map
    from vggt.utils.pose_enc import pose_encoding_to_extri_intri

    cfg = get_settings()
    device = get_device()
    images, valid = _load_tensor(views, cfg.vggt_resolution, pad_value=1.0)
    fg = None
    if masks:
        mask_t, _ = _load_tensor(masks, cfg.vggt_resolution, pad_value=0.0)
        fg = (mask_t[:, 0] > 0.5).numpy()
        del mask_t

    progress(Stage.DEPTH_ESTIMATION, 20, "Загрузка модели VGGT (первый запуск скачивает веса)...")
    with _vggt_model() as model:
        progress(Stage.DEPTH_ESTIMATION, 30, f"VGGT: оценка поз камер и глубины ({len(views)} кадров)...")
        on_cuda = device.startswith("cuda")
        amp = (torch.autocast("cuda", dtype=torch.bfloat16 if torch.cuda.get_device_capability()[0] >= 8
                              else torch.float16) if on_cuda else nullcontext())
        with log_stage("VGGT inference"), torch.inference_mode(), amp:
            preds = model(images.to(device))
            extrinsic, intrinsic = pose_encoding_to_extri_intri(preds["pose_enc"], images.shape[-2:])
            depth = preds["depth"].float().cpu().numpy().squeeze(0)          # (S,H,W,1)
            conf = preds["depth_conf"].float().cpu().numpy().squeeze(0)      # (S,H,W)
            extr = extrinsic.float().cpu().numpy().squeeze(0)                # (S,3,4)  world->cam
            intr = intrinsic.float().cpu().numpy().squeeze(0)                # (S,3,3)
        del preds, extrinsic, intrinsic
    free_memory()                                                            # веса VGGT больше не нужны

    progress(Stage.DEPTH_ESTIMATION, 42, "Построение облака точек...")
    points = unproject_depth_map_to_point_map(depth, extr, intr)             # (S,H,W,3)
    S, H, W, _ = points.shape
    rgb = (images.permute(0, 2, 3, 1).numpy() * 255.0).astype(np.uint8)
    cam_centers = -np.einsum("sji,sj->si", extr[:, :, :3], extr[:, :, 3])    # C = -R^T t

    keep = valid & np.isfinite(points).all(axis=-1)
    if fg is not None:
        if (keep & fg).sum() < 0.02 * keep.sum():
            log.warning("Маска объекта почти пуста — игнорируем её")
        else:
            keep &= fg
    if keep.sum() == 0:
        raise PipelineError("Не удалось получить 3D-точки из этих фотографий")
    threshold = np.percentile(conf[keep], cfg.conf_percentile)
    keep &= conf >= threshold

    cam_idx = np.broadcast_to(np.arange(S, dtype=np.int16)[:, None, None], (S, H, W))
    np.savez(out_path, points=points[keep].astype(np.float32), colors=rgb[keep],
             cam_idx=cam_idx[keep], cam_centers=cam_centers.astype(np.float32))
    log.info("Облако точек: %d точек сохранено в %s", int(keep.sum()), out_path.name)
    del points, depth, conf, rgb, images
    free_memory()
