"""
Самопроверка isosurface.py: правильные ли оси и направление граней.
Запуск (из каталога ml-service, с тем же окружением, что и сервис):
    python tools/check_isosurface.py /путь/к/TripoSR
Строит искусственный объект, вытянутый вдоль X, прогоняет через ВАШ MarchingCubeHelper и сообщает вердикт.
"""
import sys
import numpy as np


def signed_volume(v, f):
    return float(np.einsum("ij,ij->i", v[f[:, 0]], np.cross(v[f[:, 1]], v[f[:, 2]])).sum() / 6.0)


def diagnose(verts01: np.ndarray, faces: np.ndarray) -> list[str]:
    """verts01 — вершины в [0, 1]. Эталон: вытянут по X (≈1.6), по Y и Z ≈ 0.6; грани наружу."""
    out = []
    if verts01.min() < -1e-3 or verts01.max() > 1.001:
        out.append(f"✗ ВЕРШИНЫ ВНЕ [0, 1] (от {verts01.min():.1f} до {verts01.max():.1f}): нет деления на (resolution - 1). "
                   "Цвет запрашивается вне triplane -> серая модель.")
        verts01 = verts01 / max(float(verts01.max()), 1.0)      # дальше проверяем оси на нормированных вершинах
    ext = np.ptp(verts01 * 2 - 1, axis=0)
    out += [f"размах вершин по колонкам (x, y, z): {np.round(ext, 2).tolist()}  (ожидается ≈ [1.6, 0.6, 0.6])"]
    if ext.argmax() != 0:
        out.append("✗ ОСИ ПЕРЕПУТАНЫ: длинная ось не в колонке 0 -> уберите перестановку [2, 1, 0] / [..., ::-1] "
                   "у вершин (см. isosurface_fix.py). Поворотами матриц это не исправить.")
    else:
        out.append("✓ порядок осей верный")
    vol = signed_volume(verts01, faces)
    out.append("✓ грани смотрят наружу" if vol > 0 else
               "✗ ГРАНИ ВЫВЕРНУТЫ ВНУТРЬ (объём < 0) -> gradient_direction='ascent' или faces[:, ::-1]")
    return out


if __name__ == "__main__":
    sys.path.append(sys.argv[1] if len(sys.argv) > 1 else ".")
    from tsr.models.isosurface import MarchingCubeHelper     # ВАША версия (с skimage)
    R = 64
    helper = MarchingCubeHelper(R)
    g = helper.grid_vertices.numpy() * 2 - 1                  # узлы сетки в порядке meshgrid(indexing="ij"), [-1, 1]
    import torch
    density = 1.0 - ((g[:, 0] / 0.8) ** 2 + (g[:, 1] / 0.3) ** 2 + (g[:, 2] / 0.3) ** 2)   # >0 внутри, длинная ось — X
    verts, faces = helper(-torch.from_numpy(density).float())  # TripoSR передаёт в helper -(density - threshold)
    print("\n".join(diagnose(verts.cpu().numpy(), faces.cpu().numpy())))
