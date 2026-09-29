"""Discrete changes, not inferred physical laws or object tracking."""

from fractions import Fraction

import numpy as np


def numeric(values):
    array = np.asarray(values)
    if array.dtype.kind not in "buif" or array.dtype.itemsize > 8:
        raise ValueError("Campo requer valores numéricos reais.")
    if not np.all(np.isfinite(array)):
        raise ValueError("Campo contém valores não finitos.")
    return array


def difference(previous, current):
    previous, current = numeric(previous), numeric(current)
    if previous.dtype.kind in "ui" and previous.dtype.itemsize > 4:
        raise ValueError("Diferenças exatas suportam inteiros de até 32 bits.")
    if previous.shape != current.shape or previous.dtype != current.dtype:
        raise ValueError("Mudança de grade ou dtype requer outro segmento.")
    dtype = np.float64 if previous.dtype.kind == "f" else np.int64
    return current.astype(dtype) - previous.astype(dtype)


def rate(delta, elapsed):
    """Mean change over the stated interval; no interpolation is implied."""
    elapsed = Fraction(elapsed)
    if elapsed <= 0:
        raise ValueError("O intervalo temporal deve ser positivo.")
    return numeric(delta).astype(np.float64) / float(elapsed)


def spatial_gradient(values, axes, spacing=None):
    """Forward differences on chosen spatial axes, in declared grid units.

    Each result has one fewer sample on its differentiated axis. A singleton
    axis has an empty derivative, not an invented boundary value. For vector
    values these partial derivatives form a discrete Jacobian.
    """
    values = numeric(values)
    axes = tuple(axes)
    spacing = tuple(spacing) if spacing is not None else (1,) * len(axes)
    if len(axes) != len(spacing) or len(set(axes)) != len(axes):
        raise ValueError("Eixos/espaçamentos inválidos.")
    output = {}
    for axis, step in zip(axes, spacing):
        if not 0 <= axis < values.ndim or not np.isfinite(step) or step <= 0:
            raise ValueError("Eixo ou espaçamento inválido.")
        before, after = [slice(None)] * values.ndim, [slice(None)] * values.ndim
        before[axis], after[axis] = slice(None, -1), slice(1, None)
        output[axis] = difference(values[tuple(before)], values[tuple(after)]) / step
    return output


def trajectory_velocity(positions, times):
    """Velocity only when positions already refer to the SAME tracked object."""
    positions = numeric(positions)
    times = [Fraction(t) for t in times]
    if positions.ndim != 2 or len(positions) != len(times) or len(times) < 2:
        raise ValueError("Trajetória requer posições vetoriais e ao menos dois tempos.")
    return np.stack([rate(difference(positions[i - 1], positions[i]), times[i] - times[i - 1])
                     for i in range(1, len(times))])
