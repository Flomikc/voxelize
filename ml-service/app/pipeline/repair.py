"""
Починка меша — те же шаги, что делают вручную в Blender перед Decimate / печатью:

  Merge by Distance      сваривает дубликаты вершин (главная причина «разрывов»: у GLB с UV-развёрткой
                         вершины на швах продублированы, и Decimate рвёт сетку именно по швам)
  Degenerate Dissolve    вырожденные и дублирующиеся грани
  Select Non-Manifold    грани на рёбрах, к которым примыкает больше двух граней
  Delete Loose           плавающие обломки (шум density-поля TripoSR)
  Fill Holes             закрывает дыры веером треугольников (как Mesh > Clean Up > Fill Holes)
  Recalculate Normals    направляет грани каждой компоненты наружу (по знаку объёма)

Только numpy/scipy — без networkx и прочих необязательных зависимостей trimesh.
"""
from __future__ import annotations

import numpy as np
import trimesh
from scipy.sparse import coo_matrix
from scipy.sparse.csgraph import connected_components


def _compact(v, f, rgba):
    """Выбрасывает неиспользуемые вершины и перенумеровывает грани."""
    used, inv = np.unique(f.reshape(-1), return_inverse=True)
    return v[used], inv.reshape(-1, 3), (None if rgba is None else rgba[used])


def _components(f, n):
    r = np.concatenate([f[:, 0], f[:, 1], f[:, 2]])
    c = np.concatenate([f[:, 1], f[:, 2], f[:, 0]])
    g = coo_matrix((np.ones(len(r), dtype=np.int8), (r, c)), shape=(n, n))
    return connected_components(g, directed=False)


def _drop_non_manifold(f, n):
    e = np.sort(np.concatenate([f[:, [0, 1]], f[:, [1, 2]], f[:, [2, 0]]]), axis=1)
    key = e[:, 0] * n + e[:, 1]
    uniq, counts = np.unique(key, return_counts=True)
    bad = uniq[counts > 2]
    if len(bad) == 0:
        return f, 0
    bad_faces = np.unique(np.tile(np.arange(len(f)), 3)[np.isin(key, bad)])
    keep = np.ones(len(f), dtype=bool)
    keep[bad_faces] = False
    return f[keep], len(bad_faces)


def _fill_holes(v, f, rgba, max_edges):
    """Дыра = цикл граничных рёбер. Закрываем веером вокруг центроида (направление — согласованное с соседями)."""
    n = len(v)
    d = np.concatenate([f[:, [0, 1]], f[:, [1, 2]], f[:, [2, 0]]])
    boundary = d[~np.isin(d[:, 1] * n + d[:, 0], d[:, 0] * n + d[:, 1])]    # ребро a->b без парного b->a
    if len(boundary) == 0:
        return v, f, rgba, 0
    nxt: dict[int, list[int]] = {}
    for a, b in boundary.tolist():
        nxt.setdefault(a, []).append(b)
    used: set[tuple[int, int]] = set()
    new_v, new_c, new_f, filled = [], [], [], 0
    for a0, b0 in boundary.tolist():
        if (a0, b0) in used:
            continue
        used.add((a0, b0))
        loop, cur, ok = [a0], b0, True
        while cur != a0:
            loop.append(cur)
            cand = [x for x in nxt.get(cur, []) if (cur, x) not in used]
            if not cand or len(loop) > max_edges:
                ok = False
                break
            used.add((cur, cand[0]))
            cur = cand[0]
        if not ok or len(loop) < 3:
            continue
        idx = np.array(loop)
        c_idx = n + len(new_v)
        new_v.append(v[idx].mean(axis=0))
        if rgba is not None:
            new_c.append(np.rint(rgba[idx].astype(np.float64).mean(axis=0)).astype(np.uint8))
        nxt_idx = np.roll(idx, -1)
        new_f.append(np.stack([nxt_idx, idx, np.full(len(idx), c_idx)], axis=1))   # (q, p, c) — против направления ребра
        filled += 1
    if not filled:
        return v, f, rgba, 0
    v = np.vstack([v, np.array(new_v)])
    f = np.vstack([f] + new_f)
    if rgba is not None:
        rgba = np.vstack([rgba, np.array(new_c, dtype=np.uint8)])
    return v, f, rgba, filled


def repair_mesh(vertices, faces, rgba=None, *, merge_tol_rel: float = 1e-6,
                floater_ratio: float = 0.02, max_hole_edges: int = 400):
    """
    Возвращает (vertices, faces, rgba, report).
    floater_ratio: компоненты мельче этой доли от крупнейшей удаляются (0 — не удалять никого).
    max_hole_edges: дыры с большим периметром остаются открытыми (не «затыкаем» полмодели).
    """
    v = np.asarray(vertices, dtype=np.float64)
    f = np.asarray(faces, dtype=np.int64)
    rgba = None if rgba is None else np.asarray(rgba, dtype=np.uint8)
    rep = {"merged_vertices": 0, "degenerate_faces": 0, "duplicate_faces": 0, "non_manifold_faces": 0,
           "floaters_removed": 0, "holes_filled": 0, "flipped_components": 0}
    if len(f) == 0:
        return v, f, rgba, rep
    diag = float(np.linalg.norm(np.ptp(v, axis=0))) or 1.0

    # 1. Merge by Distance
    tol = diag * merge_tol_rel
    keys = np.round((v - v.min(axis=0)) / tol).astype(np.int64)
    _, first, inv = np.unique(keys, axis=0, return_index=True, return_inverse=True)
    inv = inv.reshape(-1)
    rep["merged_vertices"] = int(len(v) - len(first))
    v, f = v[first], inv[f]
    rgba = None if rgba is None else rgba[first]

    # 2. вырожденные и дублирующиеся грани
    area2 = np.linalg.norm(np.cross(v[f[:, 1]] - v[f[:, 0]], v[f[:, 2]] - v[f[:, 0]]), axis=1)
    keep = (f[:, 0] != f[:, 1]) & (f[:, 1] != f[:, 2]) & (f[:, 2] != f[:, 0]) & (area2 > 1e-12 * diag * diag)
    rep["degenerate_faces"] = int((~keep).sum())
    f = f[keep]
    _, uidx = np.unique(np.sort(f, axis=1), axis=0, return_index=True)
    rep["duplicate_faces"] = int(len(f) - len(uidx))
    f = f[np.sort(uidx)]

    # 3. non-manifold
    f, rep["non_manifold_faces"] = _drop_non_manifold(f, len(v))
    if len(f) == 0:
        return v, f, rgba, rep
    v, f, rgba = _compact(v, f, rgba)

    # 4. Delete Loose (плавающие куски)
    ncomp, labels = _components(f, len(v))
    fc = labels[f[:, 0]]
    sizes = np.bincount(fc, minlength=ncomp)
    keep_comp = sizes >= floater_ratio * sizes.max()
    rep["floaters_removed"] = int((~keep_comp).sum() - (sizes == 0).sum())
    f = f[keep_comp[fc]]
    v, f, rgba = _compact(v, f, rgba)

    # 5. Fill Holes
    v, f, rgba, rep["holes_filled"] = _fill_holes(v, f, rgba, max_hole_edges)

    # 6. Recalculate Normals (Outside): знак объёма каждой компоненты
    ncomp, labels = _components(f, len(v))
    fc = labels[f[:, 0]]
    vc = v - v.mean(axis=0)
    contrib = np.einsum("ij,ij->i", vc[f[:, 0]], np.cross(vc[f[:, 1]], vc[f[:, 2]]))
    flip = np.bincount(fc, weights=contrib, minlength=ncomp) < 0
    mask = flip[fc]
    f[mask] = f[mask][:, ::-1]
    rep["flipped_components"] = int(flip.sum())
    return v, f, rgba, rep


def repair_trimesh(mesh: trimesh.Trimesh, **kwargs):
    """Обёртка для trimesh: цвета вершин сохраняются. Возвращает (mesh, report)."""
    rgba = np.asarray(mesh.visual.vertex_colors, dtype=np.uint8) if mesh.visual.kind == "vertex" else None
    v, f, c, rep = repair_mesh(mesh.vertices, mesh.faces, rgba, **kwargs)
    out = trimesh.Trimesh(v, f, process=False)
    if c is not None:
        out.visual = trimesh.visual.ColorVisuals(out, vertex_colors=c)
    return out, rep
