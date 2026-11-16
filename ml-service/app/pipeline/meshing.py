"""Облако точек -> меш (Open3D, алгоритм Пуассона). Вход/выход — файлы на диске (out-of-core)."""
from __future__ import annotations

import logging
from pathlib import Path

import numpy as np

from app.config import get_settings
from app.errors import PipelineError
from app.schemas import Stage
from app.utils.memory import free_memory, log_stage

log = logging.getLogger(__name__)
MIN_POINTS = 2000


def _voxel_downsample(points, colors, cam_idx, voxel):
    """Воксельное прореживание с усреднением позиций и цветов (чистый numpy, индексы остаются согласованными)."""
    ijk = np.floor((points - points.min(axis=0)) / voxel).astype(np.int64)
    dims = ijk.max(axis=0) + 1
    key = (ijk[:, 0] * dims[1] + ijk[:, 1]) * dims[2] + ijk[:, 2]
    _, first, inverse, counts = np.unique(key, return_index=True, return_inverse=True, return_counts=True)
    n = len(counts)
    out_pts = np.stack([np.bincount(inverse, weights=points[:, c], minlength=n) / counts for c in range(3)], axis=1)
    out_col = np.stack([np.bincount(inverse, weights=colors[:, c].astype(np.float64), minlength=n) / counts
                        for c in range(3)], axis=1)
    return out_pts, out_col, cam_idx[first]


def _adaptive_downsample(points, colors, cam_idx, fraction, max_points):
    diag = float(np.linalg.norm(np.ptp(points, axis=0)))
    if diag <= 0:
        raise PipelineError("Облако точек вырождено (все точки совпадают)")
    voxel = diag * fraction
    for _ in range(12):
        pts, col, cam = _voxel_downsample(points, colors, cam_idx, voxel)
        if len(pts) <= max_points:
            return pts, col, cam, voxel
        voxel *= 1.25                                  # слишком много точек — укрупняем воксель
    return pts, col, cam, voxel


def build_mesh(cloud_raw: Path, work: Path, progress) -> tuple[Path, Path]:
    """
    Возвращает (mesh_geometry.ply, cloud_clean.npz).
    Цвета на этом этапе не нужны: меш — только геометрия, цвет переносится позже (texturing.py).
    """
    import open3d as o3d

    cfg = get_settings()
    data = np.load(cloud_raw)
    points = data["points"].astype(np.float64)
    colors = data["colors"]
    cam_idx = data["cam_idx"]
    cam_centers = data["cam_centers"].astype(np.float64)
    del data

    if len(points) < MIN_POINTS:
        raise PipelineError("Слишком мало надёжных точек для построения модели. "
                            "Снимите объект с разных сторон, при хорошем освещении и на однотонном фоне.")

    # 1) прореживание и чистка выбросов
    progress(Stage.MESH_GENERATION, 52, "Прореживание облака точек...")
    with log_stage("voxel downsample"):
        points, colors, cam_idx, voxel = _adaptive_downsample(
            points, colors, cam_idx, cfg.voxel_fraction, cfg.max_points)
    log.info("После прореживания: %d точек, воксель %.5f", len(points), voxel)

    pcd = o3d.geometry.PointCloud()
    pcd.points = o3d.utility.Vector3dVector(points)
    pcd, kept = pcd.remove_statistical_outlier(nb_neighbors=20, std_ratio=2.0)
    kept = np.asarray(kept)
    points, colors, cam_idx = points[kept], colors[kept], cam_idx[kept]
    if len(points) < MIN_POINTS // 2:
        raise PipelineError("После очистки от шума почти не осталось точек")

    cloud_clean = work / "cloud_clean.npz"             # чекпоинт на диске: понадобится для раскраски
    np.savez(cloud_clean, points=points.astype(np.float32), colors=np.clip(colors, 0, 255).astype(np.uint8))

    # 2) нормали, ориентированные в сторону камеры, которая видела точку
    progress(Stage.MESH_GENERATION, 60, "Оценка нормалей...")
    with log_stage("normals"):
        pcd.estimate_normals(o3d.geometry.KDTreeSearchParamHybrid(radius=voxel * 4, max_nn=30))
        normals = np.asarray(pcd.normals).copy()
        to_cam = cam_centers[cam_idx] - points
        flip = (normals * to_cam).sum(axis=1) < 0
        normals[flip] *= -1.0
        pcd.normals = o3d.utility.Vector3dVector(normals)

    # 3) Пуассон
    progress(Stage.MESH_GENERATION, 66, f"Реконструкция поверхности (Poisson, depth={cfg.poisson_depth})...")
    with log_stage("poisson"):
        mesh, densities = o3d.geometry.TriangleMesh.create_from_point_cloud_poisson(pcd, depth=cfg.poisson_depth)
    densities = np.asarray(densities)
    mesh.remove_vertices_by_mask(densities < np.quantile(densities, cfg.density_trim))  # срезаем «пузыри» на пустоте
    bbox = pcd.get_axis_aligned_bounding_box()
    mesh = mesh.crop(bbox.scale(1.02, bbox.get_center()))
    del pcd, densities
    free_memory()

    # 4) чистка меша
    progress(Stage.MESH_GENERATION, 78, "Очистка и упрощение меша...")
    mesh.remove_degenerate_triangles()
    mesh.remove_duplicated_triangles()
    mesh.remove_duplicated_vertices()
    mesh.remove_unreferenced_vertices()
    if len(mesh.triangles) == 0:
        raise PipelineError("Не удалось построить поверхность: данных слишком мало")

    # оставляем только крупные связные куски (убираем «летающий мусор»)
    clusters, n_tris, _ = mesh.cluster_connected_triangles()
    clusters, n_tris = np.asarray(clusters), np.asarray(n_tris)
    keep = n_tris >= max(1, 0.05 * n_tris.max())
    mesh.remove_triangles_by_mask(~keep[clusters])
    mesh.remove_unreferenced_vertices()

    if len(mesh.triangles) > cfg.target_triangles:
        mesh = mesh.simplify_quadric_decimation(target_number_of_triangles=cfg.target_triangles)
    mesh.compute_vertex_normals()

    mesh_path = work / "mesh_geometry.ply"
    o3d.io.write_triangle_mesh(str(mesh_path), mesh)
    log.info("Меш: %d вершин, %d треугольников", len(mesh.vertices), len(mesh.triangles))
    return mesh_path, cloud_clean
