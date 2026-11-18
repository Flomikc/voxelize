"""
Экспорт меша в .glb для <model-viewer> (проект Voxelize).

Модуль делает пять вещей:
  1. Ориентация: «родная» система координат модели -> glTF (Y вверх, лицо смотрит на +Z).
  2. UV-развёртка (xatlas) и запекание цветов вершин в текстуру: текстура отображается везде,
     в отличие от COLOR_0, который <model-viewer> может проигнорировать.
  3. Запасной вариант (vertex colors + явный материал), если xatlas недоступен, упал или сетка слишком большая.
  4. STL для 3D-печати (Z вверх, миллиметры, модель стоит на столе).
  5. Низкоуровневые «кирпичики» для пост-обработки (postprocess.py): атлас строится ОДИН раз,
     потом используется для любого уровня сглаживания — топология при сглаживании не меняется.

xatlas запускается в ОТДЕЛЬНОМ ПРОЦЕССЕ: это нативная библиотека, и при падении (segfault)
она не должна убивать весь ml-service вместе с очередью задач. Заодно получаем таймаут.
"""
from __future__ import annotations

import json
import logging
import os
import struct
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import trimesh
from PIL import Image
from scipy.ndimage import distance_transform_edt

from app.config import get_settings

log = logging.getLogger(__name__)
_JSON_CHUNK = 0x4E4F534A  # 'JSON'


# ══════════════════════════════════════════════════════════════════════════════
# 1. ОРИЕНТАЦИЯ
# ══════════════════════════════════════════════════════════════════════════════
# «Родные» системы координат:
#   triposr: Z вверх; лицо объекта смотрит в -X; +Y — правая сторона объекта (правая тройка).
#   opencv : (VGGT) X вправо, Y вниз, Z вперёд.
# Цель (glTF / three.js / model-viewer): Y вверх, лицо смотрит на камеру, т.е. на +Z.
#
# Для TripoSR матрица одна (и это то же, что "поворот на -90° вокруг X, затем на +90° вокруг Y"):
#
#          родные (x, y, z)  ->  glTF (x', y', z')          ┌ 0 -1  0 ┐
#   x' = -y    (право объекта  -> влево на экране)           │ 0  0  1 │  det = +1 (чистый поворот)
#   y' =  z    (верх           -> вверх)                     └-1  0  0 ┘
#   z' = -x    (лицо           -> на камеру)
#
# Матрицу задаёт строка "-y,z,-x": i-я компонента результата = указанная родная ось со знаком.
# Если после исправления marching cubes модель всё ещё стоит не так, подберите TRIPOSR_AXES:
#   стоит вертикально, но смотрит ВЛЕВО   -> "x,z,-y"
#   стоит вертикально, но смотрит ВПРАВО  -> "-x,z,y"
#   стоит вертикально, но смотрит ОТ нас  -> "y,z,x"
#   всё верно (по умолчанию)              -> "-y,z,-x"
_FRAME_SPECS = {
    "triposr": {"gltf": None, "print": "x,y,z"},          # gltf: из TRIPOSR_AXES; печать: Z уже вверх
    "opencv": {"gltf": "x,-y,-z", "print": "x,z,-y"},
}


def parse_axes(spec: str) -> np.ndarray:
    """'-y,z,-x' -> матрица 3x3 (new = R @ native). Зеркальные варианты (det=-1) запрещены."""
    tokens = [t.strip().lower() for t in spec.split(",")]
    if len(tokens) != 3:
        raise ValueError(f"Ожидалось 3 оси через запятую, получено: «{spec}»")
    rot = np.zeros((3, 3))
    for i, tok in enumerate(tokens):
        axis = tok.lstrip("+-")
        if axis not in ("x", "y", "z"):
            raise ValueError(f"Неизвестная ось «{tok}» (допустимо: x, y, z со знаком)")
        rot[i, "xyz".index(axis)] = -1.0 if tok.startswith("-") else 1.0
    det = np.linalg.det(rot)
    if abs(det) < 0.5:
        raise ValueError(f"Оси не должны повторяться: «{spec}»")
    if det < 0:
        raise ValueError(f"«{spec}» — зеркальное отражение (det=-1): модель была бы зеркальной. "
                         "Смените знак у одной из осей.")
    return rot


def frame_matrix(frame: str, purpose: str = "gltf") -> np.ndarray:
    spec = _FRAME_SPECS[frame][purpose]
    if spec is None:
        spec = get_settings().triposr_axes
    return parse_axes(spec)


def gltf_transform(vertices: np.ndarray, frame: str, target: float = 1.0) -> tuple[np.ndarray, np.ndarray]:
    """
    Аффинное преобразование родная СК -> glTF: поворот + центрирование по bbox + масштаб (наибольший габарит = target).
    Считается ОДИН раз по исходной сетке и сохраняется — все варианты сглаживания получают одинаковый масштаб.
    Возвращает (A, t): v_gltf = v @ A.T + t.
    """
    rot = frame_matrix(frame, "gltf")
    rotated = vertices @ rot.T
    lo, hi = rotated.min(axis=0), rotated.max(axis=0)
    extent = float((hi - lo).max())
    scale = target / extent if extent > 0 else 1.0
    return rot * scale, -(lo + hi) / 2.0 * scale


def apply_transform(vertices: np.ndarray, a: np.ndarray, t: np.ndarray) -> np.ndarray:
    return vertices @ a.T + t


def signed_volume(vertices: np.ndarray, faces: np.ndarray) -> float:
    """Знак < 0 означает, что грани повёрнуты внутрь (нормали смотрят в объект)."""
    v0, v1, v2 = vertices[faces[:, 0]], vertices[faces[:, 1]], vertices[faces[:, 2]]
    return float(np.einsum("ij,ij->i", v0, np.cross(v1, v2)).sum() / 6.0)


# Совместимость со старым кодом (multi_view.py): преобразования «на месте» для trimesh.Trimesh
def _apply_rotation(mesh: trimesh.Trimesh, rot: np.ndarray) -> trimesh.Trimesh:
    m = np.eye(4)
    m[:3, :3] = rot
    mesh.apply_transform(m)
    return mesh


def opencv_to_gltf_axes(mesh: trimesh.Trimesh) -> trimesh.Trimesh:
    return _apply_rotation(mesh, frame_matrix("opencv", "gltf"))


def triposr_to_gltf_axes(mesh: trimesh.Trimesh) -> trimesh.Trimesh:
    return _apply_rotation(mesh, frame_matrix("triposr", "gltf"))


def normalize_mesh(mesh: trimesh.Trimesh, size: float = 1.0) -> trimesh.Trimesh:
    lo, hi = mesh.bounds
    extent = float((hi - lo).max())
    if extent > 0:
        mesh.apply_translation(-(lo + hi) / 2.0)
        mesh.apply_scale(size / extent)
    return mesh


# ══════════════════════════════════════════════════════════════════════════════
# 2. UV-АТЛАС (xatlas, в подпроцессе) И ЗАПЕКАНИЕ ТЕКСТУРЫ
# ══════════════════════════════════════════════════════════════════════════════
class AtlasError(RuntimeError):
    """UV-развёртка не удалась (xatlas упал, не установлен или не уложился во время)."""


@dataclass
class Atlas:
    vmapping: np.ndarray   # (V',) новая вершина -> индекс исходной вершины (шовные вершины дублируются)
    faces: np.ndarray      # (F, 3) индексы новых вершин
    uvs: np.ndarray        # (V', 2) float32 в [0, 1]; v направлена ВВЕРХ (как в OpenGL / trimesh)

    def save(self, path: Path) -> None:
        np.savez(path, vmapping=self.vmapping.astype(np.uint32), faces=self.faces.astype(np.uint32), uvs=self.uvs)

    @staticmethod
    def load(path: Path) -> "Atlas":
        d = np.load(path)
        return Atlas(d["vmapping"].astype(np.int64), d["faces"].astype(np.int64), d["uvs"].astype(np.float32))


_XATLAS_SNIPPET = r"""
import sys, numpy as np, xatlas
inp, out, resolution = sys.argv[1], sys.argv[2], int(sys.argv[3])
d = np.load(inp)
atlas = xatlas.Atlas()
atlas.add_mesh(d["V"], d["F"])
chart, pack = xatlas.ChartOptions(), xatlas.PackOptions()
chart.max_iterations = 1          # быстрее, качество для vertex-color-текстуры не страдает
pack.resolution = resolution
pack.padding = 2
atlas.generate(chart, pack)
vmapping, faces, uvs = atlas[0]
np.savez(out, vmapping=vmapping, faces=faces, uvs=uvs)
"""


def _run_xatlas(vertices: np.ndarray, faces: np.ndarray, resolution: int, timeout: int) -> Atlas:
    with tempfile.TemporaryDirectory(prefix="xatlas_") as tmp:
        inp, out = Path(tmp) / "in.npz", Path(tmp) / "out.npz"
        np.savez(inp, V=np.ascontiguousarray(vertices, dtype=np.float32),
                 F=np.ascontiguousarray(faces, dtype=np.uint32))
        try:
            proc = subprocess.run([sys.executable, "-c", _XATLAS_SNIPPET, str(inp), str(out), str(resolution)],
                                  capture_output=True, timeout=timeout)
        except subprocess.TimeoutExpired as e:
            raise AtlasError(f"xatlas не уложился в {timeout} с") from e
        if proc.returncode != 0 or not out.is_file():
            tail = proc.stderr.decode(errors="replace").strip()[-300:]
            raise AtlasError(f"xatlas завершился с кодом {proc.returncode}: {tail or 'нет вывода'}")
        return Atlas.load(out)


def pick_texture_size(n_vertices: int) -> int:
    """Источник цвета — вершины, поэтому текстура больше ~3 текселей на «шаг» вершин смысла не имеет."""
    cfg = get_settings()
    if cfg.texture_size > 0:
        return int(cfg.texture_size)
    target = 3.0 * np.sqrt(max(n_vertices, 1))
    size = 512
    while size < target and size < 2048:
        size *= 2
    return size


def bake_texture(atlas: Atlas, vertex_rgba: np.ndarray, size: int, density: float = 4.0,
                 max_samples: int = 12_000_000, chunk: int = 1_000_000, seed: int = 0) -> np.ndarray:
    """
    Запекает цвета вершин в текстуру size x size (uint8, RGB, строка 0 = верх изображения).

    Метод: на каждый треугольник в UV-пространстве кидаем случайные точки (≈density штук на пиксель),
    цвет точки = барицентрическая интерполяция цветов вершин, пиксель = среднее попавших точек.
    Всё векторизовано numpy и идёт порциями — пиковая память ограничена. Пустые пиксели между островами
    заполняются ближайшими цветами (padding), чтобы не было «просветов» на швах при фильтрации.
    """
    rng = np.random.default_rng(seed)
    colors = vertex_rgba[atlas.vmapping][:, :3].astype(np.float32)               # цвета новых вершин
    uv_px = atlas.uvs.astype(np.float32) * size
    uv_px[:, 1] = size - uv_px[:, 1]          # v вверх (OpenGL) -> строка изображения сверху вниз
    tri = uv_px[atlas.faces]                                                      # (F, 3, 2)
    tri_col = colors[atlas.faces]                                                 # (F, 3, 3)

    ab, ac = tri[:, 1] - tri[:, 0], tri[:, 2] - tri[:, 0]
    area = 0.5 * np.abs(ab[:, 0] * ac[:, 1] - ab[:, 1] * ac[:, 0])               # площадь в пикселях²
    n = np.maximum(1, np.ceil(area * density)).astype(np.int64)
    if n.sum() > max_samples:
        n = np.maximum(1, np.ceil(n * (max_samples / n.sum()))).astype(np.int64)
    cum = np.cumsum(n)

    acc = np.zeros((3, size * size), dtype=np.float32)
    cnt = np.zeros(size * size, dtype=np.int32)
    start, n_faces = 0, len(n)
    while start < n_faces:
        base = int(cum[start - 1]) if start else 0
        end = max(int(np.searchsorted(cum, base + chunk, side="right")), start + 1)
        fi = np.repeat(np.arange(start, end), n[start:end])
        r1 = np.sqrt(rng.random(len(fi), dtype=np.float32))
        r2 = rng.random(len(fi), dtype=np.float32)
        w = np.stack([1.0 - r1, r1 * (1.0 - r2), r1 * r2], axis=1)               # барицентрика (равномерная)
        p = np.einsum("mk,mkc->mc", w, tri[fi])
        col = np.einsum("mk,mkc->mc", w, tri_col[fi])
        flat = (np.clip(p[:, 1].astype(np.int64), 0, size - 1) * size
                + np.clip(p[:, 0].astype(np.int64), 0, size - 1))
        cnt += np.bincount(flat, minlength=size * size).astype(np.int32)
        for ch in range(3):
            acc[ch] += np.bincount(flat, weights=col[:, ch], minlength=size * size).astype(np.float32)
        start = end

    filled = cnt > 0
    if not filled.any():
        raise AtlasError("Не удалось запечь текстуру: пустая UV-развёртка")
    tex = acc / np.maximum(cnt, 1)
    empty = ~filled.reshape(size, size)
    if empty.any():                                                               # padding: ближайший заполненный пиксель
        iy, ix = distance_transform_edt(empty, return_distances=False, return_indices=True)
        tex = tex.reshape(3, size, size)[:, iy, ix]
    return np.clip(np.rint(tex.reshape(3, size, size)), 0, 255).astype(np.uint8).transpose(1, 2, 0)


def build_texture_assets(vertices: np.ndarray, faces: np.ndarray, vertex_rgba: np.ndarray):
    """(Atlas, texture) либо None, если нужно откатиться на vertex colors (причина пишется в лог)."""
    cfg = get_settings()
    if len(faces) > cfg.uv_max_faces:
        log.warning("Граней %d > UV_MAX_FACES=%d: UV-развёртка пропущена, используем vertex colors",
                    len(faces), cfg.uv_max_faces)
        return None
    size = pick_texture_size(len(vertices))
    try:
        atlas = _run_xatlas(vertices, faces, size, cfg.uv_timeout_s)
        return atlas, bake_texture(atlas, vertex_rgba, size)
    except (AtlasError, MemoryError) as e:
        log.warning("UV-текстура недоступна (%s) — используем vertex colors", e)
        return None


# ══════════════════════════════════════════════════════════════════════════════
# 3. ЗАПИСЬ GLB / STL
# ══════════════════════════════════════════════════════════════════════════════
def _atomic_write(path: Path, data: bytes) -> None:
    """Пишем во временный файл и атомарно переименовываем: читатель не увидит «половинный» файл."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)


def write_textured_glb(vertices_native: np.ndarray, base_faces: np.ndarray, atlas: Atlas,
                       texture: np.ndarray, a: np.ndarray, t: np.ndarray, out_path: Path) -> None:
    """GLB с UV-текстурой. vertices_native — вершины ИСХОДНОЙ сетки (после сглаживания), порядок как у base_faces."""
    v_gltf = apply_transform(np.asarray(vertices_native, dtype=np.float64), a, t)
    # Нормали считаем на исходной (несшитой) сетке и раздаём по дублированным шовным вершинам —
    # иначе вдоль UV-швов появятся видимые полосы освещения.
    normals = trimesh.Trimesh(v_gltf, base_faces, process=False).vertex_normals[atlas.vmapping]
    mesh = trimesh.Trimesh(v_gltf[atlas.vmapping], atlas.faces, vertex_normals=normals, process=False)
    material = trimesh.visual.material.PBRMaterial(
        baseColorTexture=Image.fromarray(texture), metallicFactor=0.0, roughnessFactor=0.9, doubleSided=True)
    mesh.visual = trimesh.visual.TextureVisuals(uv=atlas.uvs, material=material)
    _atomic_write(out_path, mesh.export(file_type="glb"))


def _patch_glb_material(glb: bytes) -> bytes:
    """
    trimesh не пишет материал для мешей с vertex colors, а по спецификации glTF материал по умолчанию —
    металл (metallicFactor=1), и модель выглядит тёмной. Явно задаём матовый PBR-материал.
    """
    magic, version, _ = struct.unpack_from("<4sII", glb, 0)
    json_len, json_type = struct.unpack_from("<II", glb, 12)
    if magic != b"glTF" or json_type != _JSON_CHUNK:
        raise ValueError("Некорректный GLB")
    gltf = json.loads(glb[20:20 + json_len])
    rest = glb[20 + json_len:]
    gltf["materials"] = [{"name": "vertex_color_material", "doubleSided": True,
                          "pbrMetallicRoughness": {"baseColorFactor": [1, 1, 1, 1],
                                                   "metallicFactor": 0.0, "roughnessFactor": 0.85}}]
    for m in gltf.get("meshes", []):
        for prim in m.get("primitives", []):
            prim["material"] = 0
    new_json = json.dumps(gltf, separators=(",", ":")).encode("utf-8")
    new_json += b" " * ((4 - len(new_json) % 4) % 4)
    total = 12 + 8 + len(new_json) + len(rest)
    return (struct.pack("<4sII", b"glTF", version, total)
            + struct.pack("<II", len(new_json), _JSON_CHUNK) + new_json + rest)


def write_vertex_color_glb(vertices_native: np.ndarray, faces: np.ndarray, vertex_rgba: np.ndarray,
                           a: np.ndarray, t: np.ndarray, out_path: Path) -> None:
    """Запасной вариант без текстуры: COLOR_0 + явный материал."""
    mesh = trimesh.Trimesh(apply_transform(np.asarray(vertices_native, dtype=np.float64), a, t),
                           faces, process=False)
    mesh.visual = trimesh.visual.ColorVisuals(mesh, vertex_colors=vertex_rgba)
    _ = mesh.vertex_normals
    _atomic_write(out_path, _patch_glb_material(mesh.export(file_type="glb")))


def write_stl(vertices_native: np.ndarray, faces: np.ndarray, frame: str,
              print_bounds: tuple[np.ndarray, np.ndarray], size_mm: float, out_path: Path) -> None:
    """
    STL для печати: Z вверх, миллиметры, центр по XY, низ модели ровно на Z=0 (на столе).
    Масштаб берётся от ИСХОДНОЙ сетки (print_bounds), поэтому все уровни сглаживания одного размера;
    сдвиг — от текущей (сглаженной) сетки, чтобы она всегда касалась стола.
    Без текстур и цветов: формат их не поддерживает.
    """
    lo0, hi0 = print_bounds
    scale = size_mm / float((hi0 - lo0).max())
    v = np.asarray(vertices_native, dtype=np.float64) @ frame_matrix(frame, "print").T
    lo, hi = v.min(axis=0), v.max(axis=0)
    v = (v - np.array([(lo[0] + hi[0]) / 2.0, (lo[1] + hi[1]) / 2.0, lo[2]])) * scale
    _atomic_write(out_path, trimesh.Trimesh(v, faces, process=False).export(file_type="stl"))


def write_ply(vertices_native: np.ndarray, faces: np.ndarray, vertex_rgba: np.ndarray, frame: str,
              print_bounds: tuple[np.ndarray, np.ndarray], size_mm: float, out_path: Path) -> None:
    """
    PLY с цветами вершин для Blender: Z вверх (как в Blender), миллиметры, на Z=0. Вершины СВАРЕНЫ (без UV-швов),
    поэтому Decimate / Remesh в Blender не рвут сетку. Импорт: File > Import > Stanford (.ply).
    """
    lo0, hi0 = print_bounds
    scale = size_mm / float((hi0 - lo0).max())
    v = np.asarray(vertices_native, dtype=np.float64) @ frame_matrix(frame, "print").T
    lo, hi = v.min(axis=0), v.max(axis=0)
    v = (v - np.array([(lo[0] + hi[0]) / 2.0, (lo[1] + hi[1]) / 2.0, lo[2]])) * scale
    mesh = trimesh.Trimesh(v, faces, vertex_colors=vertex_rgba, process=False)
    _atomic_write(out_path, mesh.export(file_type="ply"))


# ══════════════════════════════════════════════════════════════════════════════
# 4. ВЫСОКОУРОВНЕВЫЙ ЭКСПОРТ (совместимость с multi_view.py: меш уже в системе glTF)
# ══════════════════════════════════════════════════════════════════════════════
def export_glb(mesh: trimesh.Trimesh, out_path: Path) -> None:
    vertices = np.asarray(mesh.vertices, dtype=np.float64)
    faces = np.asarray(mesh.faces, dtype=np.int64)
    if mesh.visual.kind == "vertex":
        rgba = np.asarray(mesh.visual.vertex_colors, dtype=np.uint8)
    else:
        rgba = np.full((len(vertices), 4), 200, dtype=np.uint8)
    ident, zero = np.eye(3), np.zeros(3)
    assets = build_texture_assets(vertices, faces, rgba)
    if assets is not None:
        write_textured_glb(vertices, faces, assets[0], assets[1], ident, zero, Path(out_path))
    else:
        write_vertex_color_glb(vertices, faces, rgba, ident, zero, Path(out_path))
