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
    mesh.apply_transform(trimesh.transformations.rotation_matrix(np.pi, [1, 0, 0]))
    return mesh

def triposr_to_gltf_axes(mesh: trimesh.Trimesh) -> trimesh.Trimesh:
    # 1. Поднимаем модель на "ноги" 
    # Если модель перевернута вверх ногами, поменяй np.pi / 2 на -np.pi / 2
    mesh.apply_transform(trimesh.transformations.rotation_matrix(np.pi / 2, [1, 0, 0]))
    
    # 2. Разворачиваем лицом к камере (если смотрит затылком, поменяй на 0)
    mesh.apply_transform(trimesh.transformations.rotation_matrix(np.pi, [0, 1, 0]))
    return mesh

def normalize_mesh(mesh: trimesh.Trimesh, size: float = 1.0) -> trimesh.Trimesh:
    lo, hi = mesh.bounds
    extent = float((hi - lo).max())
    if extent <= 0:
        return mesh
    mesh.apply_translation(-(lo + hi) / 2.0)
    mesh.apply_scale(size / extent)
    return mesh

def _patch_glb_material(glb: bytes) -> bytes:
    magic, version, _ = struct.unpack_from("<4sII", glb, 0)
    if magic != b"glTF":
        raise ValueError("Не GLB-файл")
    json_len, json_type = struct.unpack_from("<II", glb, 12)
    if json_type != _JSON_CHUNK:
        raise ValueError("Первый чанк GLB не JSON")
    gltf = json.loads(glb[20:20 + json_len])
    rest = glb[20 + json_len:]                      # BIN-чанк

    # Включаем KHR_materials_unlit. Это заставит model-viewer показывать ЧИСТЫЕ цвета вершин,
    # без наложения теней, которые делают модель серой.
    gltf["materials"] = [{
        "name": "vertex_color_material",
        "pbrMetallicRoughness": {
            "baseColorFactor": [1.0, 1.0, 1.0, 1.0],
        },
        "doubleSided": True,
        "alphaMode": "OPAQUE",
        "extensions": {
            "KHR_materials_unlit": {}
        }
    }]
    
    gltf.setdefault("extensionsUsed", []).append("KHR_materials_unlit")
    
    # Привязываем этот материал ко всем частям модели
    for m in gltf.get("meshes", []):
        for prim in m.get("primitives", []):
            prim["material"] = 0

    new_json = json.dumps(gltf, separators=(",", ":")).encode("utf-8")
    # Выравнивание байтов (Padding), чтобы Blender не ломался
    new_json += b" " * ((4 - len(new_json) % 4) % 4) 
    total = 12 + 8 + len(new_json) + len(rest)
    
    return (struct.pack("<4sII", b"glTF", version, total)
            + struct.pack("<II", len(new_json), _JSON_CHUNK)
            + new_json + rest)

def export_glb(mesh: trimesh.Trimesh, out_path: Path) -> None:
    # 1. ФИКС ЦВЕТОВ для GLB (переводим float 0.0-1.0 в uint8 0-255)
    if hasattr(mesh.visual, 'vertex_colors') and mesh.visual.vertex_colors is not None:
        colors = mesh.visual.vertex_colors
        if colors.dtype in (np.float32, np.float64):
            mesh.visual.vertex_colors = (colors * 255).astype(np.uint8)

    # 2. Экспорт и патч (не задаем материал вручную, чтобы trimesh не потерял цвета)
    glb_data = mesh.export(file_type="glb")
    data = _patch_glb_material(glb_data)
    
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = out_path.with_name(out_path.name + ".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, out_path)