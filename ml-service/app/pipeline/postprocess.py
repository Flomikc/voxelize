"""
Пост-обработка готовой модели: прореживание, сглаживание Taubin, экспорт GLB/STL.

Архитектура (см. описание в чате):
  tasks/{id}/post/
      mesh_raw.ply     — «сырой» меш в родной СК (после прореживания), с цветами вершин. НЕ меняется никогда.
      atlas.npz        — UV-развёртка (строится один раз: топология при сглаживании не меняется)
      texture.png      — запечённая текстура (цвета вершин при сглаживании тоже не меняются)
      meta.json        — система координат, преобразование в glTF, габариты для STL, счётчики
      variants/        — кэш результатов: s04.glb, s04_100mm.stl, ...  (уровень сглаживания -> файл)

Любой вариант строится ИЗ mesh_raw.ply заново (не накопительно), поэтому ползунок «Сглаживание»
идемпотентен: s00 всегда исходная модель, s05 — всегда одно и то же, повторный запрос отдаётся из кэша.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import threading
from pathlib import Path

import numpy as np
import trimesh
from PIL import Image
from scipy.spatial import cKDTree

from app.errors import PipelineError
from app.pipeline import export_glb as eg

log = logging.getLogger(__name__)

# Уровень ползунка (0..10) -> число итераций Taubin
SMOOTH_ITERATIONS = [0, 2, 4, 6, 10, 15, 20, 30, 45, 65, 90]
TAUBIN_LAMBDA = 0.5
TAUBIN_NU = 0.526          # 1/λ + 1/μ ≈ 0.1 при μ = -ν: сглаживает шум, почти не «съедая» объём

_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


def _lock_for(key: str) -> threading.Lock:
    with _locks_guard:
        return _locks.setdefault(key, threading.Lock())


# ─────────────────────────────── цвета и прореживание ───────────────────────────────
def transfer_colors(src_vertices: np.ndarray, src_rgba: np.ndarray, dst_vertices: np.ndarray, k: int = 3) -> np.ndarray:
    """Цвета для новых вершин = взвешенное по расстоянию среднее k ближайших вершин исходной сетки."""
    dist, idx = cKDTree(src_vertices).query(dst_vertices, k=k, workers=-1)
    w = 1.0 / (dist + 1e-12)
    w /= w.sum(axis=1, keepdims=True)
    rgb = (src_rgba[idx][..., :3].astype(np.float64) * w[..., None]).sum(axis=1)
    out = np.full((len(dst_vertices), 4), 255, dtype=np.uint8)
    out[:, :3] = np.clip(np.rint(rgb), 0, 255).astype(np.uint8)
    return out


def decimate(mesh: trimesh.Trimesh, face_limit: int) -> trimesh.Trimesh:
    """
    Quadric decimation до face_limit граней. 0 или «уже меньше» — без изменений.
    Упрощение теряет цвета вершин (trimesh/fast_simplification их не переносят), поэтому
    цвета переносятся с исходной сетки по ближайшим вершинам.
    """
    if face_limit <= 0 or len(mesh.faces) <= face_limit:
        return mesh
    src_v = np.asarray(mesh.vertices, dtype=np.float64).copy()
    rgba = np.asarray(mesh.visual.vertex_colors, dtype=np.uint8).copy() if mesh.visual.kind == "vertex" else None
    try:
        try:
            out = mesh.simplify_quadric_decimation(face_count=face_limit)
        except TypeError:                                   # старые версии trimesh: позиционный аргумент
            out = mesh.simplify_quadric_decimation(face_limit)
    except ImportError:                                     # нет fast-simplification -> Open3D (он уже установлен)
        log.warning("fast-simplification не установлен, упрощаем через Open3D")
        import open3d as o3d
        m = o3d.geometry.TriangleMesh(o3d.utility.Vector3dVector(src_v), o3d.utility.Vector3iVector(np.asarray(mesh.faces)))
        m = m.simplify_quadric_decimation(target_number_of_triangles=face_limit)
        out = trimesh.Trimesh(np.asarray(m.vertices), np.asarray(m.triangles), process=False)
    log.info("Прореживание: %d -> %d граней", len(mesh.faces), len(out.faces))
    if rgba is not None:
        out.visual = trimesh.visual.ColorVisuals(out, vertex_colors=transfer_colors(src_v, rgba, out.vertices))
    return out


def smooth_vertices(vertices: np.ndarray, faces: np.ndarray, level: int) -> np.ndarray:
    """Taubin-сглаживание (λ|μ): убирает «ступеньки» marching cubes без усадки модели. level=0 -> без изменений."""
    iterations = SMOOTH_ITERATIONS[level]
    if iterations == 0:
        return np.asarray(vertices, dtype=np.float64)
    m = trimesh.Trimesh(np.array(vertices, dtype=np.float64), faces, process=False)
    trimesh.smoothing.filter_taubin(m, lamb=TAUBIN_LAMBDA, nu=TAUBIN_NU, iterations=iterations)
    return np.asarray(m.vertices)


# ─────────────────────────────── подготовка ассетов (один раз, при генерации) ───────────────────────────────
def prepare_post_assets(mesh: trimesh.Trimesh, frame: str, post_dir: Path) -> dict:
    """Сохраняет mesh_raw.ply, UV-атлас, текстуру и meta.json. Возвращает meta."""
    post_dir.mkdir(parents=True, exist_ok=True)
    (post_dir / "variants").mkdir(exist_ok=True)

    vertices = np.asarray(mesh.vertices, dtype=np.float64)
    faces = np.asarray(mesh.faces, dtype=np.int64)
    if signed_volume_negative(vertices, faces):
        log.warning("Грани меша были повёрнуты внутрь — разворачиваем. Если модель при этом зеркальна, "
                    "проверьте порядок осей в isosurface.py (см. check_isosurface.py)")
        faces = faces[:, ::-1].copy()
    rgba = (np.asarray(mesh.visual.vertex_colors, dtype=np.uint8) if mesh.visual.kind == "vertex"
            else np.full((len(vertices), 4), 200, dtype=np.uint8))

    trimesh.Trimesh(vertices, faces, vertex_colors=rgba, process=False).export(post_dir / "mesh_raw.ply")

    a, t = eg.gltf_transform(vertices, frame)
    pv = vertices @ eg.frame_matrix(frame, "print").T
    assets = eg.build_texture_assets(vertices, faces, rgba)
    if assets is not None:
        assets[0].save(post_dir / "atlas.npz")
        Image.fromarray(assets[1]).save(post_dir / "texture.png")

    meta = {
        "frame": frame, "vertices": len(vertices), "faces": len(faces),
        "has_texture": assets is not None,
        "texture_size": int(assets[1].shape[0]) if assets else 0,
        "gltf_a": a.tolist(), "gltf_t": t.tolist(),
        "print_lo": pv.min(axis=0).tolist(), "print_hi": pv.max(axis=0).tolist(),
    }
    (post_dir / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
    return meta


def signed_volume_negative(vertices: np.ndarray, faces: np.ndarray) -> bool:
    return eg.signed_volume(vertices, faces) < 0


# ─────────────────────────────── варианты (по запросу) ───────────────────────────────
def build_variant(post_dir: Path, smoothing: int, fmt: str = "glb", size_mm: float = 100.0) -> Path:
    """Возвращает путь к файлу варианта; строит его, если ещё не в кэше."""
    if not 0 <= smoothing < len(SMOOTH_ITERATIONS):
        raise PipelineError(f"smoothing должен быть от 0 до {len(SMOOTH_ITERATIONS) - 1}")
    if fmt not in ("glb", "stl"):
        raise PipelineError("Формат: glb или stl")
    meta_path = post_dir / "meta.json"
    if not meta_path.is_file():
        raise PipelineError("Для этой модели нет данных пост-обработки")

    name = f"s{smoothing:02d}" + (f"_{int(round(size_mm))}mm" if fmt == "stl" else "")
    out = post_dir / "variants" / f"{name}.{fmt}"
    with _lock_for(str(post_dir)):                          # два одинаковых запроса не строят файл дважды
        if out.is_file():
            return out
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        raw = trimesh.load(post_dir / "mesh_raw.ply", process=False)
        faces = np.asarray(raw.faces, dtype=np.int64)
        verts = smooth_vertices(np.asarray(raw.vertices, dtype=np.float64), faces, smoothing)

        if fmt == "stl":
            eg.write_stl(verts, faces, meta["frame"], (np.array(meta["print_lo"]), np.array(meta["print_hi"])),
                         size_mm, out)
        else:
            a, t = np.array(meta["gltf_a"]), np.array(meta["gltf_t"])
            if meta["has_texture"]:
                atlas = eg.Atlas.load(post_dir / "atlas.npz")
                with Image.open(post_dir / "texture.png") as im:
                    texture = np.asarray(im.convert("RGB"))
                eg.write_textured_glb(verts, faces, atlas, texture, a, t, out)
            else:
                eg.write_vertex_color_glb(verts, faces, np.asarray(raw.visual.vertex_colors, dtype=np.uint8), a, t, out)
        log.info("Вариант %s готов (%d КБ)", out.name, out.stat().st_size // 1024)
        return out


def publish_base_model(post_dir: Path, output_path: Path) -> None:
    """Базовая модель (сглаживание 0) -> model.glb задачи. Файл появляется атомарно."""
    src = build_variant(post_dir, 0, "glb")
    tmp = output_path.with_name(output_path.name + ".tmp")
    shutil.copyfile(src, tmp)
    os.replace(tmp, output_path)
