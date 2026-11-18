"""
Пост-обработка готовой модели: стек модификаторов как в Blender (Decimate -> Subdivision Surface -> Smoothing),
починка меша, экспорт GLB / STL / PLY.

  tasks/{id}/post/
      mesh_raw.ply   — починенный «сырой» меш в родной СК, цвета вершин. НЕ меняется.
      atlas.npz, texture.png — UV-развёртка и текстура (только для варианта без смены топологии)
      meta.json      — СК, преобразование в glTF, габариты, отчёт о починке
      variants/      — кэш результатов: d0300_u1_s03.glb (+ .json со статистикой)

Любой вариант строится ИЗ mesh_raw.ply заново (не накопительно): повторный запрос — из кэша,
ползунки обратимы. Модификаторы, меняющие топологию (Decimate, Subdivision), работают на СВАРЕННОЙ
сетке и дают GLB с vertex colors: без UV-швов, а значит без «разрывов» при дальнейшей правке в Blender.
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
from app.pipeline import repair

log = logging.getLogger(__name__)

SMOOTH_ITERATIONS = [0, 2, 4, 6, 10, 15, 20, 30, 45, 65, 90]   # уровень 0..10 -> итерации Taubin
TAUBIN_LAMBDA, TAUBIN_NU = 0.5, 0.526
MAX_FACES_AFTER_SUBDIV = 1_500_000
MIN_FACES_AFTER_DECIMATE = 200

_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


def _lock_for(key: str) -> threading.Lock:
    with _locks_guard:
        return _locks.setdefault(key, threading.Lock())


def _arrays(mesh: trimesh.Trimesh):
    v = np.asarray(mesh.vertices, dtype=np.float64)
    f = np.asarray(mesh.faces, dtype=np.int64)
    rgba = (np.asarray(mesh.visual.vertex_colors, dtype=np.uint8) if mesh.visual.kind == "vertex"
            else np.full((len(v), 4), 200, dtype=np.uint8))
    return v, f, rgba


def _to_mesh(v, f, rgba) -> trimesh.Trimesh:
    m = trimesh.Trimesh(v, f, process=False)
    m.visual = trimesh.visual.ColorVisuals(m, vertex_colors=rgba)
    return m


# ──────────────────────────── цвета ────────────────────────────
def transfer_colors(src_vertices, src_rgba, dst_vertices, k: int = 3) -> np.ndarray:
    """Цвета новых вершин = среднее k ближайших вершин исходной сетки (с весом 1/расстояние)."""
    dist, idx = cKDTree(src_vertices).query(dst_vertices, k=k, workers=-1)
    w = 1.0 / (dist + 1e-12)
    w /= w.sum(axis=1, keepdims=True)
    rgb = (src_rgba[idx][..., :3].astype(np.float64) * w[..., None]).sum(axis=1)
    out = np.full((len(dst_vertices), 4), 255, dtype=np.uint8)
    out[:, :3] = np.clip(np.rint(rgb), 0, 255).astype(np.uint8)
    return out


# ──────────────────────────── модификаторы ────────────────────────────
def clean(mesh: trimesh.Trimesh) -> trimesh.Trimesh:
    """Починка (см. repair.py) для вызова из пайплайна."""
    return repair.repair_trimesh(mesh)[0]


def decimate_arrays(v, f, rgba, face_limit: int):
    """Quadric decimation + перенос цветов + повторная починка (после упрощения бывают вырожденные грани)."""
    if face_limit <= 0 or len(f) <= face_limit:
        return v, f, rgba
    src = trimesh.Trimesh(v, f, process=False)
    try:
        try:
            out = src.simplify_quadric_decimation(face_count=face_limit)
        except TypeError:                                     # старые версии trimesh: позиционный аргумент
            out = src.simplify_quadric_decimation(face_limit)
    except ImportError:                                       # нет fast-simplification -> Open3D
        log.warning("fast-simplification не установлен, упрощаем через Open3D")
        import open3d as o3d
        m = o3d.geometry.TriangleMesh(o3d.utility.Vector3dVector(v), o3d.utility.Vector3iVector(f))
        m = m.simplify_quadric_decimation(target_number_of_triangles=face_limit)
        out = trimesh.Trimesh(np.asarray(m.vertices), np.asarray(m.triangles), process=False)
    nv, nf = np.asarray(out.vertices, dtype=np.float64), np.asarray(out.faces, dtype=np.int64)
    nc = transfer_colors(v, rgba, nv)
    nv, nf, nc, _ = repair.repair_mesh(nv, nf, nc, floater_ratio=0.0)
    log.info("Decimate: %d -> %d граней", len(f), len(nf))
    return nv, nf, nc


def decimate(mesh: trimesh.Trimesh, face_limit: int) -> trimesh.Trimesh:
    """Лимит полигонов при генерации (0 = без лимита)."""
    if face_limit <= 0 or len(mesh.faces) <= face_limit:
        return mesh
    return _to_mesh(*decimate_arrays(*_arrays(mesh), face_limit))


def subdivide_arrays(v, f, rgba, levels: int):
    """Subdivision Surface (Loop — аналог Catmull-Clark для треугольников): x4 полигонов за уровень."""
    if len(f) * 4 ** levels > MAX_FACES_AFTER_SUBDIV:
        raise PipelineError(f"После Subdivision получилось бы {len(f) * 4 ** levels:,} полигонов "
                            f"(максимум {MAX_FACES_AFTER_SUBDIV:,}). Сначала уменьшите модель Decimate.".replace(",", " "))
    if hasattr(trimesh.remesh, "subdivide_loop"):
        nv, nf = trimesh.remesh.subdivide_loop(v, f, iterations=levels)
    else:                                                     # запасной вариант: деление по серединам + сглаживание
        nv, nf = v, f
        for _ in range(levels):
            nv, nf = trimesh.remesh.subdivide(nv, nf)
        nv = smooth_vertices(nv, nf, 3)
    return np.asarray(nv, dtype=np.float64), np.asarray(nf, dtype=np.int64), transfer_colors(v, rgba, nv)


def smooth_vertices(vertices, faces, level: int) -> np.ndarray:
    """Taubin (λ|μ): убирает «ступеньки» marching cubes без усадки. level=0 — без изменений."""
    iterations = SMOOTH_ITERATIONS[level]
    if iterations == 0:
        return np.asarray(vertices, dtype=np.float64)
    m = trimesh.Trimesh(np.array(vertices, dtype=np.float64), faces, process=False)
    trimesh.smoothing.filter_taubin(m, lamb=TAUBIN_LAMBDA, nu=TAUBIN_NU, iterations=iterations)
    return np.asarray(m.vertices)


# ──────────────────────────── подготовка (один раз, при генерации) ────────────────────────────
def prepare_post_assets(mesh: trimesh.Trimesh, frame: str, post_dir: Path) -> dict:
    post_dir.mkdir(parents=True, exist_ok=True)
    (post_dir / "variants").mkdir(exist_ok=True)

    v, f, rgba = _arrays(mesh)
    v, f, rgba, report = repair.repair_mesh(v, f, rgba)
    log.info("Починка: %s", report)
    if report["flipped_components"]:
        log.warning("Грани были повёрнуты внутрь — исправлено. Если модель при этом зеркальна, "
                    "проверьте isosurface.py (tools/check_isosurface.py)")

    trimesh.Trimesh(v, f, vertex_colors=rgba, process=False).export(post_dir / "mesh_raw.ply")
    a, t = eg.gltf_transform(v, frame)
    pv = v @ eg.frame_matrix(frame, "print").T
    assets = eg.build_texture_assets(v, f, rgba)
    if assets is not None:
        assets[0].save(post_dir / "atlas.npz")
        Image.fromarray(assets[1]).save(post_dir / "texture.png")

    meta = {"frame": frame, "vertices": len(v), "faces": len(f), "has_texture": assets is not None,
            "texture_size": int(assets[1].shape[0]) if assets else 0, "repair": report,
            "gltf_a": a.tolist(), "gltf_t": t.tolist(),
            "print_lo": pv.min(axis=0).tolist(), "print_hi": pv.max(axis=0).tolist()}
    (post_dir / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
    return meta


# ──────────────────────────── варианты (по запросу) ────────────────────────────
def build_variant(post_dir: Path, *, smoothing: int = 0, decimate_ratio: float = 1.0, subdivision: int = 0,
                  fmt: str = "glb", size_mm: float = 100.0) -> tuple[Path, dict]:
    """Возвращает (путь к файлу, статистика). Строит вариант, если его ещё нет в кэше."""
    if not 0 <= smoothing < len(SMOOTH_ITERATIONS):
        raise PipelineError(f"smoothing: от 0 до {len(SMOOTH_ITERATIONS) - 1}")
    if not 0.02 <= decimate_ratio <= 1.0:
        raise PipelineError("decimate_ratio: от 0.02 до 1.0")
    if subdivision not in (0, 1, 2):
        raise PipelineError("subdivision: 0, 1 или 2")
    if fmt not in ("glb", "stl", "ply"):
        raise PipelineError("Формат: glb, stl или ply")
    meta_path = post_dir / "meta.json"
    if not meta_path.is_file():
        raise PipelineError("Для этой модели нет данных пост-обработки")

    ratio_key = int(round(decimate_ratio * 1000))
    name = f"d{ratio_key:04d}_u{subdivision}_s{smoothing:02d}" + (f"_{int(round(size_mm))}mm" if fmt != "glb" else "")
    out, info = post_dir / "variants" / f"{name}.{fmt}", post_dir / "variants" / f"{name}.json"

    with _lock_for(str(post_dir)):                            # одинаковые запросы не строят файл дважды
        if out.is_file() and info.is_file():
            return out, json.loads(info.read_text(encoding="utf-8"))

        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        raw = trimesh.load(post_dir / "mesh_raw.ply", process=False)
        v, f, rgba = _arrays(raw)
        topology_changed = ratio_key < 1000 or subdivision > 0
        if ratio_key < 1000:
            v, f, rgba = decimate_arrays(v, f, rgba, max(MIN_FACES_AFTER_DECIMATE, int(len(f) * ratio_key / 1000)))
        if subdivision:
            v, f, rgba = subdivide_arrays(v, f, rgba, subdivision)
        v = smooth_vertices(v, f, smoothing)

        textured = False
        if fmt == "glb":
            a, t = np.array(meta["gltf_a"]), np.array(meta["gltf_t"])
            if not topology_changed and meta["has_texture"]:
                atlas = eg.Atlas.load(post_dir / "atlas.npz")
                with Image.open(post_dir / "texture.png") as im:
                    texture = np.asarray(im.convert("RGB"))
                eg.write_textured_glb(v, f, atlas, texture, a, t, out)
                textured = True
            else:
                eg.write_vertex_color_glb(v, f, rgba, a, t, out)
        else:
            bounds = (np.array(meta["print_lo"]), np.array(meta["print_hi"]))
            if fmt == "stl":
                eg.write_stl(v, f, meta["frame"], bounds, size_mm, out)
            else:
                eg.write_ply(v, f, rgba, meta["frame"], bounds, size_mm, out)

        stats = {"vertices": int(len(v)), "faces": int(len(f)), "base_faces": int(meta["faces"]),
                 "textured": textured, "repair": meta.get("repair", {})}
        info.write_text(json.dumps(stats), encoding="utf-8")
        log.info("Вариант %s: %d граней, %d КБ", out.name, len(f), out.stat().st_size // 1024)
        return out, stats


def publish_base_model(post_dir: Path, output_path: Path) -> None:
    """Базовая модель -> model.glb задачи (атомарно)."""
    src, _ = build_variant(post_dir)
    tmp = output_path.with_name(output_path.name + ".tmp")
    shutil.copyfile(src, tmp)
    os.replace(tmp, output_path)
