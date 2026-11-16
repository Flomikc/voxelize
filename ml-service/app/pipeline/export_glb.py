"""Экспорт в .glb: ориентация осей, нормализация размера, корректный материал, атомарная запись."""
from __future__ import annotations

import json
import os
import struct
from pathlib import Path

import numpy as np
import trimesh

_JSON_CHUNK = 0x4E4F534A  # 'JSON'


def opencv_to_gltf_axes(mesh: trimesh.Trimesh) -> trimesh.Trimesh:
    """VGGT/OpenCV: x вправо, y вниз, z вперёд. glTF: y вверх. Поворот на 180° вокруг X (det = +1)."""
    mesh.apply_transform(trimesh.transformations.rotation_matrix(np.pi, [1, 0, 0]))
    return mesh


def triposr_to_gltf_axes(mesh: trimesh.Trimesh) -> trimesh.Trimesh:
    """Та же коррекция ориентации, что в официальном gradio-демо TripoSR."""
    mesh.apply_transform(trimesh.transformations.rotation_matrix(-np.pi / 2, [1, 0, 0]))
    mesh.apply_transform(trimesh.transformations.rotation_matrix(np.pi / 2, [0, 1, 0]))
    return mesh


def normalize_mesh(mesh: trimesh.Trimesh, size: float = 1.0) -> trimesh.Trimesh:
    """Центрирует по bbox и масштабирует так, чтобы наибольшая сторона = size."""
    lo, hi = mesh.bounds
    extent = float((hi - lo).max())
    if extent <= 0:
        return mesh
    mesh.apply_translation(-(lo + hi) / 2.0)
    mesh.apply_scale(size / extent)
    return mesh


def _patch_glb_material(glb: bytes) -> bytes:
    """
    trimesh не пишет материал для мешей с vertex colors, а по спецификации glTF
    материал по умолчанию — металл (metallicFactor=1), из-за чего модель выглядит тёмной.
    Явно задаём матовый PBR-материал с белым baseColor (цвет берётся из COLOR_0).
    """
    magic, version, _ = struct.unpack_from("<4sII", glb, 0)
    if magic != b"glTF":
        raise ValueError("Не GLB-файл")
    json_len, json_type = struct.unpack_from("<II", glb, 12)
    if json_type != _JSON_CHUNK:
        raise ValueError("Первый чанк GLB не JSON")
    gltf = json.loads(glb[20:20 + json_len])
    rest = glb[20 + json_len:]                      # BIN-чанк (с заголовком) без изменений

    gltf["materials"] = [{
        "name": "vertex_color_material",
        "pbrMetallicRoughness": {
            "baseColorFactor": [1.0, 1.0, 1.0, 1.0],
            "metallicFactor": 0.0,
            "roughnessFactor": 0.85,
        },
        "doubleSided": True,
    }]
    for m in gltf.get("meshes", []):
        for prim in m.get("primitives", []):
            prim["material"] = 0

    new_json = json.dumps(gltf, separators=(",", ":")).encode("utf-8")
    new_json += b" " * ((4 - len(new_json) % 4) % 4)  # JSON-чанк выравнивается пробелами до 4 байт
    total = 12 + 8 + len(new_json) + len(rest)
    return (struct.pack("<4sII", b"glTF", version, total)
            + struct.pack("<II", len(new_json), _JSON_CHUNK)
            + new_json + rest)


def export_glb(mesh: trimesh.Trimesh, out_path: Path) -> None:
    """Пишет GLB во временный файл и атомарно переименовывает — model.glb никогда не бывает «половинным»."""
    _ = mesh.vertex_normals                          # прогреть кэш: нормали попадут в GLB (гладкое освещение)
    data = _patch_glb_material(mesh.export(file_type="glb"))
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = out_path.with_name(out_path.name + ".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, out_path)
