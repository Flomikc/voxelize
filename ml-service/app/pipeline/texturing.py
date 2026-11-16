"""
Цвет для меша. MVP использует vertex colors (их корректно показывает <model-viewer>).
Для multi-view цвет берётся из облака точек, из которого построен меш.
(UV-атлас через xatlas + запекание — возможное расширение второго этапа.)
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import trimesh
from scipy.spatial import cKDTree


def colorize_mesh(mesh_path: Path, cloud_path: Path, k: int = 4) -> trimesh.Trimesh:
    """Для каждой вершины меша — взвешенное по расстоянию среднее цветов k ближайших точек облака."""
    mesh = trimesh.load(mesh_path, process=False)
    cloud = np.load(cloud_path)
    points, colors = cloud["points"], cloud["colors"].astype(np.float64)

    tree = cKDTree(points)
    dist, idx = tree.query(np.asarray(mesh.vertices), k=k, workers=-1)
    w = 1.0 / (dist + 1e-9)
    w /= w.sum(axis=1, keepdims=True)
    vertex_rgb = (colors[idx] * w[..., None]).sum(axis=1)

    rgba = np.empty((len(vertex_rgb), 4), dtype=np.uint8)
    rgba[:, :3] = np.clip(vertex_rgb, 0, 255).astype(np.uint8)
    rgba[:, 3] = 255
    mesh.visual = trimesh.visual.ColorVisuals(mesh, vertex_colors=rgba)
    return mesh
