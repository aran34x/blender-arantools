"""
AranTools — Custom Feather / Haircards Generator (Pure Python, No Geometry Nodes)
=================================================================================
Authors and generates feather card meshes from an edge-only spline guide mesh.

Features:
- Pure-Python mathematical replica of `BS_ExtrudeAlongCurve.001` + `Deform Match Curve.002`
  (Open uniform cubic B-spline wire subdivision, 50-point uniform arc-length resampling,
  1D attribute blur for tilt and spline positions with the exact Float Curve profile,
  poly-spline tangents & tilted normals, and bounding-box curve deformation).
- Ultra-fast Edit Mode live updates (< 0.5 ms per frame when dragging feathers):
  * Per-chain memoization cache (only recomputes the feather chain(s) being moved/edited).
  * In-place `out_mesh.vertices.foreach_set("co", ...)` fast-path whenever topology is unchanged,
    avoiding expensive `clear_geometry()` / `from_pydata()` / modifier rebuilds during `G` grab.
- Per-edge / per-feather settings stored on the EDGE domain:
  * Direct Mesh Object selector (`feather_mesh_slot` -> `obj.arantools_feather.mesh_slots`)
  * Explicit Feather Direction (`feather_reverse`) with:
    1. "Flip Direction" (flips the feather visually Root <-> Tip)
    2. "Flip Direction (Keep Look) — Invert Base Meshes" (inverts the original base feather
       meshes by 180° around Z and reverses the spline direction + tilt so internal Root -> Tip
       direction is inverted while the 3D visual result remains 100% identical!)
  * Per-feather `feather_subdiv`, `feather_smooth_tilt`, `feather_smooth_spline`, `feather_up`
- Per-vertex metadata stored on the POINT domain:
  * `radius`, `tilt`, `smooth`, `pushout`, `pushtip`, `followsurface`, `tilttosurface`
"""

import colorsys
import math
import time
import numpy as np
import bpy
import bmesh
from bpy_extras import view3d_utils
from bpy.props import (
    BoolProperty,
    CollectionProperty,
    EnumProperty,
    FloatProperty,
    FloatVectorProperty,
    IntProperty,
    PointerProperty,
    StringProperty,
)
from bpy.types import Operator, PropertyGroup, UIList
from mathutils import Matrix, Quaternion, Vector
from mathutils.bvhtree import BVHTree
from mathutils.geometry import intersect_point_line


# ============================================================================
# Constants & Exact Float Curve Lookup Tables
# ============================================================================

POINT_FLOAT_ATTRS = (
    ("radius", 1.0),
    ("tilt", 0.0),
    ("smooth", 0.0),
    ("pushout", 0.0),
    ("pushtip", 0.0),
    ("followsurface", 0.0),
    ("tilttosurface", 0.0),
)

EDGE_INT_ATTRS = (
    ("feather_mesh_slot", 0),
    ("feather_mesh_mask", -1),
    ("feather_seed", 0),
    ("feather_subdiv", 2),
    ("feather_smooth_tilt", 30),
    ("feather_smooth_spline", 100),
    # Card choice for the internal mirror copy: <= 0 = same card as the original,
    # > 0 = bitmask of mesh slots (one bit = that card, several = random pick).
    ("feather_mirror_mask", 0),
)

EDGE_BOOL_ATTRS = (
    ("feather_reverse", False),
    ("feather_mirror", True),
)

# Exact Float Curve definitions from `BS_ExtrudeAlongCurve.001`:
# - `Float Curve` (pushout profile along spline factor)
_FC0_CONTROL_POINTS = (
    (0.0, 0.0, 'AUTO'),
    (0.11818181872367859, 1.0, 'VECTOR'),
    (1.0, 1.0, 'AUTO'),
)

# - `Float Curve.001` (pushtip profile along spline factor)
_FC1_CONTROL_POINTS = (
    (0.0, 0.0, 'AUTO'),
    (0.6545454859733582, 0.30000001192092896, 'AUTO'),
    (1.0, 1.0, 'AUTO'),
)

# - `Float Curve.002` (SmoothSpline weight profile along spline factor)
_FC2_CONTROL_POINTS = (
    (0.0, 0.0, 'AUTO'),
    (0.3272727429866791, 0.17499999701976776, 'AUTO'),
    (0.5045454502105713, 0.862500011920929, 'AUTO'),
    (0.75, 0.9437500238418579, 'AUTO'),
    (0.9636363387107849, 0.25, 'AUTO'),
    (1.0, 0.0, 'AUTO'),
)

_FC0_LUT = None
_FC1_LUT = None
_FC2_LUT = None
_FC_LUT_SIZE = 4096

# Guard flags & per-object caches for real-time interactive dragging
_IS_GENERATING = False
_IS_SYNCING_UI = False
_LAST_SELECTION_SIGNATURE = None
_DRAW_HANDLER = None
_ADDON_KEYMAPS = []

# Keyed by guide_obj.name:
# {
#   "topo_sig": tuple,
#   "chain_cache": {chain_idx: (chain_key, flat_verts_list)},
#   "base_caches": {slot_idx: (mesh_sig, bcache)},
#   "surface_cache": (surf_sig, bvh, surf_vert_normals_world),
# }
_RUNTIME_CACHE = {}


def invalidate_feather_cache(guide_obj=None):
    """Clear cached chain/base-mesh data so the next regeneration rebuilds everything."""
    if guide_obj is None:
        _RUNTIME_CACHE.clear()
    else:
        _RUNTIME_CACHE.pop(guide_obj.name, None)


def _build_float_curve_lut(node_name, ctrl_pts):
    """Build a 4096-sample lookup table matching a `ShaderNodeFloatCurve` in `BS_ExtrudeAlongCurve.001`."""
    ng = bpy.data.node_groups.get("BS_ExtrudeAlongCurve.001")
    fc_node = ng.nodes.get(node_name) if ng else None
    temp_ng = None

    if fc_node is None:
        try:
            temp_ng = bpy.data.node_groups.new("_AranFeatherTempCurve", "GeometryNodeTree")
            fc_node = temp_ng.nodes.new("ShaderNodeFloatCurve")
            mapping = fc_node.mapping
            curve = mapping.curves[0]
            while len(curve.points) > 2:
                curve.points.remove(curve.points[1])
            curve.points[0].location = (ctrl_pts[0][0], ctrl_pts[0][1])
            curve.points[0].handle_type = ctrl_pts[0][2]
            curve.points[1].location = (ctrl_pts[-1][0], ctrl_pts[-1][1])
            curve.points[1].handle_type = ctrl_pts[-1][2]
            for x, y, htype in ctrl_pts[1:-1]:
                pt = curve.points.new(x, y)
                pt.handle_type = htype
            mapping.update()
        except Exception:
            fc_node = None

    if fc_node is not None:
        mapping = fc_node.mapping
        mapping.initialize()
        curve = mapping.curves[0]
        lut = [
            float(mapping.evaluate(curve, i / float(_FC_LUT_SIZE - 1)))
            for i in range(_FC_LUT_SIZE)
        ]
        if temp_ng is not None:
            bpy.data.node_groups.remove(temp_ng)
        return lut

    lut = []
    for i in range(_FC_LUT_SIZE):
        t = i / float(_FC_LUT_SIZE - 1)
        val = 0.0
        for j in range(len(ctrl_pts) - 1):
            x0, y0, _ = ctrl_pts[j]
            x1, y1, _ = ctrl_pts[j + 1]
            if x0 <= t <= x1:
                u = (t - x0) / (x1 - x0) if x1 > x0 else 0.0
                val = y0 * (1.0 - u) + y1 * u
                break
        lut.append(val)
    return lut


def _eval_lut(lut, t):
    if t <= 0.0:
        return lut[0]
    if t >= 1.0:
        return lut[-1]
    pos = t * (_FC_LUT_SIZE - 1)
    idx = int(pos)
    if idx >= _FC_LUT_SIZE - 1:
        return lut[-1]
    frac = pos - idx
    return lut[idx] * (1.0 - frac) + lut[idx + 1] * frac


def eval_float_curve_000(t):
    """Evaluate `Float Curve` (`pushout` profile) at `t` in [0, 1]."""
    global _FC0_LUT
    if _FC0_LUT is None:
        _FC0_LUT = _build_float_curve_lut("Float Curve", _FC0_CONTROL_POINTS)
    return _eval_lut(_FC0_LUT, t)


def eval_float_curve_001(t):
    """Evaluate `Float Curve.001` (`pushtip` profile) at `t` in [0, 1]."""
    global _FC1_LUT
    if _FC1_LUT is None:
        _FC1_LUT = _build_float_curve_lut("Float Curve.001", _FC1_CONTROL_POINTS)
    return _eval_lut(_FC1_LUT, t)


def eval_float_curve_002(t):
    """Evaluate `Float Curve.002` (`SmoothSpline` weight profile) at `t` in [0, 1]."""
    global _FC2_LUT
    if _FC2_LUT is None:
        _FC2_LUT = _build_float_curve_lut("Float Curve.002", _FC2_CONTROL_POINTS)
    return _eval_lut(_FC2_LUT, t)


# ============================================================================
# Pure-Python Mathematical Engine (Matches BS_ExtrudeAlongCurve.001 1:1)
# ============================================================================

def extract_feather_chains(mesh):
    """Extract connected edge chains from an edge-only mesh, matching `Mesh to Curve`."""
    n_verts = len(mesh.vertices)
    if n_verts == 0 or len(mesh.edges) == 0:
        return []

    adj = [[] for _ in range(n_verts)]
    for e in mesh.edges:
        u, v = e.vertices[0], e.vertices[1]
        adj[u].append((v, e.index))
        adj[v].append((u, e.index))

    visited_edges = set()
    chains = []

    for start_v in range(n_verts):
        if len(adj[start_v]) != 2:
            for nxt_v, e_idx in adj[start_v]:
                if e_idx in visited_edges:
                    continue
                v_chain = [start_v, nxt_v]
                e_chain = [e_idx]
                visited_edges.add(e_idx)
                cur = nxt_v
                while len(adj[cur]) == 2:
                    e0, e1 = adj[cur][0], adj[cur][1]
                    cand_v, cand_e = (e0 if e0[1] not in visited_edges else e1)
                    if cand_e in visited_edges:
                        break
                    visited_edges.add(cand_e)
                    v_chain.append(cand_v)
                    e_chain.append(cand_e)
                    cur = cand_v
                chains.append((v_chain, e_chain))

    for start_v in range(n_verts):
        for nxt_v, e_idx in adj[start_v]:
            if e_idx in visited_edges:
                continue
            v_chain = [start_v, nxt_v]
            e_chain = [e_idx]
            visited_edges.add(e_idx)
            cur = nxt_v
            while len(adj[cur]) == 2:
                e0, e1 = adj[cur][0], adj[cur][1]
                cand_v, cand_e = (e0 if e0[1] not in visited_edges else e1)
                if cand_e in visited_edges:
                    break
                visited_edges.add(cand_e)
                v_chain.append(cand_v)
                e_chain.append(cand_e)
                cur = cand_v
            chains.append((v_chain, e_chain))

    return chains


def bspline_subdivide_chain_fast(
    px,
    py,
    pz,
    rad,
    tilt,
    smooth,
    level,
    pushout=None,
    pushtip=None,
    follow=None,
    tiltsurf=None,
):
    """Fast scalar-list open uniform cubic B-spline wire subdivision with all point attributes."""
    n = len(px)
    if pushout is None:
        pushout = [0.0] * n
    if pushtip is None:
        pushtip = [0.0] * n
    if follow is None:
        follow = [0.0] * n
    if tiltsurf is None:
        tiltsurf = [0.0] * n

    if level <= 0 or n < 2:
        return (
            list(px),
            list(py),
            list(pz),
            list(rad),
            list(tilt),
            list(smooth),
            list(pushout),
            list(pushtip),
            list(follow),
            list(tiltsurf),
        )

    steps = 1 << int(min(level, 6))
    inv_steps = 1.0 / float(steps)

    ex = [2.0 * px[0] - px[1]] + list(px) + [2.0 * px[-1] - px[-2]]
    ey = [2.0 * py[0] - py[1]] + list(py) + [2.0 * py[-1] - py[-2]]
    ez = [2.0 * pz[0] - pz[1]] + list(pz) + [2.0 * pz[-1] - pz[-2]]

    ox, oy, oz = [], [], []
    orad, otilt, osmooth = [], [], []
    opushout, opushtip, ofollow, otiltsurf = [], [], [], []

    for i in range(n - 1):
        x0, x1, x2, x3 = ex[i], ex[i + 1], ex[i + 2], ex[i + 3]
        y0, y1, y2, y3 = ey[i], ey[i + 1], ey[i + 2], ey[i + 3]
        z0, z1, z2, z3 = ez[i], ez[i + 1], ez[i + 2], ez[i + 3]
        r0, r1 = rad[i], rad[i + 1]
        t0, t1 = tilt[i], tilt[i + 1]
        s0, s1 = smooth[i], smooth[i + 1]
        po0, po1 = pushout[i], pushout[i + 1]
        pt0, pt1 = pushtip[i], pushtip[i + 1]
        fl0, fl1 = follow[i], follow[i + 1]
        ts0, ts1 = tiltsurf[i], tiltsurf[i + 1]

        k_end = steps if i < n - 2 else steps + 1
        for s in range(k_end):
            u = s * inv_steps
            om = 1.0 - u
            u2 = u * u
            u3 = u2 * u
            b0 = (om * om * om) / 6.0
            b1 = (3.0 * u3 - 6.0 * u2 + 4.0) / 6.0
            b2 = (-3.0 * u3 + 3.0 * u2 + 3.0 * u + 1.0) / 6.0
            b3 = u3 / 6.0

            ox.append(b0 * x0 + b1 * x1 + b2 * x2 + b3 * x3)
            oy.append(b0 * y0 + b1 * y1 + b2 * y2 + b3 * y3)
            oz.append(b0 * z0 + b1 * z1 + b2 * z2 + b3 * z3)
            orad.append(r0 * om + r1 * u)
            otilt.append(t0 * om + t1 * u)
            osmooth.append(s0 * om + s1 * u)
            opushout.append(po0 * om + po1 * u)
            opushtip.append(pt0 * om + pt1 * u)
            ofollow.append(fl0 * om + fl1 * u)
            otiltsurf.append(ts0 * om + ts1 * u)

    return ox, oy, oz, orad, otilt, osmooth, opushout, opushtip, ofollow, otiltsurf


_TILT_BLUR_MAT_CACHE = {}


def blur_1d_float(vals, iterations, weights=None):
    """Exact replica of Blender's 1D Curve `Blur Attribute` node on floats (NumPy accelerated)."""
    n = len(vals)
    if n < 2 or iterations <= 0:
        return list(vals)
    if weights is None:
        key = (n, iterations)
        mat = _TILT_BLUR_MAT_CACHE.get(key)
        if mat is None:
            m = np.zeros((n, n), dtype=np.float64)
            m[0, 0] = 0.5
            m[0, 1] = 0.5
            m[n - 1, n - 1] = 0.5
            m[n - 1, n - 2] = 0.5
            for i in range(1, n - 1):
                m[i, i - 1] = 1.0 / 3.0
                m[i, i] = 1.0 / 3.0
                m[i, i + 1] = 1.0 / 3.0
            mat = np.linalg.matrix_power(m, iterations)
            _TILT_BLUR_MAT_CACHE[key] = mat
        return (mat @ np.asarray(vals, dtype=np.float64)).tolist()

    w = np.maximum(0.0, np.asarray(weights, dtype=np.float64))
    m = np.zeros((n, n), dtype=np.float64)
    m[0, 0] = 1.0 / (1.0 + w[0])
    m[0, 1] = w[0] / (1.0 + w[0])
    m[n - 1, n - 1] = 1.0 / (1.0 + w[n - 1])
    m[n - 1, n - 2] = w[n - 1] / (1.0 + w[n - 1])
    idx = np.arange(1, n - 1)
    inv_d = 1.0 / (1.0 + 2.0 * w[idx])
    m[idx, idx] = inv_d
    m[idx, idx - 1] = w[idx] * inv_d
    m[idx, idx + 1] = w[idx] * inv_d
    mat = np.linalg.matrix_power(m, iterations)
    return (mat @ np.asarray(vals, dtype=np.float64)).tolist()


def blur_1d_xyz(px, py, pz, iterations, weights):
    """Fast NumPy matrix-power 1D blur for 3D spline points."""
    n = len(px)
    if n < 2 or iterations <= 0:
        return list(px), list(py), list(pz)

    w = np.maximum(0.0, np.asarray(weights, dtype=np.float64))
    m = np.zeros((n, n), dtype=np.float64)
    m[0, 0] = 1.0 / (1.0 + w[0])
    m[0, 1] = w[0] / (1.0 + w[0])
    m[n - 1, n - 1] = 1.0 / (1.0 + w[n - 1])
    m[n - 1, n - 2] = w[n - 1] / (1.0 + w[n - 1])
    idx = np.arange(1, n - 1)
    inv_d = 1.0 / (1.0 + 2.0 * w[idx])
    m[idx, idx] = inv_d
    m[idx, idx - 1] = w[idx] * inv_d
    m[idx, idx + 1] = w[idx] * inv_d

    mat = np.linalg.matrix_power(m, iterations)
    pts = np.column_stack((px, py, pz))
    out = mat @ pts
    return out[:, 0].tolist(), out[:, 1].tolist(), out[:, 2].tolist()


def _get_surface_bvh_cache(guide_obj, obj_cache=None):
    """Build or return a cached `BVHTree` of `guide_obj.arantools_feather.surface_object` in `guide_obj` local space."""
    fprops = getattr(guide_obj, "arantools_feather", None)
    if fprops is None:
        return None, None
    surf_obj = fprops.surface_object
    if surf_obj is None or surf_obj.type != 'MESH' or surf_obj.data is None or len(surf_obj.data.polygons) == 0:
        return None, None

    if obj_cache is None:
        obj_cache = _RUNTIME_CACHE.setdefault(guide_obj.name, {})

    sm = surf_obj.data
    blur_iters = max(0, int(getattr(fprops, "blur_source_mesh", 0)))
    xform = guide_obj.matrix_world.inverted_safe() @ surf_obj.matrix_world
    xform_sig = tuple(round(v, 5) for row in xform for v in row)
    surf_sig = (surf_obj.name, len(sm.vertices), len(sm.polygons), blur_iters, xform_sig)

    cached = obj_cache.get("surface_cache")
    if cached is not None and cached[0] == surf_sig:
        return cached[1], surf_sig

    verts = [xform @ v.co for v in sm.vertices]
    if blur_iters > 0 and len(verts) > 0 and len(sm.edges) > 0:
        adj = [[] for _ in range(len(verts))]
        for e in sm.edges:
            u, v = e.vertices[0], e.vertices[1]
            adj[u].append(v)
            adj[v].append(u)
        for _ in range(blur_iters):
            new_verts = []
            for idx_v, cur_co in enumerate(verts):
                nbrs = adj[idx_v]
                if not nbrs:
                    new_verts.append(cur_co)
                else:
                    acc = cur_co.copy()
                    for nb in nbrs:
                        acc += verts[nb]
                    new_verts.append(acc / float(len(nbrs) + 1))
            verts = new_verts

    polys = [tuple(p.vertices) for p in sm.polygons]
    bvh = BVHTree.FromPolygons(verts, polys)
    obj_cache["surface_cache"] = (surf_sig, bvh)
    return bvh, surf_sig


def _evaluate_chain_50pts(
    px,
    py,
    pz,
    rad_scaled,
    tilt_arr,
    smooth_arr,
    subdiv_level,
    smooth_tilt_iters,
    smooth_spline_iters,
    n_base,
    invert_profile,
    pushout_arr=None,
    pushtip_arr=None,
    follow_arr=None,
    tiltsurf_arr=None,
    surf_bvh=None,
):
    """Evaluate a feather chain into its 50 resampled points, tangents, normals, radii, and intermediate states."""
    # 1. Open uniform cubic B-spline wire subdivision
    sx, sy, sz, srad, stilt, ssmooth, spushout, spushtip, sfollow, stiltsurf = bspline_subdivide_chain_fast(
        px,
        py,
        pz,
        rad_scaled,
        tilt_arr,
        smooth_arr,
        subdiv_level,
        pushout=pushout_arr,
        pushtip=pushtip_arr,
        follow=follow_arr,
        tiltsurf=tiltsurf_arr,
    )

    # 2. Uniform arc-length resampling into 50 points
    n_sub = len(sx)
    cum = [0.0] * n_sub
    acc = 0.0
    for i in range(1, n_sub):
        dx = sx[i] - sx[i - 1]
        dy = sy[i] - sy[i - 1]
        dz = sz[i] - sz[i - 1]
        acc += math.sqrt(dx * dx + dy * dy + dz * dz)
        cum[i] = acc
    tot = acc

    rx = [0.0] * 50
    ry = [0.0] * 50
    rz = [0.0] * 50
    c_rad = [0.0] * 50
    c_tilt = [0.0] * 50
    res_smooth = [0.0] * 50
    res_pushout = [0.0] * 50
    res_pushtip = [0.0] * 50
    res_follow = [0.0] * 50
    res_tiltsurf = [0.0] * 50

    idx = 0
    max_idx = n_sub - 2
    for k in range(50):
        d = (k / 49.0) * tot
        while idx < max_idx and cum[idx + 1] < d:
            idx += 1
        seg = cum[idx + 1] - cum[idx]
        u = (d - cum[idx]) / seg if seg > 1e-12 else 0.0
        om = 1.0 - u
        i1 = idx + 1
        rx[k] = sx[idx] * om + sx[i1] * u
        ry[k] = sy[idx] * om + sy[i1] * u
        rz[k] = sz[idx] * om + sz[i1] * u
        c_rad[k] = srad[idx] * om + srad[i1] * u
        c_tilt[k] = stilt[idx] * om + stilt[i1] * u
        res_smooth[k] = ssmooth[idx] * om + ssmooth[i1] * u
        res_pushout[k] = spushout[idx] * om + spushout[i1] * u
        res_pushtip[k] = spushtip[idx] * om + spushtip[i1] * u
        res_follow[k] = sfollow[idx] * om + sfollow[i1] * u
        res_tiltsurf[k] = stiltsurf[idx] * om + stiltsurf[i1] * u

    has_push = any(v != 0.0 for v in res_pushout) or any(v != 0.0 for v in res_pushtip)
    has_follow = any(v != 0.0 for v in res_follow)
    has_tiltsurf = any(v != 0.0 for v in res_tiltsurf)

    # 3. Surface sampling (`Projection Object` / `surf_bvh`, with `n_base` fallback)
    if has_push or has_follow or has_tiltsurf:
        surf_pos = []
        surf_nor = []
        for k in range(50):
            pt = Vector((rx[k], ry[k], rz[k]))
            if surf_bvh is not None:
                loc, nor, _, _ = surf_bvh.find_nearest(pt)
                if loc is not None and nor is not None:
                    surf_pos.append(loc)
                    surf_nor.append(nor.normalized() if nor.length_squared > 1e-12 else n_base)
                else:
                    surf_pos.append(pt)
                    surf_nor.append(n_base)
            else:
                surf_pos.append(pt)
                surf_nor.append(n_base)
    else:
        surf_pos = None
        surf_nor = None

    # 4. `tilttosurface` (`Mix.003` -> `Blur Attribute.001` 2 iterations)
    if has_tiltsurf and surf_nor is not None:
        mx_x, mx_y, mx_z = [], [], []
        for k in range(50):
            fac = max(0.0, min(1.0, res_tiltsurf[k]))
            mv = n_base.lerp(surf_nor[k], fac)
            mx_x.append(mv.x)
            mx_y.append(mv.y)
            mx_z.append(mv.z)
        mx_x, mx_y, mx_z = blur_1d_xyz(mx_x, mx_y, mx_z, 2, [1.0] * 50)
        n_curve = []
        for k in range(50):
            v = Vector((mx_x[k], mx_y[k], mx_z[k]))
            n_curve.append(v.normalized() if v.length_squared > 1e-12 else n_base)
    else:
        n_curve = [n_base] * 50

    # 5. `SmoothTilt` (`Blur Attribute.003`)
    c_tilt = blur_1d_float(c_tilt, smooth_tilt_iters)

    # 6. `followsurface` (`Mix.001` -> `Set Position.001`)
    if has_follow and surf_pos is not None:
        for k in range(50):
            fac = max(0.0, min(1.0, res_follow[k]))
            if fac != 0.0:
                sp = surf_pos[k]
                om = 1.0 - fac
                rx[k] = rx[k] * om + sp.x * fac
                ry[k] = ry[k] * om + sp.y * fac
                rz[k] = rz[k] * om + sp.z * fac

    # 7. `pushout` + `pushtip` (`Blur Attribute` 6 iterations on `surf_nor`, `Float Curve` & `Float Curve.001`)
    if has_push and surf_nor is not None:
        snx = [v.x for v in surf_nor]
        sny = [v.y for v in surf_nor]
        snz = [v.z for v in surf_nor]
        bnx, bny, bnz = blur_1d_xyz(snx, sny, snz, 6, [1.0] * 50)

        cum_p = [0.0] * 50
        acc_p = 0.0
        for i in range(1, 50):
            dx = rx[i] - rx[i - 1]
            dy = ry[i] - ry[i - 1]
            dz = rz[i] - rz[i - 1]
            acc_p += math.sqrt(dx * dx + dy * dy + dz * dz)
            cum_p[i] = acc_p
        tot_p = acc_p

        for k in range(50):
            t_fac = (cum_p[k] / tot_p) if tot_p > 1e-12 else (k / 49.0)
            if invert_profile:
                t_fac = 1.0 - t_fac
            fc0 = eval_float_curve_000(t_fac)
            fc1 = eval_float_curve_001(t_fac)
            delta = res_pushout[k] * fc0 + res_pushtip[k] * fc1
            rx[k] += bnx[k] * delta
            ry[k] += bny[k] * delta
            rz[k] += bnz[k] * delta

    # Save the 50 points right after surface/push offsets (before `SmoothSpline`)
    pre_smooth_pts = (list(rx), list(ry), list(rz))

    # 8. `SmoothSpline` (`Float Curve.002` + `Blur Attribute.002`)
    cum2 = [0.0] * 50
    acc2 = 0.0
    for i in range(1, 50):
        dx = rx[i] - rx[i - 1]
        dy = ry[i] - ry[i - 1]
        dz = rz[i] - rz[i - 1]
        acc2 += math.sqrt(dx * dx + dy * dy + dz * dz)
        cum2[i] = acc2
    tot2 = acc2

    if invert_profile:
        fc2_weights = [
            eval_float_curve_002(1.0 - ((cum2[i] / tot2) if tot2 > 1e-12 else 0.0))
            for i in range(50)
        ]
    else:
        fc2_weights = [
            eval_float_curve_002((cum2[i] / tot2) if tot2 > 1e-12 else 0.0)
            for i in range(50)
        ]

    rx_ss, ry_ss, rz_ss = blur_1d_xyz(rx, ry, rz, smooth_spline_iters, fc2_weights)

    # 9. `smooth` (`Blur Attribute.004` 10 iterations)
    if any(s != 0.0 for s in res_smooth):
        rx_fin, ry_fin, rz_fin = blur_1d_xyz(rx_ss, ry_ss, rz_ss, 10, res_smooth)
        # Also apply `smooth` to `pre_smooth_pts` so baking `smooth` while keeping `SmoothSpline` preserves it!
        psx, psy, psz = blur_1d_xyz(pre_smooth_pts[0], pre_smooth_pts[1], pre_smooth_pts[2], 10, res_smooth)
        pre_smooth_pts = (psx, psy, psz)
    else:
        rx_fin, ry_fin, rz_fin = rx_ss, ry_ss, rz_ss

    # 10. Poly-spline tangents & tilted normals
    res_pts = [Vector((rx_fin[i], ry_fin[i], rz_fin[i])) for i in range(50)]
    seg_dirs = [(res_pts[i + 1] - res_pts[i]).normalized() for i in range(49)]
    c_tan = (
        [seg_dirs[0]]
        + [(seg_dirs[i - 1] + seg_dirs[i]).normalized() for i in range(1, 49)]
        + [seg_dirs[48]]
    )
    c_nor = [Quaternion(c_tan[i], c_tilt[i]) @ n_curve[i] for i in range(50)]

    return res_pts, c_tan, c_nor, c_rad, c_tilt, n_curve, pre_smooth_pts


def _deform_card(frames, bcache, flip=False, xform=None):
    """Deform Match Curve: bend `bcache`'s card along evaluated `frames` -> flat coords.

    `flip` reflects the card across its own spine (negates the sideways card axis) for variation.
    `xform` (4x4) is applied to every output point — used for the internal mirror copy.
    """
    res_pts, c_tan, c_nor, c_rad = frames[0], frames[1], frames[2], frames[3]
    side_sign = -1.0 if flip else 1.0

    cum3 = [0.0] * 50
    acc3 = 0.0
    for i in range(1, 50):
        acc3 += (res_pts[i] - res_pts[i - 1]).length
        cum3[i] = acc3
    tot3 = acc3

    out_flat = []
    for fac, sv1, sv2 in bcache["vert_params"]:
        d = fac * tot3
        s_idx = 0
        while s_idx < 48 and cum3[s_idx + 1] < d:
            s_idx += 1
        seg = cum3[s_idx + 1] - cum3[s_idx]
        u = (d - cum3[s_idx]) / seg if seg > 1e-12 else 0.0
        pos = res_pts[s_idx].lerp(res_pts[s_idx + 1], u)
        tan = c_tan[s_idx].lerp(c_tan[s_idx + 1], u).normalized()
        nor = c_nor[s_idx].lerp(c_nor[s_idx + 1], u).normalized()
        rad = c_rad[s_idx] * (1.0 - u) + c_rad[s_idx + 1] * u
        binor = tan.cross(nor)
        s2 = sv2 * side_sign
        fx = pos.x + rad * (sv1 * nor.x + s2 * binor.x)
        fy = pos.y + rad * (sv1 * nor.y + s2 * binor.y)
        fz = pos.z + rad * (sv1 * nor.z + s2 * binor.z)
        if xform is not None:
            fx, fy, fz = xform @ Vector((fx, fy, fz))
        out_flat.extend((fx, fy, fz))

    return out_flat


def _hash01(*vals):
    """Deterministic hash of ints -> float in [0, 1)."""
    h = 0x27D4EB2D
    for v in vals:
        h = (h ^ (int(v) & 0xFFFFFFFF)) * 0x9E3779B1 & 0xFFFFFFFF
        h = ((h ^ (h >> 16)) * 0x85EBCA6B) & 0xFFFFFFFF
        h = ((h ^ (h >> 13)) * 0xC2B2AE35) & 0xFFFFFFFF
        h = (h ^ (h >> 16)) & 0xFFFFFFFF
    return h / 4294967296.0


def _pick_random_slot_from_mask(enabled_slots, chain_idx, edge_seed, global_seed):
    """Deterministically pick a mesh slot from `enabled_slots` for `chain_idx`."""
    if not enabled_slots:
        return -1
    if len(enabled_slots) == 1:
        return enabled_slots[0]
    h = (
        int(chain_idx) * 0x9E3779B1
        + int(edge_seed) * 0x85EBCA6B
        + int(global_seed) * 0xC2B2AE35
        + 0x27D4EB2D
    ) & 0xFFFFFFFF
    h = ((h ^ (h >> 16)) * 0x85EBCA6B) & 0xFFFFFFFF
    h = ((h ^ (h >> 13)) * 0xC2B2AE35) & 0xFFFFFFFF
    h = (h ^ (h >> 16)) & 0xFFFFFFFF
    return enabled_slots[h % len(enabled_slots)]


def ensure_feather_guide_attributes(mesh):
    """Ensure all required POINT and EDGE attributes exist on the guide mesh."""
    for name, default_val in POINT_FLOAT_ATTRS:
        attr = mesh.attributes.get(name)
        if attr is None:
            attr = mesh.attributes.new(name=name, type='FLOAT', domain='POINT')
            for d in attr.data:
                d.value = default_val

    for name, default_val in EDGE_INT_ATTRS:
        attr = mesh.attributes.get(name)
        if attr is None:
            attr = mesh.attributes.new(name=name, type='INT', domain='EDGE')
            if name == "feather_mesh_mask":
                slot_attr = mesh.attributes.get("feather_mesh_slot")
                for idx_e, d in enumerate(attr.data):
                    s_val = int(slot_attr.data[idx_e].value) if slot_attr else 0
                    d.value = (1 << max(0, min(29, s_val)))
            else:
                for d in attr.data:
                    d.value = default_val

    for name, default_val in EDGE_BOOL_ATTRS:
        attr = mesh.attributes.get(name)
        if attr is None:
            attr = mesh.attributes.new(name=name, type='BOOLEAN', domain='EDGE')
            for d in attr.data:
                d.value = default_val

    up_attr = mesh.attributes.get("feather_up")
    if up_attr is None:
        up_attr = mesh.attributes.new(name="feather_up", type='FLOAT_VECTOR', domain='EDGE')
        for d in up_attr.data:
            d.vector = (0.0, 1.0, 0.0)


def _extract_guide_data(guide_obj):
    """Read vertices, edges, and all 7 point attributes + edge attributes from `guide_obj`."""
    mesh = guide_obj.data
    if guide_obj.mode == 'EDIT':
        bm = bmesh.from_edit_mesh(mesh)
        bm.verts.ensure_lookup_table()
        bm.edges.ensure_lookup_table()

        verts_co = [(v.co.x, v.co.y, v.co.z) for v in bm.verts]
        edges_v = [(e.verts[0].index, e.verts[1].index) for e in bm.edges]

        pt_layers = {}
        for name, default_val in POINT_FLOAT_ATTRS:
            layer = bm.verts.layers.float.get(name)
            if layer is not None:
                pt_layers[name] = [float(v[layer]) for v in bm.verts]
            else:
                pt_layers[name] = [default_val] * len(bm.verts)

        edge_int = {}
        for name, default_val in EDGE_INT_ATTRS:
            layer = bm.edges.layers.int.get(name)
            if layer is not None:
                edge_int[name] = [int(e[layer]) for e in bm.edges]
            else:
                edge_int[name] = [default_val] * len(bm.edges)

        slot_arr = edge_int["feather_mesh_slot"]
        mask_arr = edge_int["feather_mesh_mask"]
        for i, mval in enumerate(mask_arr):
            if mval < 0:
                mask_arr[i] = 1 << max(0, min(29, slot_arr[i]))

        edge_bool = {}
        for name, default_val in EDGE_BOOL_ATTRS:
            layer = bm.edges.layers.bool.get(name)
            if layer is not None:
                edge_bool[name] = [bool(e[layer]) for e in bm.edges]
            else:
                edge_bool[name] = [default_val] * len(bm.edges)

        up_layer = bm.edges.layers.float_vector.get("feather_up")
        if up_layer is not None:
            edge_up = [(float(e[up_layer][0]), float(e[up_layer][1]), float(e[up_layer][2])) for e in bm.edges]
        else:
            edge_up = [(0.0, 1.0, 0.0)] * len(bm.edges)

        return verts_co, edges_v, pt_layers, edge_int, edge_bool, edge_up
    else:
        ensure_feather_guide_attributes(mesh)
        verts_co = [(v.co.x, v.co.y, v.co.z) for v in mesh.vertices]
        edges_v = [(e.vertices[0], e.vertices[1]) for e in mesh.edges]

        pt_layers = {}
        for name, default_val in POINT_FLOAT_ATTRS:
            attr = mesh.attributes.get(name)
            pt_layers[name] = [float(d.value) for d in attr.data] if attr else [default_val] * len(verts_co)

        edge_int = {}
        for name, default_val in EDGE_INT_ATTRS:
            attr = mesh.attributes.get(name)
            edge_int[name] = [int(d.value) for d in attr.data] if attr else [default_val] * len(edges_v)

        slot_arr = edge_int["feather_mesh_slot"]
        mask_arr = edge_int["feather_mesh_mask"]
        for i, mval in enumerate(mask_arr):
            if mval < 0:
                mask_arr[i] = 1 << max(0, min(29, slot_arr[i]))

        edge_bool = {}
        for name, default_val in EDGE_BOOL_ATTRS:
            attr = mesh.attributes.get(name)
            edge_bool[name] = [bool(d.value) for d in attr.data] if attr else [default_val] * len(edges_v)

        up_attr = mesh.attributes.get("feather_up")
        if up_attr is not None:
            edge_up = [(float(d.vector[0]), float(d.vector[1]), float(d.vector[2])) for d in up_attr.data]
        else:
            edge_up = [(0.0, 1.0, 0.0)] * len(edges_v)

        return verts_co, edges_v, pt_layers, edge_int, edge_bool, edge_up


def _trace_chains_from_edges(n_verts, edges_v):
    """Extract `(v_chain, e_chain)` from a list of `(u, v)` edges."""
    if n_verts == 0 or not edges_v:
        return []
    adj = [[] for _ in range(n_verts)]
    for e_idx, (u, v) in enumerate(edges_v):
        adj[u].append((v, e_idx))
        adj[v].append((u, e_idx))

    visited_edges = set()
    chains = []
    for start_v in range(n_verts):
        if len(adj[start_v]) != 2:
            for nxt_v, e_idx in adj[start_v]:
                if e_idx in visited_edges:
                    continue
                v_chain = [start_v, nxt_v]
                e_chain = [e_idx]
                visited_edges.add(e_idx)
                cur = nxt_v
                while len(adj[cur]) == 2:
                    e0, e1 = adj[cur][0], adj[cur][1]
                    cand_v, cand_e = (e0 if e0[1] not in visited_edges else e1)
                    if cand_e in visited_edges:
                        break
                    visited_edges.add(cand_e)
                    v_chain.append(cand_v)
                    e_chain.append(cand_e)
                    cur = cand_v
                chains.append((v_chain, e_chain))

    for start_v in range(n_verts):
        for nxt_v, e_idx in adj[start_v]:
            if e_idx in visited_edges:
                continue
            v_chain = [start_v, nxt_v]
            e_chain = [e_idx]
            visited_edges.add(e_idx)
            cur = nxt_v
            while len(adj[cur]) == 2:
                e0, e1 = adj[cur][0], adj[cur][1]
                cand_v, cand_e = (e0 if e0[1] not in visited_edges else e1)
                if cand_e in visited_edges:
                    break
                visited_edges.add(cand_e)
                v_chain.append(cand_v)
                e_chain.append(cand_e)
                cur = cand_v
            chains.append((v_chain, e_chain))

    return chains


def _prepare_base_mesh_cache(base_obj, axis):
    """Pre-extract geometry, bounding box, UVs, and sharp_edge data from a source feather mesh."""
    if base_obj is None or base_obj.type != 'MESH' or base_obj.data is None:
        return None
    bm_data = base_obj.data
    if len(bm_data.vertices) == 0:
        return None

    src_verts = [v.co.copy() for v in bm_data.vertices]
    min_v = Vector((
        min(v.x for v in src_verts),
        min(v.y for v in src_verts),
        min(v.z for v in src_verts),
    ))
    max_v = Vector((
        max(v.x for v in src_verts),
        max(v.y for v in src_verts),
        max(v.z for v in src_verts),
    ))
    size_v = max_v - min_v
    ax0, ax1, ax2 = axis, (axis + 1) % 3, (axis + 2) % 3
    span = size_v[ax0] if abs(size_v[ax0]) > 1e-12 else 1.0

    vert_params = []
    for sv in src_verts:
        fac = max(0.0, min(1.0, (sv[ax0] - min_v[ax0]) / span))
        vert_params.append((fac, sv[ax1], sv[ax2]))

    polys = [tuple(p.vertices) for p in bm_data.polygons]
    edges = [tuple(e.vertices) for e in bm_data.edges]
    # Reversed winding for cards that end up reflected (mirror copy XOR flip), so their normals
    # still face outward. Corner order (v0, v1, ..., vn) -> (v0, vn, ..., v1).
    polys_rev = [(p[0],) + tuple(reversed(p[1:])) for p in polys]
    rev_loop_order = []
    for p in bm_data.polygons:
        s, n = p.loop_start, p.loop_total
        rev_loop_order.extend([s] + [s + k for k in range(n - 1, 0, -1)])

    sharp_attr = bm_data.attributes.get("sharp_edge")
    if sharp_attr is not None:
        sharp_edges = [bool(d.value) for d in sharp_attr.data]
    else:
        # No `sharp_edge` attribute means no sharp edges (`MeshEdge.use_sharp` no longer exists).
        sharp_edges = [False] * len(bm_data.edges)

    uv_layer = bm_data.uv_layers.active
    if uv_layer is not None:
        loop_uvs = [tuple(d.uv) for d in uv_layer.data]
    else:
        loop_uvs = [(0.0, 0.0)] * len(bm_data.loops)

    # Lightweight signature of the base mesh so if the base mesh is inverted/edited, cache updates
    v0 = src_verts[0]
    vn = src_verts[-1]
    sig = (base_obj.name, len(src_verts), len(polys), axis, round(v0.x, 5), round(v0.y, 5), round(vn.x, 5), round(vn.y, 5))

    return {
        "sig": sig,
        "n_verts": len(src_verts),
        "n_edges": len(edges),
        "n_polys": len(polys),
        "n_loops": len(bm_data.loops),
        "vert_params": vert_params,
        "vert_fac": [vp[0] for vp in vert_params],
        "edges": edges,
        "polys": polys,
        "polys_rev": polys_rev,
        "sharp_edges": sharp_edges,
        "loop_uvs": loop_uvs,
        "loop_uvs_rev": [loop_uvs[li] for li in rev_loop_order],
    }


def _get_or_create_uv_preview_material(view_mode):
    """Return or create a preview material ('UV_CHECKER', 'UV_GRADIENT', 'FEATHER_ID', 'XRAY')."""
    if view_mode == 'UV_CHECKER':
        mat_name = "AranFeather_UV_Checker_Mat"
        img_name = "AranFeather_UV_ColorGrid"
        img = bpy.data.images.get(img_name)
        if img is None:
            img = bpy.data.images.new(img_name, width=1024, height=1024, alpha=False)
            img.generated_type = 'COLOR_GRID'

        mat = bpy.data.materials.get(mat_name)
        if mat is None:
            mat = bpy.data.materials.new(name=mat_name)
        mat.use_nodes = True
        nt = mat.node_tree
        nt.nodes.clear()

        out_node = nt.nodes.new("ShaderNodeOutputMaterial")
        out_node.location = (300, 0)
        bsdf = nt.nodes.new("ShaderNodeBsdfPrincipled")
        bsdf.location = (0, 0)
        bsdf.inputs["Roughness"].default_value = 0.8
        tex_node = nt.nodes.new("ShaderNodeTexImage")
        tex_node.location = (-300, 0)
        tex_node.image = img
        uv_node = nt.nodes.new("ShaderNodeUVMap")
        uv_node.location = (-520, 0)
        uv_node.uv_map = "UVMap"

        nt.links.new(uv_node.outputs["UV"], tex_node.inputs["Vector"])
        nt.links.new(tex_node.outputs["Color"], bsdf.inputs["Base Color"])
        nt.links.new(bsdf.outputs["BSDF"], out_node.inputs["Surface"])

        # Setting tex_node as active makes Blender's SOLID + TEXTURE shading display this UV grid!
        for n in nt.nodes:
            n.select = False
        tex_node.select = True
        nt.nodes.active = tex_node
        return mat

    elif view_mode == 'UV_GRADIENT':
        mat_name = "AranFeather_UV_Gradient_Mat"
        mat = bpy.data.materials.get(mat_name)
        if mat is None:
            mat = bpy.data.materials.new(name=mat_name)
        mat.use_nodes = True
        nt = mat.node_tree
        nt.nodes.clear()

        out_node = nt.nodes.new("ShaderNodeOutputMaterial")
        out_node.location = (300, 0)
        bsdf = nt.nodes.new("ShaderNodeBsdfPrincipled")
        bsdf.location = (0, 0)
        bsdf.inputs["Roughness"].default_value = 0.8
        uv_node = nt.nodes.new("ShaderNodeUVMap")
        uv_node.location = (-300, 0)
        uv_node.uv_map = "UVMap"

        nt.links.new(uv_node.outputs["UV"], bsdf.inputs["Base Color"])
        nt.links.new(bsdf.outputs["BSDF"], out_node.inputs["Surface"])
        return mat

    elif view_mode == 'FEATHER_ID':
        mat_name = "AranFeather_ID_Mat"
        mat = bpy.data.materials.get(mat_name)
        if mat is None:
            mat = bpy.data.materials.new(name=mat_name)
        mat.use_nodes = True
        nt = mat.node_tree
        nt.nodes.clear()

        out_node = nt.nodes.new("ShaderNodeOutputMaterial")
        out_node.location = (300, 0)
        bsdf = nt.nodes.new("ShaderNodeBsdfPrincipled")
        bsdf.location = (0, 0)
        bsdf.inputs["Roughness"].default_value = 0.75
        attr_node = nt.nodes.new("ShaderNodeAttribute")
        attr_node.location = (-300, 0)
        attr_node.attribute_name = "_AranUVColor"

        nt.links.new(attr_node.outputs["Color"], bsdf.inputs["Base Color"])
        nt.links.new(bsdf.outputs["BSDF"], out_node.inputs["Surface"])
        return mat

    elif view_mode == 'XRAY':
        mat_name = "AranFeather_XRay_Mat"
        mat = bpy.data.materials.get(mat_name)
        if mat is None:
            mat = bpy.data.materials.new(name=mat_name)
        mat.diffuse_color = (0.25, 0.65, 1.0, 0.30)
        if hasattr(mat, "surface_render_method"):
            mat.surface_render_method = 'BLENDED'
        elif hasattr(mat, "blend_method"):
            mat.blend_method = 'BLEND'
        mat.use_nodes = True
        nt = mat.node_tree
        nt.nodes.clear()

        out_node = nt.nodes.new("ShaderNodeOutputMaterial")
        out_node.location = (300, 0)
        bsdf = nt.nodes.new("ShaderNodeBsdfPrincipled")
        bsdf.location = (0, 0)
        bsdf.inputs["Base Color"].default_value = (0.25, 0.65, 1.0, 1.0)
        bsdf.inputs["Alpha"].default_value = 0.30
        nt.links.new(bsdf.outputs["BSDF"], out_node.inputs["Surface"])
        return mat

    return None


_SAVED_VIEWPORT_COLOR_TYPES = {}


def _sync_viewport_shading_for_view_mode(view_mode):
    """Ensure the 3D Viewport immediately shows the selected View Mode in Solid mode."""
    if bpy.app.background:
        return
    wm = getattr(bpy.context, "window_manager", None)
    if wm is None:
        return
    for win in wm.windows:
        if win.screen is None:
            continue
        for area in win.screen.areas:
            if area.type != 'VIEW_3D':
                continue
            space = area.spaces.active
            if space is None or not hasattr(space, "shading"):
                continue
            sh = space.shading
            area_key = area.as_pointer()
            if view_mode == 'UV_CHECKER':
                if area_key not in _SAVED_VIEWPORT_COLOR_TYPES:
                    _SAVED_VIEWPORT_COLOR_TYPES[area_key] = sh.color_type
                if sh.type == 'SOLID':
                    sh.color_type = 'TEXTURE'
            elif view_mode in ('UV_GRADIENT', 'FEATHER_ID'):
                if area_key not in _SAVED_VIEWPORT_COLOR_TYPES:
                    _SAVED_VIEWPORT_COLOR_TYPES[area_key] = sh.color_type
                if sh.type == 'SOLID':
                    sh.color_type = 'VERTEX'
            elif view_mode == 'XRAY':
                if area_key not in _SAVED_VIEWPORT_COLOR_TYPES:
                    _SAVED_VIEWPORT_COLOR_TYPES[area_key] = sh.color_type
                if sh.type == 'SOLID':
                    sh.color_type = 'MATERIAL'
            else:
                prev_ct = _SAVED_VIEWPORT_COLOR_TYPES.pop(area_key, None)
                if prev_ct is not None and sh.type == 'SOLID':
                    sh.color_type = prev_ct
            area.tag_redraw()


def _capture_shape_keys(out_obj):
    """Snapshot the generated mesh's shape keys as offsets from their basis (or None).

    Regeneration rewrites the base positions; shape keys (Fix Overlaps, or the artist's own)
    are kept as *offsets* and re-applied on top of the new positions.
    """
    mesh = out_obj.data
    sk = mesh.shape_keys
    if sk is None or len(sk.key_blocks) < 2:
        return None
    n = len(sk.reference_key.data)
    basis = np.empty(n * 3, dtype=np.float32)
    sk.reference_key.data.foreach_get("co", basis)
    keys = []
    for kb in sk.key_blocks:
        if kb == sk.reference_key:
            continue
        co = np.empty(n * 3, dtype=np.float32)
        kb.data.foreach_get("co", co)
        keys.append({
            "name": kb.name, "offsets": co - basis, "value": kb.value, "mute": kb.mute,
            "slider_min": kb.slider_min, "slider_max": kb.slider_max,
        })
    return {"basis_name": sk.reference_key.name, "keys": keys, "active": out_obj.active_shape_key_index}


def _restore_shape_keys(out_obj, captured, flat_co):
    """Rebuild captured shape keys on new base positions `flat_co` (dropped if the vertex count changed)."""
    mesh = out_obj.data
    n = len(mesh.vertices)
    base = np.asarray(flat_co, dtype=np.float32)
    if base.size != n * 3 or any(k["offsets"].size != n * 3 for k in captured["keys"]):
        if mesh.shape_keys is not None:
            out_obj.shape_key_clear()
        print(f"[AranTools] Shape keys on '{out_obj.name}' dropped: feather topology changed.")
        return
    sk = mesh.shape_keys
    if sk is None or len(sk.key_blocks) != len(captured["keys"]) + 1:
        if sk is not None:
            out_obj.shape_key_clear()
        out_obj.shape_key_add(name=captured["basis_name"], from_mix=False)
        for k in captured["keys"]:
            out_obj.shape_key_add(name=k["name"], from_mix=False)
        sk = mesh.shape_keys
    sk.reference_key.data.foreach_set("co", base)
    others = [kb for kb in sk.key_blocks if kb != sk.reference_key]
    for kb, k in zip(others, captured["keys"]):
        kb.data.foreach_set("co", base + k["offsets"])
        kb.slider_min = k["slider_min"]
        kb.slider_max = k["slider_max"]
        kb.value = k["value"]
        kb.mute = k["mute"]
    out_obj.active_shape_key_index = min(captured["active"], len(sk.key_blocks) - 1)


def regenerate_feather_object(guide_obj, force_full_rebuild=False, coords_only_hint=False):
    """Run the pure-Python feather generator on `guide_obj` and update its paired output mesh.

    Uses per-chain caching and an in-place `foreach_set("co", ...)` fast-path so dragging
    feathers in Edit Mode runs in ~1 ms per frame!
    """
    global _IS_GENERATING
    if _IS_GENERATING or guide_obj is None or guide_obj.type != 'MESH':
        return None

    fprops = getattr(guide_obj, "arantools_feather", None)
    if fprops is None or not fprops.is_feather_guide:
        return None

    _IS_GENERATING = True
    try:
        out_obj = fprops.output_object
        created_new = False
        if out_obj is None or out_obj.name not in bpy.data.objects:
            out_mesh = bpy.data.meshes.new(f"{guide_obj.name}_GeneratedMesh")
            out_obj = bpy.data.objects.new(f"{guide_obj.name}_Generated", out_mesh)
            for coll in guide_obj.users_collection:
                if out_obj.name not in coll.objects:
                    coll.objects.link(out_obj)
            out_obj.parent = guide_obj
            out_obj.matrix_parent_inverse = Matrix.Identity(4)
            out_obj.arantools_feather.is_feather_output = True
            out_obj.arantools_feather.guide_object = guide_obj
            fprops.output_object = out_obj
            created_new = True

        obj_cache = _RUNTIME_CACHE.setdefault(
            guide_obj.name,
            {
                "topo_sig": None,
                "chain_cache": {},
                "base_caches": {},
                "last_verts_co": None,
                "guide_meta": None,
                "overlay_data": None,
                "gpu_batches": None,
                "pick_bvh": None,
            },
        )
        # Any regeneration may move or renumber the cards -> pick BVH / overlay fills are stale.
        obj_cache["pick_bvh"] = None
        obj_cache["gen_id"] = obj_cache.get("gen_id", 0) + 1
        if force_full_rebuild or created_new:
            obj_cache["topo_sig"] = None
            obj_cache["chain_cache"].clear()
            obj_cache["base_caches"].clear()
            obj_cache["last_verts_co"] = None
            obj_cache["guide_meta"] = None
            obj_cache["overlay_data"] = None
            obj_cache["gpu_batches"] = None

        guide_meta = obj_cache.get("guide_meta")
        if coords_only_hint and guide_obj.mode == 'EDIT' and guide_meta is not None:
            bm = bmesh.from_edit_mesh(guide_obj.data)
            if len(bm.verts) == guide_meta["n_verts"] and len(bm.edges) == guide_meta["n_edges"]:
                verts_co = [(v.co.x, v.co.y, v.co.z) for v in bm.verts]
                edges_v = guide_meta["edges_v"]
                chains = guide_meta["chains"]
                pt_layers = guide_meta["pt_layers"]
                edge_int = guide_meta["edge_int"]
                edge_bool = guide_meta["edge_bool"]
                edge_up = guide_meta["edge_up"]
            else:
                verts_co, edges_v, pt_layers, edge_int, edge_bool, edge_up = _extract_guide_data(guide_obj)
                chains = _trace_chains_from_edges(len(verts_co), edges_v)
                obj_cache["guide_meta"] = {
                    "n_verts": len(verts_co),
                    "n_edges": len(edges_v),
                    "edges_v": edges_v,
                    "chains": chains,
                    "pt_layers": pt_layers,
                    "edge_int": edge_int,
                    "edge_bool": edge_bool,
                    "edge_up": edge_up,
                }
        else:
            verts_co, edges_v, pt_layers, edge_int, edge_bool, edge_up = _extract_guide_data(guide_obj)
            chains = _trace_chains_from_edges(len(verts_co), edges_v)
            obj_cache["guide_meta"] = {
                "n_verts": len(verts_co),
                "n_edges": len(edges_v),
                "edges_v": edges_v,
                "chains": chains,
                "pt_layers": pt_layers,
                "edge_int": edge_int,
                "edge_bool": edge_bool,
                "edge_up": edge_up,
            }

        axis = int(fprops.deform_axis)
        invert_profile = bool(fprops.invert_curve_profile)
        slots = [slot.mesh_obj for slot in fprops.mesh_slots]

        base_caches = []
        base_sigs = []
        cached_bases = obj_cache["base_caches"]
        for s_idx, mobj in enumerate(slots):
            if mobj is None or mobj.type != 'MESH' or mobj.data is None or len(mobj.data.vertices) == 0:
                base_caches.append(None)
                base_sigs.append(None)
                continue
            bm_data = mobj.data
            v0 = bm_data.vertices[0].co
            vn = bm_data.vertices[-1].co
            fast_sig = (mobj.name, len(bm_data.vertices), len(bm_data.polygons), axis, round(v0.x, 5), round(v0.y, 5), round(vn.x, 5), round(vn.y, 5))
            prev_b = cached_bases.get(s_idx)
            if prev_b is not None and prev_b["sig"] == fast_sig:
                bcache = prev_b
            else:
                bcache = _prepare_base_mesh_cache(mobj, axis)
                cached_bases[s_idx] = bcache
            base_caches.append(bcache)
            base_sigs.append(fast_sig)

        chain_cache = obj_cache["chain_cache"]
        new_chain_cache = {}
        orig_cards = []    # (c_idx, slot_idx, reflected, flat_coords)
        mirror_cards = []  # same, for the internal mirror copies

        rad_list = pt_layers["radius"]
        tilt_list = pt_layers["tilt"]
        smooth_list = pt_layers["smooth"]
        pushout_list = pt_layers["pushout"]
        pushtip_list = pt_layers["pushtip"]
        follow_list = pt_layers["followsurface"]
        tiltsurf_list = pt_layers["tilttosurface"]
        slot_list = edge_int["feather_mesh_slot"]
        mask_list = edge_int["feather_mesh_mask"]
        mirror_mask_list = edge_int["feather_mirror_mask"]
        seed_list = edge_int["feather_seed"]
        subdiv_list = edge_int["feather_subdiv"]
        st_list = edge_int["feather_smooth_tilt"]
        ss_list = edge_int["feather_smooth_spline"]
        rev_list = edge_bool["feather_reverse"]
        mir_list = edge_bool["feather_mirror"]
        global_seed = int(getattr(fprops, "random_seed", 1))
        random_flip = float(getattr(fprops, "random_flip", 0.0))

        # Internal mirror: reflection across the Mirror Object's X plane (or the output object's own
        # local X), expressed in the output mesh's local space. Built into the mesh — no modifier.
        mirror_xform = None
        if fprops.use_mirror:
            out_mw = out_obj.matrix_world
            mirror_xform = out_mw.inverted_safe() @ _get_mirror_reflection_matrix(fprops) @ out_mw
        mirror_key = tuple(round(x, 6) for row in mirror_xform for x in row) if mirror_xform is not None else None

        def _resolve_slot(mask_val, fallback_slot, c_idx, seed):
            enabled = [
                s_i for s_i in range(len(base_caches))
                if (mask_val & (1 << s_i)) and base_caches[s_i] is not None
            ]
            if enabled:
                return _pick_random_slot_from_mask(enabled, c_idx, seed, global_seed)
            return fallback_slot

        any_surf_attr = (
            any(v != 0.0 for v in pushout_list)
            or any(v != 0.0 for v in pushtip_list)
            or any(v != 0.0 for v in follow_list)
            or any(v != 0.0 for v in tiltsurf_list)
        )
        if any_surf_attr:
            surf_bvh, surf_sig = _get_surface_bvh_cache(guide_obj, obj_cache)
        else:
            surf_bvh, surf_sig = None, None

        for c_idx, (v_chain, e_chain) in enumerate(chains):
            if len(v_chain) < 2 or not e_chain:
                continue

            e0 = e_chain[0]
            mask_val = mask_list[e0]
            if mask_val == 0:
                continue
            slot_idx = _resolve_slot(mask_val, slot_list[e0], c_idx, seed_list[e0]) if mask_val > 0 else slot_list[e0]
            if slot_idx < 0 or slot_idx >= len(base_caches):
                slot_idx = 0
            bcache = base_caches[slot_idx] if base_caches else None
            if bcache is None:
                continue

            # Mirror copy: same card as the original unless the feather has its own mirror cards ticked
            mirror_slot = None
            if mirror_xform is not None and mir_list[e0]:
                m_mask = mirror_mask_list[e0]
                mirror_slot = _resolve_slot(m_mask, slot_idx, c_idx, seed_list[e0] ^ 0x5BD1E995) if m_mask > 0 else slot_idx
                if base_caches[mirror_slot] is None:
                    mirror_slot = slot_idx

            # Random flip (card reflected across its own spine), decided independently per side
            flip_o = random_flip > 0.0 and _hash01(c_idx, seed_list[e0], global_seed, 1) < random_flip
            flip_m = random_flip > 0.0 and _hash01(c_idx, seed_list[e0], global_seed, 2) < random_flip

            is_reversed = any(rev_list[ei] for ei in e_chain)
            ordered_v = list(reversed(v_chain)) if is_reversed else list(v_chain)

            subdiv_level = max(0, min(5, subdiv_list[e0]))
            smooth_tilt_iters = max(0, st_list[e0])
            smooth_spline_iters = max(0, ss_list[e0])
            up_tuple = edge_up[e0]

            # Build per-chain cache key
            v_key = tuple(
                (
                    verts_co[vi],
                    rad_list[vi],
                    tilt_list[vi],
                    smooth_list[vi],
                    pushout_list[vi],
                    pushtip_list[vi],
                    follow_list[vi],
                    tiltsurf_list[vi],
                )
                for vi in ordered_v
            )
            c_key = (
                bcache["sig"],
                base_caches[mirror_slot]["sig"] if mirror_slot is not None else None,
                flip_o,
                flip_m,
                mirror_key,
                is_reversed,
                subdiv_level,
                smooth_tilt_iters,
                smooth_spline_iters,
                invert_profile,
                up_tuple,
                surf_sig,
                v_key,
            )

            cached = chain_cache.get(c_idx)
            if cached is not None and cached[0] == c_key:
                c_flat, m_flat = cached[1], cached[2]
            else:
                n_base = Vector(up_tuple)
                if n_base.length_squared < 1e-12:
                    n_base = Vector((0.0, 1.0, 0.0))
                else:
                    n_base = n_base.normalized()

                frames = _evaluate_chain_50pts(
                    [verts_co[vi][0] for vi in ordered_v],
                    [verts_co[vi][1] for vi in ordered_v],
                    [verts_co[vi][2] for vi in ordered_v],
                    [rad_list[vi] * 0.1 for vi in ordered_v],
                    [tilt_list[vi] for vi in ordered_v],
                    [smooth_list[vi] for vi in ordered_v],
                    subdiv_level,
                    smooth_tilt_iters,
                    smooth_spline_iters,
                    n_base,
                    invert_profile,
                    pushout_arr=[pushout_list[vi] for vi in ordered_v],
                    pushtip_arr=[pushtip_list[vi] for vi in ordered_v],
                    follow_arr=[follow_list[vi] for vi in ordered_v],
                    tiltsurf_arr=[tiltsurf_list[vi] for vi in ordered_v],
                    surf_bvh=surf_bvh,
                )
                c_flat = _deform_card(frames, bcache, flip=flip_o)
                m_flat = None
                if mirror_slot is not None:
                    m_flat = _deform_card(frames, base_caches[mirror_slot], flip=flip_m, xform=mirror_xform)
            new_chain_cache[c_idx] = (c_key, c_flat, m_flat)

            # A card is "reflected" (needs reversed winding) when exactly one of mirror / flip applies
            orig_cards.append((c_idx, slot_idx, flip_o, c_flat))
            if m_flat is not None:
                mirror_cards.append((c_idx, mirror_slot, not flip_m, m_flat))

        # Output layout: all original cards first, then all mirror copies.
        flat_all_verts = []
        card_layout = []   # (slot_idx, reflected) per card, in output order -> topology signature
        chain_ranges = []
        mirror_ranges = []
        vert_t = []        # per output vertex: 0 = feather root, 1 = tip (Fix Overlaps pinning / falloff)
        cur_v_ofs = cur_p_ofs = cur_l_ofs = 0
        for cards, ranges in ((orig_cards, chain_ranges), (mirror_cards, mirror_ranges)):
            for c_idx, s_idx, reflected, flat in cards:
                bc = base_caches[s_idx]
                ranges.append((
                    c_idx,
                    cur_v_ofs, cur_v_ofs + bc["n_verts"],
                    cur_p_ofs, cur_p_ofs + bc["n_polys"],
                    cur_l_ofs, cur_l_ofs + bc["n_loops"],
                ))
                cur_v_ofs += bc["n_verts"]
                cur_p_ofs += bc["n_polys"]
                cur_l_ofs += bc["n_loops"]
                card_layout.append((s_idx, reflected))
                flat_all_verts.extend(flat)
                vert_t.extend(bc["vert_fac"])

        obj_cache["chain_cache"] = new_chain_cache
        obj_cache["chain_ranges"] = chain_ranges
        obj_cache["mirror_ranges"] = mirror_ranges
        # Inverted base meshes (Flip Keep Look) put the visual root at the card's far end
        vt = np.asarray(vert_t, dtype=np.float32)
        obj_cache["vert_t"] = (1.0 - vt) if invert_profile else vt
        obj_cache["last_verts_co"] = list(verts_co)
        obj_cache["overlay_data"] = (verts_co, chains, list(rev_list))
        obj_cache["gpu_batches"] = None

        view_mode = getattr(fprops, "view_mode", 'MATERIAL')
        base_mat = fprops.material
        if view_mode in ('UV_CHECKER', 'UV_GRADIENT', 'FEATHER_ID', 'XRAY'):
            active_mat = _get_or_create_uv_preview_material(view_mode) or base_mat
        else:
            active_mat = base_mat

        out_obj.show_wire = (view_mode == 'XRAY')
        mat_name = active_mat.name if active_mat else None
        topo_sig = (tuple(card_layout), tuple(base_sigs), mat_name, view_mode)

        out_mesh = out_obj.data
        total_verts = len(flat_all_verts) // 3

        # ── FAST PATH: Topology is unchanged -> update vertex positions in-place! ──
        if (
            not force_full_rebuild
            and obj_cache["topo_sig"] == topo_sig
            and len(out_mesh.vertices) == total_verts
            and total_verts > 0
        ):
            captured_keys = _capture_shape_keys(out_obj)
            out_mesh.vertices.foreach_set("co", flat_all_verts)
            if captured_keys is not None:
                _restore_shape_keys(out_obj, captured_keys, flat_all_verts)
            out_mesh.update_tag()
            out_obj.update_tag(refresh={'DATA'})
            return out_obj

        # ── FULL REBUILD PATH: Build topology, UVs, sharp_edge, Material ──
        all_verts = [
            (flat_all_verts[i], flat_all_verts[i + 1], flat_all_verts[i + 2])
            for i in range(0, len(flat_all_verts), 3)
        ]
        all_edges = []
        all_polys = []
        all_sharp = []
        all_uvs = []
        v_offset = 0

        for slot_idx, reflected in card_layout:
            bcache = base_caches[slot_idx]
            for e_u, e_v in bcache["edges"]:
                all_edges.append((e_u + v_offset, e_v + v_offset))
            for poly in bcache["polys_rev" if reflected else "polys"]:
                all_polys.append(tuple(vi + v_offset for vi in poly))
            all_sharp.extend(bcache["sharp_edges"])
            all_uvs.extend(bcache["loop_uvs_rev" if reflected else "loop_uvs"])
            v_offset += bcache["n_verts"]

        captured_keys = _capture_shape_keys(out_obj)
        if out_mesh.shape_keys is not None:
            out_obj.shape_key_clear()
        out_mesh.clear_geometry()
        if all_verts:
            out_mesh.from_pydata(all_verts, all_edges, all_polys)
            out_mesh.update()

            out_mesh.polygons.foreach_set("use_smooth", [True] * len(out_mesh.polygons))

            uv_layer = out_mesh.uv_layers.get("UVMap")
            if uv_layer is None:
                uv_layer = out_mesh.uv_layers.new(name="UVMap")
            out_mesh.uv_layers.active = uv_layer
            if len(uv_layer.data) == len(all_uvs):
                flat_uvs = [coord for uv in all_uvs for coord in uv]
                uv_layer.data.foreach_set("uv", flat_uvs)

            sharp_attr = out_mesh.attributes.get("sharp_edge")
            if sharp_attr is None:
                sharp_attr = out_mesh.attributes.new(name="sharp_edge", type='BOOLEAN', domain='EDGE')
            if len(sharp_attr.data) == len(all_sharp):
                sharp_attr.data.foreach_set("value", all_sharp)

            # Remove any black 'Color' attribute so SOLID + VERTEX viewport shading never turns feathers black!
            old_col = out_mesh.attributes.get("Color")
            if old_col is not None:
                out_mesh.attributes.remove(old_col)

            uv_col_attr = out_mesh.attributes.get("_AranUVColor")
            if view_mode == 'UV_GRADIENT':
                if uv_col_attr is None:
                    uv_col_attr = out_mesh.attributes.new(name="_AranUVColor", type='FLOAT_COLOR', domain='CORNER')
                if len(uv_col_attr.data) == len(all_uvs):
                    flat_cols = []
                    for u, v in all_uvs:
                        flat_cols.extend((max(0.0, min(1.0, u)), max(0.0, min(1.0, v)), 0.15, 1.0))
                    uv_col_attr.data.foreach_set("color", flat_cols)
                out_mesh.color_attributes.active_color = uv_col_attr
            elif view_mode == 'FEATHER_ID':
                if uv_col_attr is None:
                    uv_col_attr = out_mesh.attributes.new(name="_AranUVColor", type='FLOAT_COLOR', domain='CORNER')
                if len(uv_col_attr.data) == len(all_uvs):
                    flat_cols = []
                    for c_idx, _, _, _, _, l_start, l_end in chain_ranges + mirror_ranges:
                        hue = (c_idx * 0.618033988749895) % 1.0
                        r, g, b = colorsys.hsv_to_rgb(hue, 0.72, 0.95)
                        flat_cols.extend((r, g, b, 1.0) * (l_end - l_start))
                    uv_col_attr.data.foreach_set("color", flat_cols)
                out_mesh.color_attributes.active_color = uv_col_attr
            else:
                if uv_col_attr is not None:
                    out_mesh.attributes.remove(uv_col_attr)

            if active_mat is not None:
                out_mesh.materials.clear()
                out_mesh.materials.append(active_mat)
                out_mesh.polygons.foreach_set("material_index", [0] * len(out_mesh.polygons))

            if base_mat is not None and guide_obj.mode != 'EDIT':
                if len(guide_obj.data.materials) == 0 or guide_obj.data.materials[0] != base_mat:
                    guide_obj.data.materials.clear()
                    guide_obj.data.materials.append(base_mat)

        if captured_keys is not None:
            _restore_shape_keys(out_obj, captured_keys, flat_all_verts)

        obj_cache["topo_sig"] = topo_sig
        return out_obj
    finally:
        _IS_GENERATING = False


# ============================================================================
# Helper Functions for Resolving Feather Object & Live Editing in Edit Mode
# ============================================================================

def get_feather_guide_from_context(context):
    """Return the active Feather Guide object if either a Guide or Generated mesh is active."""
    obj = context.active_object
    if obj is None or obj.type != 'MESH':
        return None
    fprops = getattr(obj, "arantools_feather", None)
    if fprops is None:
        return None
    if fprops.is_feather_guide:
        return obj
    if fprops.is_feather_output and fprops.guide_object is not None:
        return fprops.guide_object
    return None


def find_or_add_mesh_slot(guide_obj, target_mesh_obj):
    """Return the index of `target_mesh_obj` in `guide_obj.arantools_feather.mesh_slots`, adding if needed."""
    if target_mesh_obj is None:
        return 0
    fprops = guide_obj.arantools_feather
    for idx, slot in enumerate(fprops.mesh_slots):
        if slot.mesh_obj == target_mesh_obj:
            return idx
    new_slot = fprops.mesh_slots.add()
    new_slot.mesh_obj = target_mesh_obj
    return len(fprops.mesh_slots) - 1


def _get_collection_feather_meshes(coll, fprops):
    """Return sorted valid feather mesh objects inside `coll`."""
    if coll is None:
        return []
    candidates = [
        o for o in coll.all_objects
        if _poll_mesh_object(fprops, o) and len(o.data.polygons) > 0
    ]
    # If the user picked a shared collection that also holds this guide's surface / mirror objects,
    # leave those out (identified by the guide's own pointers, never by object name).
    excluded = {fprops.surface_object, fprops.mirror_object} - {None}
    filtered = [o for o in candidates if o not in excluded]
    result = filtered if filtered else candidates
    return sorted(result, key=lambda o: o.name)


def sync_admitted_collection_to_slots(guide_obj):
    """Ensure every mesh in `guide_obj.arantools_feather.admitted_collection` has a slot in `mesh_slots`."""
    if guide_obj is None:
        return []
    fprops = guide_obj.arantools_feather
    coll = fprops.admitted_collection
    if coll is None:
        return [(idx, s.mesh_obj) for idx, s in enumerate(fprops.mesh_slots) if s.mesh_obj is not None]

    meshes = _get_collection_feather_meshes(coll, fprops)
    out = []
    for mobj in meshes:
        s_idx = find_or_add_mesh_slot(guide_obj, mobj)
        out.append((s_idx, mobj))
    return out



def get_admitted_mesh_slots(guide_obj):
    """Return `[(slot_idx, mesh_obj), ...]` for the admitted feather meshes of `guide_obj`."""
    if guide_obj is None:
        return []
    fprops = guide_obj.arantools_feather
    if fprops.admitted_collection is not None:
        # Read without mutating ID properties during UI draw if all slots already exist
        meshes = _get_collection_feather_meshes(fprops.admitted_collection, fprops)
        slot_map = {s.mesh_obj: idx for idx, s in enumerate(fprops.mesh_slots) if s.mesh_obj is not None}
        out = []
        for mobj in meshes:
            if mobj in slot_map:
                out.append((slot_map[mobj], mobj))
            else:
                out.append((-1, mobj))
        return out
    return [(idx, s.mesh_obj) for idx, s in enumerate(fprops.mesh_slots) if s.mesh_obj is not None]


def _get_object_mode_selected_edge_indices(guide_obj, whole_chain=True):
    """Return list of edge indices selected on `guide_obj.data` in Object Mode (expanded to full chains if `whole_chain`).
    If nothing is selected, returns all edge indices.
    """
    if guide_obj is None or guide_obj.data is None:
        return []
    mesh = guide_obj.data
    n_edges = len(mesh.edges)
    if n_edges == 0:
        return []

    sel_e = {i for i, e in enumerate(mesh.edges) if e.select}
    sel_v = {i for i, v in enumerate(mesh.vertices) if v.select}

    if not sel_e and not sel_v:
        return list(range(n_edges))

    if not whole_chain:
        return sorted(sel_e)

    chains = _get_guide_chains_fast(guide_obj)
    out_edges = []
    for v_chain, e_chain in chains:
        if any(ei in sel_e for ei in e_chain) or any(vi in sel_v for vi in v_chain):
            out_edges.extend(e_chain)
    return out_edges


def _get_selected_chain_indices(guide_obj):
    """Return sorted list of currently selected feather chain indices `c_idx` in either Edit Mode or Object Mode."""
    if guide_obj is None or guide_obj.data is None:
        return []
    chains = _get_guide_chains_fast(guide_obj)
    if not chains:
        return []

    if guide_obj.mode == 'EDIT':
        try:
            bm = bmesh.from_edit_mesh(guide_obj.data)
            sel_v = {v.index for v in bm.verts if v.select and not v.hide}
            sel_e = {e.index for e in bm.edges if e.select and not e.hide}
            return [
                ci for ci, (vc, ec) in enumerate(chains)
                if any(vi in sel_v for vi in vc) or any(ei in sel_e for ei in ec)
            ]
        except Exception:
            return []
    else:
        mesh = guide_obj.data
        sel_e = {i for i, e in enumerate(mesh.edges) if e.select}
        sel_v = {i for i, v in enumerate(mesh.vertices) if v.select}
        if not sel_e and not sel_v:
            return []
        return [
            ci for ci, (vc, ec) in enumerate(chains)
            if any(vi in sel_v for vi in vc) or any(ei in sel_e for ei in ec)
        ]


def get_selected_feathers_mask_state(guide_obj):
    """Return `(and_mask, or_mask, n_selected_edges)` for the currently selected feather(s)."""
    if guide_obj is None:
        return 0, 0, 0
    if guide_obj.mode == 'EDIT':
        bm = bmesh.from_edit_mesh(guide_obj.data)
        _, chain_edges = _get_selected_bm_elements(bm, whole_chain=True)
        if not chain_edges:
            chain_edges = [e for e in bm.edges if not e.hide]
        if not chain_edges:
            return 0, 0, 0
        mask_layer = bm.edges.layers.int.get("feather_mesh_mask")
        slot_layer = bm.edges.layers.int.get("feather_mesh_slot")
        or_m = 0
        and_m = None
        for e in chain_edges:
            mval = int(e[mask_layer]) if mask_layer is not None else -1
            if mval < 0:
                sval = int(e[slot_layer]) if slot_layer is not None else 0
                mval = 1 << max(0, min(29, sval))
            or_m |= mval
            and_m = mval if and_m is None else (and_m & mval)
        return (and_m if and_m is not None else 0), or_m, len(chain_edges)
    else:
        mesh = guide_obj.data
        mask_attr = mesh.attributes.get("feather_mesh_mask")
        slot_attr = mesh.attributes.get("feather_mesh_slot")
        target_e_indices = _get_object_mode_selected_edge_indices(guide_obj, whole_chain=True)
        if not target_e_indices:
            return 0, 0, 0
        or_m = 0
        and_m = None
        for i in target_e_indices:
            mval = int(mask_attr.data[i].value) if mask_attr else -1
            if mval < 0:
                sval = int(slot_attr.data[i].value) if slot_attr else 0
                mval = 1 << max(0, min(29, sval))
            or_m |= mval
            and_m = mval if and_m is None else (and_m & mval)
        return (and_m if and_m is not None else 0), or_m, len(target_e_indices)


def _get_selected_bm_elements(bm, whole_chain=True):
    """Return `(selected_verts, selected_edges)` in Edit Mode (ignoring hidden elements)."""
    bm.verts.ensure_lookup_table()
    bm.edges.ensure_lookup_table()

    sel_verts = [v for v in bm.verts if v.select and not v.hide]
    sel_edges = [e for e in bm.edges if e.select and not e.hide]

    if not whole_chain:
        return sel_verts, sel_edges

    seed_verts = set(sel_verts)
    for e in sel_edges:
        seed_verts.add(e.verts[0])
        seed_verts.add(e.verts[1])

    visited_v = set(seed_verts)
    queue = list(seed_verts)
    chain_edges = set(sel_edges)

    while queue:
        v = queue.pop()
        for e in v.link_edges:
            chain_edges.add(e)
            other = e.other_vert(v)
            if other not in visited_v:
                visited_v.add(other)
                queue.append(other)

    return sel_verts, list(chain_edges)


def sync_ui_from_selection(context):
    """Read metadata from the active/selected vertex & edge (in Edit Mode or Feather Mode) into Scene UI properties."""
    global _IS_SYNCING_UI
    if _IS_SYNCING_UI:
        return
    guide_obj = get_feather_guide_from_context(context)
    if guide_obj is None:
        return
    ui = getattr(context.scene, "arantools_feather_ui", None)
    if ui is None:
        return

    _IS_SYNCING_UI = True
    try:
        if guide_obj.mode == 'EDIT':
            bm = bmesh.from_edit_mesh(guide_obj.data)
            bm.verts.ensure_lookup_table()
            bm.edges.ensure_lookup_table()

            act_v = bm.select_history.active if isinstance(bm.select_history.active, bmesh.types.BMVert) else None
            if act_v is None:
                act_v = next((v for v in bm.verts if v.select and not v.hide), None)

            act_e = bm.select_history.active if isinstance(bm.select_history.active, bmesh.types.BMEdge) else None
            if act_e is None:
                act_e = next((e for e in bm.edges if e.select and not e.hide), None)
            if act_e is None and act_v is not None and act_v.link_edges:
                act_e = act_v.link_edges[0]

            if act_v is not None:
                for name, _ in POINT_FLOAT_ATTRS:
                    layer = bm.verts.layers.float.get(name)
                    if layer is not None:
                        setattr(ui, f"vert_{name}", float(act_v[layer]))

            if act_e is not None:
                slot_layer = bm.edges.layers.int.get("feather_mesh_slot")
                if slot_layer is not None:
                    s_idx = int(act_e[slot_layer])
                    slots = guide_obj.arantools_feather.mesh_slots
                    if 0 <= s_idx < len(slots):
                        ui.edge_mesh_obj = slots[s_idx].mesh_obj
                        guide_obj.arantools_feather.active_slot_index = s_idx

                rev_layer = bm.edges.layers.bool.get("feather_reverse")
                if rev_layer is not None:
                    ui.edge_reverse = bool(act_e[rev_layer])

                subdiv_layer = bm.edges.layers.int.get("feather_subdiv")
                if subdiv_layer is not None:
                    ui.edge_subdiv = int(act_e[subdiv_layer])

                st_layer = bm.edges.layers.int.get("feather_smooth_tilt")
                if st_layer is not None:
                    ui.edge_smooth_tilt = int(act_e[st_layer])

                ss_layer = bm.edges.layers.int.get("feather_smooth_spline")
                if ss_layer is not None:
                    ui.edge_smooth_spline = int(act_e[ss_layer])
        else:
            # Object Mode: read from the first selected edge & vertex
            mesh = guide_obj.data
            sel_e_indices = _get_object_mode_selected_edge_indices(guide_obj, whole_chain=True)
            if sel_e_indices:
                ei = sel_e_indices[0]
                vi = mesh.edges[ei].vertices[0]
                for name, _ in POINT_FLOAT_ATTRS:
                    attr = mesh.attributes.get(name)
                    if attr is not None and vi < len(attr.data):
                        setattr(ui, f"vert_{name}", float(attr.data[vi].value))

                slot_attr = mesh.attributes.get("feather_mesh_slot")
                if slot_attr is not None and ei < len(slot_attr.data):
                    s_idx = int(slot_attr.data[ei].value)
                    slots = guide_obj.arantools_feather.mesh_slots
                    if 0 <= s_idx < len(slots):
                        ui.edge_mesh_obj = slots[s_idx].mesh_obj
                        guide_obj.arantools_feather.active_slot_index = s_idx

                rev_attr = mesh.attributes.get("feather_reverse")
                if rev_attr is not None and ei < len(rev_attr.data):
                    ui.edge_reverse = bool(rev_attr.data[ei].value)

                subdiv_attr = mesh.attributes.get("feather_subdiv")
                if subdiv_attr is not None and ei < len(subdiv_attr.data):
                    ui.edge_subdiv = int(subdiv_attr.data[ei].value)

                st_attr = mesh.attributes.get("feather_smooth_tilt")
                if st_attr is not None and ei < len(st_attr.data):
                    ui.edge_smooth_tilt = int(st_attr.data[ei].value)

                ss_attr = mesh.attributes.get("feather_smooth_spline")
                if ss_attr is not None and ei < len(ss_attr.data):
                    ui.edge_smooth_spline = int(ss_attr.data[ei].value)
    finally:
        _IS_SYNCING_UI = False


def _make_vert_attr_updater(attr_name):
    def _update(self, context):
        if _IS_SYNCING_UI:
            return
        guide_obj = get_feather_guide_from_context(context)
        if guide_obj is None:
            return
        val = getattr(self, f"vert_{attr_name}")
        if guide_obj.mode == 'EDIT':
            bm = bmesh.from_edit_mesh(guide_obj.data)
            layer = bm.verts.layers.float.get(attr_name)
            if layer is None:
                layer = bm.verts.layers.float.new(attr_name)
            sel_verts, chain_edges = _get_selected_bm_elements(bm, whole_chain=self.apply_whole_chain)
            target_verts = sel_verts
            if self.apply_whole_chain and chain_edges:
                vset = set(sel_verts)
                for e in chain_edges:
                    vset.add(e.verts[0])
                    vset.add(e.verts[1])
                target_verts = list(vset)
            for v in target_verts:
                v[layer] = val
            bmesh.update_edit_mesh(guide_obj.data, loop_triangles=False, destructive=False)
        else:
            ensure_feather_guide_attributes(guide_obj.data)
            attr = guide_obj.data.attributes.get(attr_name)
            if attr is not None:
                sel_e = _get_object_mode_selected_edge_indices(guide_obj, whole_chain=True)
                target_v = set()
                for ei in sel_e:
                    e = guide_obj.data.edges[ei]
                    target_v.add(e.vertices[0])
                    target_v.add(e.vertices[1])
                if not target_v:
                    target_v = set(range(len(guide_obj.data.vertices)))
                for vi in target_v:
                    attr.data[vi].value = val
        if guide_obj.arantools_feather.auto_update:
            regenerate_feather_object(guide_obj)
    return _update


def _update_edge_mesh_obj(self, context):
    if _IS_SYNCING_UI:
        return
    guide_obj = get_feather_guide_from_context(context)
    if guide_obj is None or self.edge_mesh_obj is None:
        return
    slot_idx = find_or_add_mesh_slot(guide_obj, self.edge_mesh_obj)
    mask_bit = 1 << max(0, min(29, slot_idx))
    if guide_obj.mode == 'EDIT':
        bm = bmesh.from_edit_mesh(guide_obj.data)
        layer = bm.edges.layers.int.get("feather_mesh_slot") or bm.edges.layers.int.new("feather_mesh_slot")
        mask_layer = bm.edges.layers.int.get("feather_mesh_mask") or bm.edges.layers.int.new("feather_mesh_mask")
        _, target_edges = _get_selected_bm_elements(bm, whole_chain=True)
        for e in target_edges:
            e[layer] = slot_idx
            e[mask_layer] = mask_bit
        bmesh.update_edit_mesh(guide_obj.data, loop_triangles=False, destructive=False)
    else:
        ensure_feather_guide_attributes(guide_obj.data)
        target_e = _get_object_mode_selected_edge_indices(guide_obj, whole_chain=True)
        attr = guide_obj.data.attributes.get("feather_mesh_slot")
        if attr is not None:
            for ei in target_e:
                attr.data[ei].value = slot_idx
        mask_attr = guide_obj.data.attributes.get("feather_mesh_mask")
        if mask_attr is not None:
            for ei in target_e:
                mask_attr.data[ei].value = mask_bit
    if guide_obj.arantools_feather.auto_update:
        regenerate_feather_object(guide_obj)


def _update_admitted_collection(self, context):
    if _IS_GENERATING:
        return
    guide_obj = _resolve_owner_guide_obj(self, context)
    if guide_obj is not None:
        sync_admitted_collection_to_slots(guide_obj)
        regenerate_feather_object(guide_obj, force_full_rebuild=True)


def _update_edge_reverse(self, context):
    if _IS_SYNCING_UI:
        return
    guide_obj = get_feather_guide_from_context(context)
    if guide_obj is None:
        return
    val = bool(self.edge_reverse)
    if guide_obj.mode == 'EDIT':
        bm = bmesh.from_edit_mesh(guide_obj.data)
        layer = bm.edges.layers.bool.get("feather_reverse")
        if layer is None:
            layer = bm.edges.layers.bool.new("feather_reverse")
        _, target_edges = _get_selected_bm_elements(bm, whole_chain=True)
        for e in target_edges:
            e[layer] = val
        bmesh.update_edit_mesh(guide_obj.data, loop_triangles=False, destructive=False)
    else:
        ensure_feather_guide_attributes(guide_obj.data)
        target_e = _get_object_mode_selected_edge_indices(guide_obj, whole_chain=True)
        attr = guide_obj.data.attributes.get("feather_reverse")
        if attr is not None:
            for ei in target_e:
                attr.data[ei].value = val
    if guide_obj.arantools_feather.auto_update:
        regenerate_feather_object(guide_obj)


def _make_edge_int_updater(prop_name, attr_name):
    def _update(self, context):
        if _IS_SYNCING_UI:
            return
        guide_obj = get_feather_guide_from_context(context)
        if guide_obj is None:
            return
        val = int(getattr(self, prop_name))
        if guide_obj.mode == 'EDIT':
            bm = bmesh.from_edit_mesh(guide_obj.data)
            layer = bm.edges.layers.int.get(attr_name)
            if layer is None:
                layer = bm.edges.layers.int.new(attr_name)
            _, target_edges = _get_selected_bm_elements(bm, whole_chain=True)
            for e in target_edges:
                e[layer] = val
            bmesh.update_edit_mesh(guide_obj.data, loop_triangles=False, destructive=False)
        else:
            ensure_feather_guide_attributes(guide_obj.data)
            target_e = _get_object_mode_selected_edge_indices(guide_obj, whole_chain=True)
            attr = guide_obj.data.attributes.get(attr_name)
            if attr is not None:
                for ei in target_e:
                    attr.data[ei].value = val
        if guide_obj.arantools_feather.auto_update:
            regenerate_feather_object(guide_obj)
    return _update


def _resolve_owner_guide_obj(self, context):
    owner = getattr(self, "id_data", None)
    if isinstance(owner, bpy.types.Object):
        fprops = getattr(owner, "arantools_feather", None)
        if fprops is not None:
            if fprops.is_feather_guide:
                return owner
            if fprops.is_feather_output and fprops.guide_object is not None:
                return fprops.guide_object
    return get_feather_guide_from_context(context)


def _update_feather_obj_settings(self, context):
    if _IS_GENERATING:
        return
    guide_obj = _resolve_owner_guide_obj(self, context)
    if guide_obj is not None:
        regenerate_feather_object(guide_obj, force_full_rebuild=True)


def _update_feather_view_mode(self, context):
    if _IS_GENERATING:
        return
    guide_obj = _resolve_owner_guide_obj(self, context)
    if guide_obj is not None:
        _sync_viewport_shading_for_view_mode(guide_obj.arantools_feather.view_mode)
        regenerate_feather_object(guide_obj, force_full_rebuild=True)


def _on_active_slot_index_changed(self, context):
    """When clicking a row in the Feather Cards UIList in Feather Mode, assign it as ONLY mesh."""
    if _IS_GENERATING or _IS_SYNCING_UI:
        return
    guide_obj = _resolve_owner_guide_obj(self, context)
    if guide_obj is None:
        return
    fprops = guide_obj.arantools_feather
    if not is_in_feather_mode(guide_obj):
        return
    idx = int(fprops.active_slot_index)
    if 0 <= idx < len(fprops.mesh_slots):
        mobj = fprops.mesh_slots[idx].mesh_obj
        if mobj is not None:
            try:
                bpy.ops.arantools.feather_assign_slot(
                    slot_index=idx,
                    mesh_name=mobj.name,
                    extend_random=False,
                )
            except Exception:
                pass


# ============================================================================
# Property Groups
# ============================================================================

def _tag_view3d_redraw(self, context):
    for area in context.screen.areas if context.screen else []:
        if area.type == 'VIEW_3D':
            area.tag_redraw()


def _update_feather_look(self, context):
    # Arrow geometry is baked into cached GPU batches; drop them so size changes show up.
    for obj_cache in _RUNTIME_CACHE.values():
        obj_cache["gpu_batches"] = None
    wm = getattr(context, "window_manager", None)
    for win in wm.windows if wm else []:
        for area in win.screen.areas:
            if area.type == 'VIEW_3D':
                area.tag_redraw()


def _look_color(name, default, description):
    return FloatVectorProperty(
        name=name,
        description=description,
        subtype='COLOR_GAMMA',
        size=4,
        min=0.0,
        max=1.0,
        default=default,
        update=_update_feather_look,
    )


class ARANTOOLS_PG_FeatherLook(PropertyGroup):
    """Feather Mode overlay look (Edit > Preferences > Add-ons > Aran Tools > Feather Mode)."""

    picked_color: _look_color(
        "Picked", (0.72, 0.58, 1.0, 0.85),
        "Fill color of feathers picked with Shift+Click in Feather Select (not orange: that means 'selected')",
    )
    picked_fill: FloatProperty(
        name="Picked Fill", description="Opacity of the picked feathers' card fill",
        default=0.12, min=0.0, max=1.0, subtype='FACTOR', update=_update_feather_look,
    )
    hover_color: _look_color(
        "Hover", (0.55, 0.92, 1.0, 0.95), "Spline and outline color of the feather under the mouse",
    )
    hover_fill: FloatProperty(
        name="Hover Fill", description="Opacity of the hovered feather's card fill",
        default=0.12, min=0.0, max=1.0, subtype='FACTOR', update=_update_feather_look,
    )
    unpicked_fill_color: _look_color(
        "Unpicked Fill", (1.0, 0.25, 0.25, 0.05),
        "Very faint fill on every feather that isn't picked, while in Feather Select (alpha 0 = off)",
    )
    arrow_color: _look_color(
        "Arrows", (0.40, 0.90, 0.58, 0.75), "Color of the Root -> Tip direction chevrons",
    )
    root_color: _look_color("Root", (0.35, 0.65, 1.0, 0.9), "Color of the dot at each feather's root")
    tip_color: _look_color("Tip", (1.0, 0.50, 0.35, 0.9), "Color of the dot at each feather's tip")

    spline_width: FloatProperty(
        name="Spline Width", description="Line width (px) of highlighted feather splines",
        default=1.5, min=0.5, max=8.0, update=_update_feather_look,
    )
    outline_width: FloatProperty(
        name="Card Outline Width", description="Line width (px) of highlighted card outlines",
        default=1.0, min=0.0, max=8.0, update=_update_feather_look,
    )
    arrow_width: FloatProperty(
        name="Arrow Width", description="Line width (px) of the direction chevrons",
        default=2.76, min=0.5, max=8.0, update=_update_feather_look,
    )
    arrow_size: FloatProperty(
        name="Arrow Size", description="Chevron size relative to each spline segment",
        default=0.13, min=0.01, max=0.5, update=_update_feather_look,
    )
    point_size: FloatProperty(
        name="Point Size", description="Size (px) of spline points and root / tip dots",
        default=4.0, min=0.0, max=16.0, update=_update_feather_look,
    )
    behind_opacity: FloatProperty(
        name="Behind Opacity",
        description=(
            "Opacity of Feather Mode overlay lines where they are hidden behind geometry "
            "(0 = hidden, 1 = always fully visible)"
        ),
        default=0.25, min=0.0, max=1.0, subtype='FACTOR', update=_update_feather_look,
    )


def get_feather_look(context=None):
    """The Feather Mode look settings from the add-on preferences (property defaults as fallback)."""
    context = context or bpy.context
    addon = context.preferences.addons.get(__package__) if context.preferences else None
    prefs = getattr(addon, "preferences", None)
    look = getattr(prefs, "feather_look", None)
    if look is not None:
        return look
    # Prefs unavailable (e.g. mid-reload): fall back to the declared defaults
    return type("FeatherLookDefaults", (), {
        name: ARANTOOLS_PG_FeatherLook.__annotations__[name].keywords["default"]
        for name in ARANTOOLS_PG_FeatherLook.__annotations__
    })()


def draw_feather_look_prefs(layout, look):
    """Feather Mode section of the add-on preferences."""
    box = layout.box()
    col = box.column()
    head = col.row()
    head.label(text="Feather Mode", icon='STRANDS')
    head.operator("arantools.feather_look_reset", text="Reset", icon='LOOP_BACK')

    split = col.split(factor=0.5)
    left = split.column(align=True)
    left.label(text="Colors")
    left.prop(look, "picked_color")
    left.prop(look, "picked_fill", slider=True)
    left.separator()
    left.prop(look, "hover_color")
    left.prop(look, "hover_fill", slider=True)
    left.separator()
    left.prop(look, "unpicked_fill_color")
    left.separator()
    left.prop(look, "arrow_color")
    left.prop(look, "root_color")
    left.prop(look, "tip_color")

    right = split.column(align=True)
    right.label(text="Sizes")
    right.prop(look, "spline_width")
    right.prop(look, "outline_width")
    right.prop(look, "arrow_width")
    right.prop(look, "arrow_size")
    right.prop(look, "point_size")
    right.separator()
    right.label(text="Occlusion")
    right.prop(look, "behind_opacity", slider=True)


class ARANTOOLS_OT_feather_look_reset(Operator):
    bl_idname = "arantools.feather_look_reset"
    bl_label = "Reset Feather Mode Look"
    bl_description = "Reset all Feather Mode colors and sizes to their defaults"
    bl_options = {'INTERNAL'}

    def execute(self, context):
        look = get_feather_look(context)
        if not isinstance(look, ARANTOOLS_PG_FeatherLook):
            return {'CANCELLED'}
        for name in ARANTOOLS_PG_FeatherLook.__annotations__:
            look.property_unset(name)
        _update_feather_look(look, context)
        return {'FINISHED'}


def _poll_mesh_object(self, obj):
    return (
        obj is not None
        and obj.type == 'MESH'
        and not getattr(obj.arantools_feather, "is_feather_guide", False)
        and not getattr(obj.arantools_feather, "is_feather_output", False)
    )


class ARANTOOLS_PG_FeatherMeshSlot(PropertyGroup):
    mesh_obj: PointerProperty(
        name="Feather Mesh",
        type=bpy.types.Object,
        poll=_poll_mesh_object,
        update=_update_feather_obj_settings,
    )


class ARANTOOLS_PG_FeatherObject(PropertyGroup):
    is_feather_guide: BoolProperty(
        name="Is Feather Guide",
        default=False,
    )
    is_feather_output: BoolProperty(
        name="Is Generated Feather Mesh",
        default=False,
    )
    guide_object: PointerProperty(
        name="Feather Guide Object",
        type=bpy.types.Object,
    )
    output_object: PointerProperty(
        name="Generated Feather Mesh",
        type=bpy.types.Object,
    )
    mesh_slots: CollectionProperty(
        name="Feather Mesh Slots",
        type=ARANTOOLS_PG_FeatherMeshSlot,
    )
    active_slot_index: IntProperty(
        name="Active Feather Mesh",
        default=0,
        update=_on_active_slot_index_changed,
    )
    admitted_collection: PointerProperty(
        name="Feather Cards Collection",
        description="Collection holding the feather card meshes this Feather Object can use",
        type=bpy.types.Collection,
        update=_update_admitted_collection,
    )
    random_seed: IntProperty(
        name="Random Seed",
        description="Global random seed used when a feather has multiple card meshes checked",
        default=1,
        min=0,
        max=99999,
        update=_update_feather_obj_settings,
    )
    material: PointerProperty(
        name="Feather Material",
        type=bpy.types.Material,
        update=_update_feather_obj_settings,
    )
    view_mode: EnumProperty(
        name="Feather View Mode",
        description="Switch between the original Feather Material, UV Preview, Feather IDs, and X-Ray modes",
        items=[
            ('MATERIAL', "Material", "Display the feather's original material"),
            ('UV_CHECKER', "UV Grid", "Display a 1024x1024 UV Color Grid texture on the feathers"),
            ('UV_GRADIENT', "UV Coords", "Display UV coordinates (Red = U, Green = V) on the feathers"),
            ('FEATHER_ID', "Feather IDs", "Display each individual feather card with a distinct color"),
            ('XRAY', "X-Ray", "Display ghost/wireframe feather cards so internal spline vertices are easy to see"),
        ],
        default='MATERIAL',
        update=_update_feather_view_mode,
    )
    deform_axis: EnumProperty(
        name="Deform Axis",
        items=[
            ('0', "X", "Deform along X axis"),
            ('1', "Y", "Deform along Y axis"),
            ('2', "Z", "Deform along Z axis"),
        ],
        default='1',
        update=_update_feather_obj_settings,
    )
    invert_curve_profile: BoolProperty(
        name="Inverted Profile Direction",
        description="Whether the base meshes & spline smoothing profile are inverted (Root -> Tip)",
        default=False,
        update=_update_feather_obj_settings,
    )
    use_mirror: BoolProperty(
        name="Mirror Feathers",
        description=(
            "Generate a mirrored copy of every feather (with Mirror enabled) across the Mirror "
            "Object's X plane. Built into the generated mesh; the guide stays one-sided"
        ),
        default=True,
        update=_update_feather_obj_settings,
    )
    random_flip: FloatProperty(
        name="Random Flip",
        description=(
            "Chance for each feather card (original or mirror copy, decided separately) to be "
            "reflected across its own spine for variation"
        ),
        default=0.0,
        min=0.0,
        max=1.0,
        subtype='FACTOR',
        update=_update_feather_obj_settings,
    )
    mirror_object: PointerProperty(
        name="Mirror Object",
        type=bpy.types.Object,
        update=_update_feather_obj_settings,
    )
    surface_object: PointerProperty(
        name="Surface / Projection Mesh",
        description="Target body mesh used by Follow Surface, Tilt to Surface, Push Out, and Push Tip",
        type=bpy.types.Object,
        poll=_poll_mesh_object,
        update=_update_feather_obj_settings,
    )
    blur_source_mesh: IntProperty(
        name="Blur Surface Mesh",
        description="Number of smoothing iterations on the Surface / Projection Mesh before sampling",
        default=0,
        min=0,
        max=20,
        update=_update_feather_obj_settings,
    )
    auto_update: BoolProperty(
        name="Live Update in Edit Mode",
        description="Automatically regenerate 3D feathers as you move vertices or edit attributes",
        default=True,
    )
    show_direction_overlay: BoolProperty(
        name="Show Direction Arrows",
        description="Draw Root-to-Tip direction arrows along feather splines in the 3D Viewport",
        default=True,
    )


class ARANTOOLS_PG_FeatherSceneUI(PropertyGroup):
    panel_tab: EnumProperty(
        name="Panel Tab",
        description="Switch between active mode Controls and Advanced settings",
        items=[
            ('CONTROLS', "Controls", "Primary controls for the current Feather Mode"),
            ('ADVANCED', "Advanced", "Surface projection, baking, mirror, and scene setup settings"),
        ],
        default='CONTROLS',
    )
    apply_whole_chain: BoolProperty(
        name="Apply to Whole Feather",
        description=(
            "Vertex sliders (radius, twist, smooth, ...) and quick adjustments change EVERY point of the "
            "selected feather(s) instead of only the selected points"
        ),
        default=False,
    )
    show_extra_attrs: BoolProperty(
        name="Extra Vertex Attributes",
        default=True,
    )
    tweak_push_out: FloatProperty(
        name="Push Out",
        description=(
            "Drag: the selected points move along the Direction (Normal / View) while you drag. "
            "Release keeps the move and the slider springs back to 0. Esc cancels"
        ),
        default=0.0,
        soft_min=-0.05,
        soft_max=0.05,
        subtype='DISTANCE',
        precision=4,
        options=set(),
        update=lambda self, context: _on_push_out(self, context),
    )
    tweak_direction: EnumProperty(
        name="Tweak Direction",
        description="Direction the Tweaking drags move the selected points",
        items=[
            ('NORMAL', "Normal", "Along the feather card's face normal at each point (includes its twist)"),
            ('VIEW', "View", "Toward / away from the current viewport camera"),
        ],
        default='NORMAL',
    )
    feather_return_level: EnumProperty(
        name="Return Level",
        description="Level Feather Mode returns to after clicking a feather in Feather Select",
        items=[
            ('VERTEX', "Vertices", ""),
            ('EDGE', "Splines", ""),
        ],
        default='VERTEX',
    )
    convert_source_collection: PointerProperty(
        name="Source Collection",
        description="Collection holding the Geonodes feather objects to convert into a new Feather Object",
        type=bpy.types.Collection,
    )
    edge_mesh_obj: PointerProperty(
        name="Feather Mesh",
        description="Select the 3D feather card mesh for the selected feather(s)",
        type=bpy.types.Object,
        poll=_poll_mesh_object,
        update=_update_edge_mesh_obj,
    )
    edge_reverse: BoolProperty(
        name="Flip Direction (Tip -> Root)",
        description="Flip the Root-to-Tip direction of the selected feather chain(s)",
        default=False,
        update=_update_edge_reverse,
    )
    edge_subdiv: IntProperty(
        name="Curve Subdivision",
        description="B-spline subdivision level for the selected feather chain(s)",
        default=2,
        min=0,
        max=5,
        update=_make_edge_int_updater("edge_subdiv", "feather_subdiv"),
    )
    edge_smooth_tilt: IntProperty(
        name="Smooth Tilt",
        description="Tilt blur iterations for the selected feather chain(s)",
        default=30,
        min=0,
        max=200,
        update=_make_edge_int_updater("edge_smooth_tilt", "feather_smooth_tilt"),
    )
    edge_smooth_spline: IntProperty(
        name="Smooth Spline",
        description="Spline position blur iterations for the selected feather chain(s)",
        default=100,
        min=0,
        max=300,
        update=_make_edge_int_updater("edge_smooth_spline", "feather_smooth_spline"),
    )
    vert_radius: FloatProperty(
        name="Radius",
        description="Feather width/scale at the selected vertex(es)",
        default=1.0,
        min=0.0,
        soft_min=0.0,
        soft_max=20.0,
        step=5,
        precision=3,
        update=_make_vert_attr_updater("radius"),
    )
    vert_tilt: FloatProperty(
        name="Twist",
        description="Feather twist at the selected point(s)",
        default=0.0,
        soft_min=-math.pi,
        soft_max=math.pi,
        subtype='ANGLE',
        precision=1,
        update=_make_vert_attr_updater("tilt"),
    )
    vert_smooth: FloatProperty(
        name="Smooth",
        description="Extra per-vertex spline smoothing factor",
        default=0.0,
        min=0.0,
        max=1.0,
        subtype='FACTOR',
        update=_make_vert_attr_updater("smooth"),
    )
    vert_pushout: FloatProperty(
        name="Push Out",
        default=0.0,
        soft_min=-2.0,
        soft_max=2.0,
        step=5,
        precision=3,
        update=_make_vert_attr_updater("pushout"),
    )
    vert_pushtip: FloatProperty(
        name="Push Tip",
        default=0.0,
        soft_min=-2.0,
        soft_max=2.0,
        step=5,
        precision=3,
        update=_make_vert_attr_updater("pushtip"),
    )
    vert_followsurface: FloatProperty(
        name="Follow Surface",
        default=0.0,
        min=0.0,
        max=1.0,
        subtype='FACTOR',
        update=_make_vert_attr_updater("followsurface"),
    )
    vert_tilttosurface: FloatProperty(
        name="Tilt to Surface",
        default=0.0,
        min=0.0,
        max=1.0,
        subtype='FACTOR',
        update=_make_vert_attr_updater("tilttosurface"),
    )


# ============================================================================
# Bake Parameters to Vertices & Reset Logic
# ============================================================================

def _solve_bspline_control_points_from_50pts(target_50, n_ctrl, subdiv_level):
    """Find `n_ctrl` B-spline control points whose subdivided + 50-point resampled curve
    reproduces `target_50` `[(x0,y0,z0), ..., (x49,y49,z49)]` with pinned endpoints.
    """
    pts_50 = np.asarray(target_50, dtype=np.float64)
    if n_ctrl <= 2 or subdiv_level <= 0:
        if n_ctrl == 2:
            return [tuple(pts_50[0].tolist()), tuple(pts_50[-1].tolist())]
        out = []
        for j in range(n_ctrl):
            pos = (j / float(max(1, n_ctrl - 1))) * 49.0
            idx = min(48, int(pos))
            frac = pos - idx
            p = pts_50[idx] * (1.0 - frac) + pts_50[idx + 1] * frac
            out.append(tuple(p.tolist()))
        return out

    # Build the exact B-spline subdivision matrix S: (M x n_ctrl)
    steps = 1 << int(min(subdiv_level, 6))
    inv_steps = 1.0 / float(steps)
    m_rows = (n_ctrl - 1) * steps + 1
    s_mat = np.zeros((m_rows, n_ctrl), dtype=np.float64)

    # Extended control polygon indices:
    # e[0] = 2*c[0] - c[1], e[1..n] = c[0..n-1], e[n+1] = 2*c[n-1] - c[n-2]
    ext_weights = np.zeros((n_ctrl + 2, n_ctrl), dtype=np.float64)
    ext_weights[0, 0] = 2.0
    ext_weights[0, 1] = -1.0
    for j in range(n_ctrl):
        ext_weights[j + 1, j] = 1.0
    ext_weights[n_ctrl + 1, n_ctrl - 1] = 2.0
    ext_weights[n_ctrl + 1, n_ctrl - 2] = -1.0

    row_idx = 0
    for i in range(n_ctrl - 1):
        k_end = steps if i < n_ctrl - 2 else steps + 1
        for s in range(k_end):
            u = s * inv_steps
            om = 1.0 - u
            u2 = u * u
            u3 = u2 * u
            b0 = (om * om * om) / 6.0
            b1 = (3.0 * u3 - 6.0 * u2 + 4.0) / 6.0
            b2 = (-3.0 * u3 + 3.0 * u2 + 3.0 * u + 1.0) / 6.0
            b3 = u3 / 6.0
            s_mat[row_idx] = (
                b0 * ext_weights[i]
                + b1 * ext_weights[i + 1]
                + b2 * ext_weights[i + 2]
                + b3 * ext_weights[i + 3]
            )
            row_idx += 1

    # Resample S_mat from M_rows to 50 rows to form A: (50 x n_ctrl)
    r_mat = np.zeros((50, m_rows), dtype=np.float64)
    for k in range(50):
        pos = (k / 49.0) * float(m_rows - 1)
        idx = min(m_rows - 2, int(pos))
        frac = pos - idx
        r_mat[k, idx] = 1.0 - frac
        r_mat[k, idx + 1] = frac

    a_mat = r_mat @ s_mat

    # Pin endpoints v[0] = target_50[0] and v[n_ctrl-1] = target_50[49]
    v_start = pts_50[0]
    v_end = pts_50[49]
    rhs_curve = pts_50 - np.outer(a_mat[:, 0], v_start) - np.outer(a_mat[:, -1], v_end)

    a_int = a_mat[:, 1:-1]
    n_int = n_ctrl - 2

    # Arc-length sampled points along target_50 as regularizer prior
    p_sampled = np.zeros((n_int, 3), dtype=np.float64)
    for j in range(1, n_ctrl - 1):
        pos = (j / float(n_ctrl - 1)) * 49.0
        idx = min(48, int(pos))
        frac = pos - idx
        p_sampled[j - 1] = pts_50[idx] * (1.0 - frac) + pts_50[idx + 1] * frac

    reg_lambda = 1e-3
    lhs = (a_int.T @ a_int) + reg_lambda * np.eye(n_int, dtype=np.float64)
    rhs = (a_int.T @ rhs_curve) + reg_lambda * p_sampled
    v_int = np.linalg.solve(lhs, rhs)

    out = [tuple(v_start.tolist())]
    for j in range(n_int):
        out.append(tuple(v_int[j].tolist()))
    out.append(tuple(v_end.tolist()))
    return out


def bake_feather_parameters_to_vertices(
    guide_obj,
    only_selected=False,
    bake_spline_smoothing=False,
):
    """Bake `pushout`, `pushtip`, `followsurface`, `tilttosurface`, and `smooth`
    (plus optionally `feather_smooth_spline` and `feather_smooth_tilt`) into the guide
    vertex positions and tilts, then reset all baked parameters to 0.
    """
    if guide_obj is None or guide_obj.type != 'MESH':
        return 0

    fprops = guide_obj.arantools_feather
    invert_profile = bool(fprops.invert_curve_profile)
    verts_co, edges_v, pt_layers, edge_int, edge_bool, edge_up = _extract_guide_data(guide_obj)
    chains = _trace_chains_from_edges(len(verts_co), edges_v)
    if not chains:
        return 0

    # Determine which chains to bake
    if only_selected and guide_obj.mode == 'EDIT':
        bm = bmesh.from_edit_mesh(guide_obj.data)
        bm.verts.ensure_lookup_table()
        bm.edges.ensure_lookup_table()
        sel_v_idx = {v.index for v in bm.verts if v.select and not v.hide}
        sel_e_idx = {e.index for e in bm.edges if e.select and not e.hide}
        target_chain_indices = [
            c_idx
            for c_idx, (v_chain, e_chain) in enumerate(chains)
            if any(vi in sel_v_idx for vi in v_chain) or any(ei in sel_e_idx for ei in e_chain)
        ]
        if not target_chain_indices:
            # Fall back to all unhidden chains if nothing is selected
            vis_v_idx = {v.index for v in bm.verts if not v.hide}
            target_chain_indices = [
                c_idx
                for c_idx, (v_chain, _) in enumerate(chains)
                if any(vi in vis_v_idx for vi in v_chain)
            ]
    else:
        target_chain_indices = list(range(len(chains)))

    if not target_chain_indices:
        return 0

    surf_bvh, _ = _get_surface_bvh_cache(guide_obj)

    new_verts_co = list(verts_co)
    rad_list = list(pt_layers["radius"])
    tilt_list = list(pt_layers["tilt"])
    smooth_list = list(pt_layers["smooth"])
    pushout_list = list(pt_layers["pushout"])
    pushtip_list = list(pt_layers["pushtip"])
    follow_list = list(pt_layers["followsurface"])
    tiltsurf_list = list(pt_layers["tilttosurface"])

    subdiv_list = list(edge_int["feather_subdiv"])
    st_list = list(edge_int["feather_smooth_tilt"])
    ss_list = list(edge_int["feather_smooth_spline"])
    rev_list = list(edge_bool["feather_reverse"])
    new_edge_up = list(edge_up)

    baked_count = 0
    for c_idx in target_chain_indices:
        v_chain, e_chain = chains[c_idx]
        if len(v_chain) < 2 or not e_chain:
            continue

        e0 = e_chain[0]
        is_reversed = any(rev_list[ei] for ei in e_chain)
        ordered_v = list(reversed(v_chain)) if is_reversed else list(v_chain)

        subdiv_level = max(0, min(5, subdiv_list[e0]))
        smooth_tilt_iters = max(0, st_list[e0])
        smooth_spline_iters = max(0, ss_list[e0])
        up_tuple = new_edge_up[e0]

        has_offset_params = any(
            smooth_list[vi] != 0.0
            or pushout_list[vi] != 0.0
            or pushtip_list[vi] != 0.0
            or follow_list[vi] != 0.0
            or tiltsurf_list[vi] != 0.0
            for vi in ordered_v
        )
        has_spline_smooth = bake_spline_smoothing and (smooth_spline_iters > 0 or smooth_tilt_iters > 0)

        if has_offset_params or has_spline_smooth:
            px = [verts_co[vi][0] for vi in ordered_v]
            py = [verts_co[vi][1] for vi in ordered_v]
            pz = [verts_co[vi][2] for vi in ordered_v]
            rad_scaled = [rad_list[vi] * 0.1 for vi in ordered_v]
            tilt_arr = [tilt_list[vi] for vi in ordered_v]
            smooth_arr = [smooth_list[vi] for vi in ordered_v]
            pushout_arr = [pushout_list[vi] for vi in ordered_v]
            pushtip_arr = [pushtip_list[vi] for vi in ordered_v]
            follow_arr = [follow_list[vi] for vi in ordered_v]
            tiltsurf_arr = [tiltsurf_list[vi] for vi in ordered_v]

            n_base = Vector(up_tuple)
            if n_base.length_squared < 1e-12:
                n_base = Vector((0.0, 1.0, 0.0))
            else:
                n_base = n_base.normalized()

            res_pts, c_tan, _, _, c_tilt, n_curve, pre_smooth_pts = _evaluate_chain_50pts(
                px,
                py,
                pz,
                rad_scaled,
                tilt_arr,
                smooth_arr,
                subdiv_level,
                smooth_tilt_iters,
                smooth_spline_iters,
                n_base,
                invert_profile,
                pushout_arr=pushout_arr,
                pushtip_arr=pushtip_arr,
                follow_arr=follow_arr,
                tiltsurf_arr=tiltsurf_arr,
                surf_bvh=surf_bvh,
            )

            if bake_spline_smoothing:
                target_50 = [(p.x, p.y, p.z) for p in res_pts]
            else:
                target_50 = list(zip(pre_smooth_pts[0], pre_smooth_pts[1], pre_smooth_pts[2]))

            solved_ctrl = _solve_bspline_control_points_from_50pts(
                target_50,
                len(ordered_v),
                subdiv_level,
            )
            for k, vi in enumerate(ordered_v):
                new_verts_co[vi] = solved_ctrl[k]

            # Bake `tilttosurface` into `feather_up` + per-vertex `tilt`
            any_ts = any(tiltsurf_list[vi] != 0.0 for vi in ordered_v)
            if any_ts or (bake_spline_smoothing and smooth_tilt_iters > 0):
                if any_ts:
                    avg_nc = Vector((0.0, 0.0, 0.0))
                    for nc in n_curve:
                        avg_nc += nc
                    if avg_nc.length_squared > 1e-10:
                        n_base_new = avg_nc.normalized()
                    else:
                        n_base_new = n_base
                    for ei in e_chain:
                        new_edge_up[ei] = (n_base_new.x, n_base_new.y, n_base_new.z)
                else:
                    n_base_new = n_base

                delta_theta = [0.0] * 50
                if any_ts:
                    prev_ang = 0.0
                    for k in range(50):
                        tk = c_tan[k]
                        p0 = n_base_new - tk * n_base_new.dot(tk)
                        p1 = n_curve[k] - tk * n_curve[k].dot(tk)
                        if p0.length_squared > 1e-10 and p1.length_squared > 1e-10:
                            p0.normalize()
                            p1.normalize()
                            sin_a = p0.cross(p1).dot(tk)
                            cos_a = max(-1.0, min(1.0, p0.dot(p1)))
                            ang = math.atan2(sin_a, cos_a)
                            while ang - prev_ang > math.pi:
                                ang -= 2.0 * math.pi
                            while ang - prev_ang < -math.pi:
                                ang += 2.0 * math.pi
                            prev_ang = ang
                            delta_theta[k] = ang
                        else:
                            delta_theta[k] = prev_ang

                n_v = len(ordered_v)
                for m, vi in enumerate(ordered_v):
                    pos = (m / float(max(1, n_v - 1))) * 49.0
                    idx = min(48, int(pos))
                    frac = pos - idx
                    dt_val = delta_theta[idx] * (1.0 - frac) + delta_theta[idx + 1] * frac
                    if bake_spline_smoothing:
                        ct_val = c_tilt[idx] * (1.0 - frac) + c_tilt[idx + 1] * frac
                        tilt_list[vi] = ct_val + dt_val
                    else:
                        tilt_list[vi] = tilt_list[vi] + dt_val

        # Reset all baked vertex & edge parameters to 0
        for vi in ordered_v:
            smooth_list[vi] = 0.0
            pushout_list[vi] = 0.0
            pushtip_list[vi] = 0.0
            follow_list[vi] = 0.0
            tiltsurf_list[vi] = 0.0

        if bake_spline_smoothing:
            for ei in e_chain:
                ss_list[ei] = 0
                st_list[ei] = 0

        baked_count += 1

    # Write updated vertex positions and zeroed attributes back to `guide_obj`
    if guide_obj.mode == 'EDIT':
        bm = bmesh.from_edit_mesh(guide_obj.data)
        bm.verts.ensure_lookup_table()
        bm.edges.ensure_lookup_table()

        for vi, v in enumerate(bm.verts):
            co = new_verts_co[vi]
            v.co.x = co[0]
            v.co.y = co[1]
            v.co.z = co[2]

        updated_pt = {
            "tilt": tilt_list,
            "smooth": smooth_list,
            "pushout": pushout_list,
            "pushtip": pushtip_list,
            "followsurface": follow_list,
            "tilttosurface": tiltsurf_list,
        }
        for attr_name, arr in updated_pt.items():
            layer = bm.verts.layers.float.get(attr_name) or bm.verts.layers.float.new(attr_name)
            for vi, v in enumerate(bm.verts):
                v[layer] = arr[vi]

        up_layer = bm.edges.layers.float_vector.get("feather_up") or bm.edges.layers.float_vector.new("feather_up")
        for ei, e in enumerate(bm.edges):
            e[up_layer] = new_edge_up[ei]

        if bake_spline_smoothing:
            ss_layer = bm.edges.layers.int.get("feather_smooth_spline") or bm.edges.layers.int.new("feather_smooth_spline")
            st_layer = bm.edges.layers.int.get("feather_smooth_tilt") or bm.edges.layers.int.new("feather_smooth_tilt")
            for ei, e in enumerate(bm.edges):
                e[ss_layer] = ss_list[ei]
                e[st_layer] = st_list[ei]

        bmesh.update_edit_mesh(guide_obj.data, loop_triangles=False, destructive=False)
    else:
        mesh = guide_obj.data
        ensure_feather_guide_attributes(mesh)
        flat_co = [c for co in new_verts_co for c in co]
        mesh.vertices.foreach_set("co", flat_co)
        mesh.attributes["tilt"].data.foreach_set("value", tilt_list)
        mesh.attributes["smooth"].data.foreach_set("value", smooth_list)
        mesh.attributes["pushout"].data.foreach_set("value", pushout_list)
        mesh.attributes["pushtip"].data.foreach_set("value", pushtip_list)
        mesh.attributes["followsurface"].data.foreach_set("value", follow_list)
        mesh.attributes["tilttosurface"].data.foreach_set("value", tiltsurf_list)
        flat_up = [c for u in new_edge_up for c in u]
        mesh.attributes["feather_up"].data.foreach_set("vector", flat_up)
        if bake_spline_smoothing:
            mesh.attributes["feather_smooth_spline"].data.foreach_set("value", ss_list)
            mesh.attributes["feather_smooth_tilt"].data.foreach_set("value", st_list)
        mesh.update()

    invalidate_feather_cache(guide_obj)
    regenerate_feather_object(guide_obj, force_full_rebuild=True)
    return baked_count


# ============================================================================
# Conversion Logic (Geonodes feather collection -> new Feather Object)
# ============================================================================

def convert_scene_feathers_to_aranfeathers(
    src_coll,
    hide_source_collection=True,
    bake_parameters_on_convert=False,
):
    """Convert the Geonodes feather objects in `src_coll` into a NEW pure-Python Feather Object
    inside a new `<src>_AranFeathers` collection, leaving `src_coll` and every existing Feather
    Object in the file untouched.
    """
    if src_coll is None:
        raise RuntimeError("No source collection given.")

    src_objects = sorted(
        [
            o for o in src_coll.objects
            if o.type == 'MESH'
            and not o.arantools_feather.is_feather_guide
            and not o.arantools_feather.is_feather_output
        ],
        key=lambda o: o.name,
    )
    if not src_objects:
        raise RuntimeError(f"No mesh objects found in '{src_coll.name}'.")

    unified_object_name = f"{src_coll.name}_AranFeathers"
    target_coll = bpy.data.collections.new(unified_object_name)
    bpy.context.scene.collection.children.link(target_coll)

    guide_mesh = bpy.data.meshes.new(f"{unified_object_name}_GuideMesh")
    guide_obj = bpy.data.objects.new(unified_object_name, guide_mesh)
    target_coll.objects.link(guide_obj)
    guide_obj.show_in_front = True

    # Detect original material and Projection Object (`Socket_10`) / BlurSourceMesh (`Socket_19`)
    detected_mat = None
    detected_surf_obj = None
    detected_blur_src = 0
    for s_obj in src_objects:
        gn_m = s_obj.modifiers.get("GeometryNodes")
        if gn_m is not None:
            if detected_mat is None and gn_m.get("Socket_17") is not None:
                detected_mat = gn_m.get("Socket_17")
            if detected_surf_obj is None and gn_m.get("Socket_10") is not None:
                detected_surf_obj = gn_m.get("Socket_10")
                detected_blur_src = int(gn_m.get("Socket_19", 0))
        if detected_mat is None and s_obj.active_material is not None:
            detected_mat = s_obj.active_material
    fprops = guide_obj.arantools_feather
    fprops.is_feather_guide = True
    fprops.deform_axis = '1'
    fprops.invert_curve_profile = False
    fprops.use_mirror = True
    fprops.surface_object = detected_surf_obj
    fprops.blur_source_mesh = detected_blur_src
    fprops.material = detected_mat
    fprops.view_mode = 'MATERIAL'

    all_verts = []
    all_edges = []
    pt_data = {name: [] for name, _ in POINT_FLOAT_ATTRS}
    edge_slot_data = []
    edge_rev_data = []
    edge_subdiv_data = []
    edge_st_data = []
    edge_ss_data = []
    edge_up_data = []
    edge_mirror_data = []

    v_offset = 0

    for src_obj in src_objects:
        m = src_obj.data
        mw = src_obj.matrix_world
        rot_q = mw.to_quaternion()
        scale_s = mw.to_scale().x

        gn_mod = src_obj.modifiers.get("GeometryNodes")
        if gn_mod is not None:
            mesh1_obj = gn_mod.get("Socket_2")
            mesh2_obj = gn_mod.get("Socket_16")
            curve_subdiv = int(gn_mod.get("Socket_4", 2))
            base_radius = float(gn_mod.get("Socket_5", 0.0))
            radius_mult = float(gn_mod.get("Socket_9", 1.0))
            base_tilt = float(gn_mod.get("Socket_6", 0.0))
            base_normal_vec = Vector(gn_mod.get("Socket_11", (0.0, 1.0, 0.0)))
            smooth_tilt_iters = int(gn_mod.get("Socket_18", 30))
            smooth_spline_iters = int(gn_mod.get("Socket_20", 100))
        else:
            mesh1_obj = None
            mesh2_obj = None
            curve_subdiv = 2
            base_radius = 0.0
            radius_mult = 1.0
            base_tilt = 0.0
            base_normal_vec = Vector((0.0, 1.0, 0.0))
            smooth_tilt_iters = 30
            smooth_spline_iters = 100

        world_up = (rot_q @ base_normal_vec).normalized()
        chains = extract_feather_chains(m)

        src_rad = m.attributes.get("radius")
        src_tilt = m.attributes.get("tilt")
        src_smooth = m.attributes.get("smooth")
        src_pushout = m.attributes.get("pushout")
        src_pushtip = m.attributes.get("pushtip")
        src_follow = m.attributes.get("followsurface")
        src_tiltsurf = m.attributes.get("tilttosurface")
        src_rev = m.attributes.get("reverse")
        src_alt = m.attributes.get("alt")
        src_mir = m.attributes.get("mirror")

        non_alt_chains = []
        alt_chains = []
        for v_chain, e_chain in chains:
            uses_alt = any(bool(src_alt.data[vi].value) for vi in v_chain) if src_alt else False
            if uses_alt:
                alt_chains.append((v_chain, e_chain, True))
            else:
                non_alt_chains.append((v_chain, e_chain, False))

        for v_chain, e_chain, uses_alt in (non_alt_chains + alt_chains):
            is_rev = any(bool(src_rev.data[vi].value) for vi in v_chain) if src_rev else False
            ordered_v = list(reversed(v_chain)) if is_rev else list(v_chain)

            chosen_mesh = mesh2_obj if uses_alt else mesh1_obj
            slot_idx = find_or_add_mesh_slot(guide_obj, chosen_mesh)
            is_mir = any(bool(src_mir.data[vi].value) for vi in ordered_v) if src_mir else True

            n_c = len(ordered_v)
            for vi in ordered_v:
                w_co = mw @ m.vertices[vi].co
                all_verts.append((w_co.x, w_co.y, w_co.z))

                r_raw = float(src_rad.data[vi].value) if src_rad else 1.0
                t_raw = float(src_tilt.data[vi].value) if src_tilt else 0.0
                pt_data["radius"].append((base_radius + r_raw) * radius_mult * scale_s)
                pt_data["tilt"].append(2.0 * t_raw + base_tilt)
                pt_data["smooth"].append(float(src_smooth.data[vi].value) if src_smooth else 0.0)
                pt_data["pushout"].append((float(src_pushout.data[vi].value) * scale_s) if src_pushout else 0.0)
                pt_data["pushtip"].append((float(src_pushtip.data[vi].value) * scale_s) if src_pushtip else 0.0)
                pt_data["followsurface"].append(float(src_follow.data[vi].value) if src_follow else 0.0)
                pt_data["tilttosurface"].append(float(src_tiltsurf.data[vi].value) if src_tiltsurf else 0.0)

            for k in range(n_c - 1):
                all_edges.append((v_offset + k, v_offset + k + 1))
                edge_slot_data.append(slot_idx)
                edge_rev_data.append(False)
                edge_subdiv_data.append(curve_subdiv)
                edge_st_data.append(smooth_tilt_iters)
                edge_ss_data.append(smooth_spline_iters)
                edge_up_data.extend((world_up.x, world_up.y, world_up.z))
                edge_mirror_data.append(is_mir)

            v_offset += n_c

    guide_mesh.from_pydata(all_verts, all_edges, [])
    guide_mesh.update()
    ensure_feather_guide_attributes(guide_mesh)

    for name, _ in POINT_FLOAT_ATTRS:
        guide_mesh.attributes[name].data.foreach_set("value", pt_data[name])

    edge_mask_data = [1 << max(0, min(29, s)) for s in edge_slot_data]
    edge_seed_data = [0] * len(edge_slot_data)
    guide_mesh.attributes["feather_mesh_slot"].data.foreach_set("value", edge_slot_data)
    guide_mesh.attributes["feather_mesh_mask"].data.foreach_set("value", edge_mask_data)
    guide_mesh.attributes["feather_seed"].data.foreach_set("value", edge_seed_data)
    guide_mesh.attributes["feather_reverse"].data.foreach_set("value", edge_rev_data)
    guide_mesh.attributes["feather_subdiv"].data.foreach_set("value", edge_subdiv_data)
    guide_mesh.attributes["feather_smooth_tilt"].data.foreach_set("value", edge_st_data)
    guide_mesh.attributes["feather_smooth_spline"].data.foreach_set("value", edge_ss_data)
    guide_mesh.attributes["feather_up"].data.foreach_set("vector", edge_up_data)
    guide_mesh.attributes["feather_mirror"].data.foreach_set("value", edge_mirror_data)

    if bake_parameters_on_convert:
        bake_feather_parameters_to_vertices(
            guide_obj,
            only_selected=False,
            bake_spline_smoothing=False,
        )

    out_obj = regenerate_feather_object(guide_obj, force_full_rebuild=True)

    if hide_source_collection:
        def _find_layer_coll(layer_coll, coll):
            if layer_coll.collection == coll:
                return layer_coll
            for child in layer_coll.children:
                found = _find_layer_coll(child, coll)
                if found is not None:
                    return found
            return None

        lc = _find_layer_coll(bpy.context.view_layer.layer_collection, src_coll)
        if lc is not None:
            lc.hide_viewport = True

    return guide_obj, out_obj


# ============================================================================
# Operators
# ============================================================================

class ARANTOOLS_OT_feather_convert_scene(Operator):
    bl_idname = "arantools.feather_convert_scene"
    bl_label = "Convert Feather Collection"
    bl_description = (
        "Convert the Geonodes feathers in a chosen collection into a NEW Feather Object. "
        "The source collection and any existing Feather Objects are left untouched"
    )
    bl_options = {'REGISTER', 'UNDO'}

    source_collection: StringProperty(
        name="Source Collection",
        description="Collection holding the Geonodes feather objects to convert",
    )
    hide_original: BoolProperty(
        name="Hide Source Collection",
        default=True,
    )
    bake_parameters: BoolProperty(
        name="Bake Push/Surface Params into Vertices",
        description="Bake any non-zero pushout, pushtip, followsurface, tilttosurface, and smooth into the vertices and reset them to 0",
        default=False,
    )

    def invoke(self, context, event):
        # Collection already picked in the panel -> run straight away; otherwise ask for one.
        if self.source_collection in bpy.data.collections:
            return self.execute(context)
        return context.window_manager.invoke_props_dialog(self)

    def draw(self, context):
        col = self.layout.column()
        col.prop_search(self, "source_collection", bpy.data, "collections")
        col.prop(self, "hide_original")
        col.prop(self, "bake_parameters")

    def execute(self, context):
        src_coll = bpy.data.collections.get(self.source_collection)
        if src_coll is None:
            self.report({'ERROR'}, "Pick a source collection to convert.")
            return {'CANCELLED'}
        try:
            guide_obj, out_obj = convert_scene_feathers_to_aranfeathers(
                src_coll,
                hide_source_collection=self.hide_original,
                bake_parameters_on_convert=self.bake_parameters,
            )
        except Exception as exc:
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}

        if context.mode != 'OBJECT':
            bpy.ops.object.mode_set(mode='OBJECT')
        bpy.ops.object.select_all(action='DESELECT')
        guide_obj.select_set(True)
        context.view_layer.objects.active = guide_obj

        n_polys = len(out_obj.data.polygons) if out_obj and out_obj.data else 0
        self.report(
            {'INFO'},
            f"Created '{guide_obj.name}' + '{out_obj.name}' ({n_polys} base polys) from '{src_coll.name}'.",
        )
        return {'FINISHED'}


class ARANTOOLS_OT_feather_bake_parameters(Operator):
    bl_idname = "arantools.feather_bake_parameters"
    bl_label = "Bake Parameters to Vertices & Reset"
    bl_description = (
        "Bake the deformation achieved through Push Out, Push Tip, Follow Surface, "
        "Tilt to Surface, and Smooth into the guide vertices, then reset those parameters to 0"
    )
    bl_options = {'REGISTER', 'UNDO'}

    only_selected: BoolProperty(
        name="Selected Feathers Only (Edit Mode)",
        description="In Edit Mode, only bake the selected/isolated feather(s); otherwise bake all feathers",
        default=False,
    )
    bake_spline_smoothing: BoolProperty(
        name="Also Bake Smooth Spline & Smooth Tilt",
        description="Also bake per-edge Smooth Spline and Smooth Tilt into the vertices and reset them to 0",
        default=False,
    )

    def execute(self, context):
        guide_obj = get_feather_guide_from_context(context)
        if guide_obj is None:
            self.report({'WARNING'}, "Select a Feather Object first.")
            return {'CANCELLED'}

        n_baked = bake_feather_parameters_to_vertices(
            guide_obj,
            only_selected=self.only_selected,
            bake_spline_smoothing=self.bake_spline_smoothing,
        )
        if guide_obj.mode == 'EDIT':
            sync_ui_from_selection(context)
        else:
            ui = getattr(context.scene, "arantools_feather_ui", None)
            if ui is not None:
                global _IS_SYNCING_UI
                _IS_SYNCING_UI = True
                try:
                    ui.vert_smooth = 0.0
                    ui.vert_pushout = 0.0
                    ui.vert_pushtip = 0.0
                    ui.vert_followsurface = 0.0
                    ui.vert_tilttosurface = 0.0
                    if self.bake_spline_smoothing:
                        ui.edge_smooth_spline = 0
                        ui.edge_smooth_tilt = 0
                finally:
                    _IS_SYNCING_UI = False

        for area in context.screen.areas if context.screen else []:
            area.tag_redraw()

        mode_str = "all parameters (including Smooth Spline/Tilt)" if self.bake_spline_smoothing else "push, surface & smooth parameters"
        self.report({'INFO'}, f"Baked {mode_str} into vertices and reset to 0 on {n_baked} feather(s).")
        return {'FINISHED'}


_VIEW3D_HEADER_DRAW_KEY = "_arantools_feather_view3d_header"


# ============================================================================
# Feather Mode (= Edit Mode on a Feather Guide)
# ============================================================================
#
# Blender's interaction modes (Object / Edit / Sculpt / ...) are hardcoded in C, so an addon
# cannot add a real new mode. Feather Mode is therefore simply the guide object in Edit Mode,
# with three selection levels:
#   1 = Vertices  -> native vertex select mode
#   2 = Splines   -> native edge select mode
#   3 = Feather Select -> the `Feather Select` toolbar tool: hover highlights a feather; clicking
#                    it exits Feather Select, isolating that feather back in Vertices / Splines
#                    (whichever was active before) to edit it.
# Everything else (Tab, Undo, G/R/S, hide/reveal, box select, ...) is stock Edit Mode.

FEATHER_TOOL_ID = "arantools.feather_select_tool"
_DEFAULT_EDIT_TOOL_ID = "builtin.select_box"

# Hover state for the Feather Select highlight. Only the hover tracker writes it; it never
# consumes events, so if it ever dies the worst case is a missing highlight.
_HOVER = {"guide": "", "chain": -1, "mirrored": False, "last_seen": 0.0}
# Bumped on every hover-tracker start; older instances see a stale generation and exit.
_HOVER_GEN = 0


def is_in_feather_mode(guide_obj):
    """Feather Mode is active whenever the Feather Guide is in Edit Mode."""
    return guide_obj is not None and guide_obj.mode == 'EDIT'


def _get_active_edit_tool_id(context):
    workspace = getattr(context, "workspace", None)
    if workspace is None:
        return ""
    try:
        tool = workspace.tools.from_space_view3d_mode('EDIT_MESH', create=False)
    except Exception:
        return ""
    return tool.idname if tool is not None else ""


def get_feather_level(context):
    """Return the current Feather Mode level: 'FEATHER', 'EDGE' (Splines) or 'VERTEX'."""
    if _get_active_edit_tool_id(context) == FEATHER_TOOL_ID:
        return 'FEATHER'
    sel_mode = context.tool_settings.mesh_select_mode
    if sel_mode[0]:
        return 'VERTEX'
    return 'EDGE'


def _get_guide_chains_fast(guide_obj):
    """Return `chains` `[(v_chain, e_chain), ...]` for `guide_obj` using cache if available."""
    obj_cache = _RUNTIME_CACHE.get(guide_obj.name)
    if obj_cache is not None and obj_cache.get("guide_meta") is not None:
        return obj_cache["guide_meta"]["chains"]
    verts_co, edges_v, _, _, _, _ = _extract_guide_data(guide_obj)
    return _trace_chains_from_edges(len(verts_co), edges_v)


def _set_selected_chain_indices(guide_obj, chain_indices, context=None):
    """Select exactly `chain_indices` (whole feathers) on `guide_obj`. Nothing is ever hidden."""
    if guide_obj is None or guide_obj.data is None:
        return
    chains = _get_guide_chains_fast(guide_obj)
    valid_chains = [ci for ci in sorted(set(chain_indices)) if 0 <= ci < len(chains)]

    keep_v = set()
    keep_e = set()
    for c_idx in valid_chains:
        v_chain, e_chain = chains[c_idx]
        keep_v.update(v_chain)
        keep_e.update(e_chain)

    if guide_obj.mode == 'EDIT':
        bm = bmesh.from_edit_mesh(guide_obj.data)
        bm.verts.ensure_lookup_table()
        bm.edges.ensure_lookup_table()
        for v in bm.verts:
            v.select = v.index in keep_v
        for e in bm.edges:
            e.select = e.index in keep_e

        bm.select_history.clear()
        if valid_chains:
            first_v = chains[valid_chains[-1]][0]
            if first_v and first_v[0] < len(bm.verts):
                bm.select_history.add(bm.verts[first_v[0]])

        bm.select_flush_mode()
        bmesh.update_edit_mesh(guide_obj.data, loop_triangles=False, destructive=False)
    else:
        mesh = guide_obj.data
        for v in mesh.vertices:
            v.select = v.index in keep_v
        for e in mesh.edges:
            e.select = e.index in keep_e

    obj_cache = _RUNTIME_CACHE.get(guide_obj.name)
    if obj_cache is not None:
        obj_cache["gpu_batches"] = None

    if context is not None:
        sync_ui_from_selection(context)


def _get_mirror_reflection_matrix(fprops):
    """Return the 4x4 world-space reflection matrix across `mirror_object` local X plane.

    Without a mirror object the Mirror modifier reflects across the output object's own local X,
    so match that instead of assuming world origin.
    """
    m_obj = fprops.mirror_object or fprops.output_object
    if m_obj is not None:
        mw_m = m_obj.matrix_world
        return mw_m @ Matrix.Scale(-1.0, 4, (1.0, 0.0, 0.0)) @ mw_m.inverted_safe()
    return Matrix.Scale(-1.0, 4, (1.0, 0.0, 0.0))


def _get_cards_pick_bvh(out_obj, obj_cache):
    """BVH over the generated cards WITHOUT modifiers, so hit face indices match `chain_ranges`.

    `Object.ray_cast` hits the evaluated mesh, where the Mirror modifier's bisect/merge renumbers
    faces — that made hover/pick jump to feathers far from the mouse. The mirrored half is handled
    by reflecting the ray instead.
    """
    bvh = obj_cache.get("pick_bvh")
    if bvh is None:
        mesh = out_obj.data
        bvh = BVHTree.FromPolygons(
            [v.co.copy() for v in mesh.vertices],
            [tuple(p.vertices) for p in mesh.polygons],
        )
        obj_cache["pick_bvh"] = bvh
    return bvh


def _pick_feather_chain(region, rv3d, coord, guide_obj):
    """Return `(c_idx, mirrored)` for the feather under region-space `coord` (`c_idx` = -1 if none).

    Raycasts the generated feather cards (`out_obj` + mirrored half), occluded by the surface body,
    with a depth-culled 2D distance-to-spline fallback.
    """
    if region is None or rv3d is None or guide_obj is None:
        return -1, False

    fprops = guide_obj.arantools_feather
    out_obj = fprops.output_object
    obj_cache = _RUNTIME_CACHE.get(guide_obj.name)
    if obj_cache is None or obj_cache.get("chain_ranges") is None:
        regenerate_feather_object(guide_obj)
        obj_cache = _RUNTIME_CACHE.get(guide_obj.name)

    chain_ranges = obj_cache.get("chain_ranges", []) if obj_cache else []

    ray_origin = view3d_utils.region_2d_to_origin_3d(region, rv3d, coord)
    ray_dir = view3d_utils.region_2d_to_vector_3d(region, rv3d, coord).normalized()

    # 0. Depth occlusion against the surface body so feathers behind it are never picked through it.
    max_depth = 1e30
    surf_obj = fprops.surface_object
    if surf_obj is not None and surf_obj.type == 'MESH' and surf_obj.data is not None and len(surf_obj.data.polygons) > 0:
        s_inv = surf_obj.matrix_world.inverted_safe()
        s_orig = s_inv @ ray_origin
        s_dir = (s_inv.to_3x3() @ ray_dir).normalized()
        s_hit, s_loc, _, _ = surf_obj.ray_cast(s_orig, s_dir)
        if s_hit:
            body_dist = (surf_obj.matrix_world @ s_loc - ray_origin).length
            # Small tolerance (0.04m) for feather roots slightly embedded in the body skin
            max_depth = body_dist + 0.04

    # 1. Raycast against the generated feather cards (`out_obj`), plus its mirrored half
    if out_obj is not None and out_obj.data is not None and len(out_obj.data.polygons) > 0 and chain_ranges:
        mw_out = out_obj.matrix_world
        mw_inv = mw_out.inverted_safe()
        cards_bvh = _get_cards_pick_bvh(out_obj, obj_cache)

        loc, _, poly_idx, _ = cards_bvh.ray_cast(mw_inv @ ray_origin, (mw_inv.to_3x3() @ ray_dir).normalized())
        if loc is not None and poly_idx is not None and (mw_out @ loc - ray_origin).length <= max_depth:
            for ranges, mirrored in ((chain_ranges, False), (obj_cache.get("mirror_ranges") or [], True)):
                for c_idx, _, _, p_start, p_end, _, _ in ranges:
                    if p_start <= poly_idx < p_end:
                        return c_idx, mirrored

    # 2. Fallback: depth-culled 2D screen-space distance to guide spline segments (within 22 px)
    if obj_cache and obj_cache.get("overlay_data"):
        verts_co, chains, _ = obj_cache["overlay_data"]
    else:
        verts_co, edges_v, _, _, _, _ = _extract_guide_data(guide_obj)
        chains = _trace_chains_from_edges(len(verts_co), edges_v)

    mw = guide_obj.matrix_world
    refl_mat = _get_mirror_reflection_matrix(fprops) if fprops.use_mirror else None
    m_pt = Vector((float(coord[0]), float(coord[1])))
    best_c_idx = -1
    best_px_dist = 22.0
    best_mirrored = False

    for c_idx, (v_chain, _) in enumerate(chains):
        for is_mir in ((False, True) if refl_mat is not None else (False,)):
            pts_2d = []
            for vi in v_chain:
                w_pt = mw @ Vector(verts_co[vi])
                if is_mir:
                    w_pt = refl_mat @ w_pt
                if (w_pt - ray_origin).length > max_depth + 0.05:
                    continue
                p2 = view3d_utils.location_3d_to_region_2d(region, rv3d, w_pt)
                if p2 is not None:
                    pts_2d.append(Vector((float(p2.x), float(p2.y))))
            for i in range(len(pts_2d) - 1):
                a, b = pts_2d[i], pts_2d[i + 1]
                ab = b - a
                ab_len_sq = ab.length_squared
                if ab_len_sq < 1e-6:
                    d = (m_pt - a).length
                else:
                    t = max(0.0, min(1.0, (m_pt - a).dot(ab) / ab_len_sq))
                    d = (m_pt - (a + ab * t)).length
                if d < best_px_dist:
                    best_px_dist = d
                    best_c_idx = c_idx
                    best_mirrored = is_mir

    return best_c_idx, best_mirrored


def _enter_feather_mode(context, guide_obj):
    """Put `guide_obj` into Edit Mode as the only selected, active object."""
    if guide_obj.mode == 'EDIT':
        return
    if context.mode != 'OBJECT':
        bpy.ops.object.mode_set(mode='OBJECT')
    for o in list(context.selected_objects):
        o.select_set(False)
    guide_obj.hide_set(False)
    guide_obj.select_set(True)
    context.view_layer.objects.active = guide_obj
    bpy.ops.object.mode_set(mode='EDIT')


def _set_chains_hidden(guide_obj, visible_chains):
    """Edit Mode: show only `visible_chains` (all feathers when it is empty or covers everything)."""
    if guide_obj.mode != 'EDIT':
        return
    chains = _get_guide_chains_fast(guide_obj)
    show_v = set()
    show_e = set()
    for c_idx in visible_chains:
        if 0 <= c_idx < len(chains):
            show_v.update(chains[c_idx][0])
            show_e.update(chains[c_idx][1])
    isolate = 0 < len(set(visible_chains)) < len(chains)

    bm = bmesh.from_edit_mesh(guide_obj.data)
    for v in bm.verts:
        v.hide = isolate and v.index not in show_v
        if v.hide:
            v.select = False
    for e in bm.edges:
        e.hide = isolate and e.index not in show_e
        if e.hide:
            e.select = False
    bmesh.update_edit_mesh(guide_obj.data, loop_triangles=False, destructive=False)

    obj_cache = _RUNTIME_CACHE.get(guide_obj.name)
    if obj_cache is not None:
        obj_cache["gpu_batches"] = None


# Feathers picked (Shift+click) while in Feather Select, per guide name. The guide mesh is fully
# hidden while picking so Blender draws no edit-mode wires, and hidden elements can't hold a
# selection — so the picks live here and become the real selection when Feather Select is left.
_PICKED = {}


def get_picked_chains(guide_obj):
    return sorted(_PICKED.get(guide_obj.name, ())) if guide_obj is not None else []


def _tag_statusbar_redraw(context):
    """The status bar (Confirm / Cancel) only redraws when told; clearing status text tags it."""
    workspace = getattr(context, "workspace", None) if context is not None else None
    if workspace is not None and not bpy.app.background:
        try:
            workspace.status_text_set(None)
        except Exception:
            pass


def _set_picked(guide_obj, chain_indices, context=None):
    _PICKED[guide_obj.name] = set(chain_indices)
    screen = getattr(context, "screen", None) if context is not None else None
    for area in screen.areas if screen else []:
        if area.type == 'VIEW_3D':
            area.tag_redraw()
    _tag_statusbar_redraw(context)


def _start_feather_select(guide_obj):
    """Start picking with nothing picked, then hide the whole guide (no edit-mode wires).
    The existing Edit Mode selection deliberately doesn't carry over — only Shift+clicks pick."""
    _PICKED.setdefault(guide_obj.name, set())
    bm = bmesh.from_edit_mesh(guide_obj.data)
    for elems in (bm.verts, bm.edges):
        for el in elems:
            el.select = False
            el.hide = True
    bmesh.update_edit_mesh(guide_obj.data, loop_triangles=False, destructive=False)


def _leave_feather_select(guide_obj, context, select=True):
    """Show just the picked feathers (all when none); optionally make them the selection.

    Vertices passes `select=False`: selecting every point of the feather would make the per-vertex
    sliders (twist, radius, ...) hit the whole feather instead of the points you then click.
    """
    picked = sorted(_PICKED.pop(guide_obj.name, ()))
    _set_chains_hidden(guide_obj, picked)
    _set_selected_chain_indices(guide_obj, picked if select else [], context=context)


def _set_feather_level(context, guide_obj, level):
    """Switch the Feather Mode selection level (entering Edit Mode first if needed).

    Entering Feather Select remembers the level you came from and hides the guide mesh (feathers
    are picked on their cards). Leaving it isolates the picked feather(s) for editing.
    """
    _enter_feather_mode(context, guide_obj)
    ts = context.tool_settings
    prev_level = get_feather_level(context)

    if level == 'FEATHER':
        if prev_level in ('VERTEX', 'EDGE'):
            context.scene.arantools_feather_ui.feather_return_level = prev_level
        ts.mesh_select_mode = (True, False, False)
        if prev_level != 'FEATHER':
            _PICKED.pop(guide_obj.name, None)  # fresh start: nothing picked
        _start_feather_select(guide_obj)
        if _get_active_edit_tool_id(context) != FEATHER_TOOL_ID:
            bpy.ops.wm.tool_set_by_id(name=FEATHER_TOOL_ID, space_type='VIEW_3D')
        if context.window is not None and not bpy.app.background:
            bpy.ops.arantools.feather_hover('INVOKE_DEFAULT')  # no-op if one is already running
    else:
        ts.mesh_select_mode = (level == 'VERTEX', level == 'EDGE', False)
        if _get_active_edit_tool_id(context) == FEATHER_TOOL_ID:
            bpy.ops.wm.tool_set_by_id(name=_DEFAULT_EDIT_TOOL_ID, space_type='VIEW_3D')
        if prev_level == 'FEATHER':
            _leave_feather_select(guide_obj, context, select=(level != 'VERTEX'))
            _tag_statusbar_redraw(context)
        sync_ui_from_selection(context)


def _find_view3d_region_at_mouse(context, event):
    """Return `(region, rv3d, (local_x, local_y))` for the 3D Viewport main region under the cursor."""
    screen = getattr(context, "screen", None)
    if screen is None:
        return None, None, None
    mx, my = event.mouse_x, event.mouse_y
    for area in screen.areas:
        if area.type != 'VIEW_3D':
            continue
        # The N-panel, toolbar and header overlap the main region; never hover-pick through them.
        for reg in area.regions:
            if reg.type in {'UI', 'TOOLS', 'HEADER', 'TOOL_HEADER', 'HUD'}:
                if reg.x <= mx < reg.x + reg.width and reg.y <= my < reg.y + reg.height:
                    return None, None, None
        for reg in area.regions:
            if reg.type == 'WINDOW' and reg.x <= mx < reg.x + reg.width and reg.y <= my < reg.y + reg.height:
                return reg, area.spaces.active.region_3d, (mx - reg.x, my - reg.y)
    return None, None, None


def _set_hover(guide_name, c_idx, mirrored, context):
    if (_HOVER["guide"], _HOVER["chain"], _HOVER["mirrored"]) == (guide_name, c_idx, mirrored):
        return
    _HOVER.update(guide=guide_name, chain=c_idx, mirrored=mirrored)
    for area in context.screen.areas if context.screen else []:
        if area.type == 'VIEW_3D':
            area.tag_redraw()


class ARANTOOLS_OT_feather_hover(Operator):
    """Feather Select hover highlight. Purely passive: it only reads mouse moves and never
    consumes an event; clicks are handled by the `Feather Select` tool keymap."""
    bl_idname = "arantools.feather_hover"
    bl_label = "Feather Hover Highlight"
    bl_options = {'INTERNAL'}

    def invoke(self, context, event):
        global _HOVER_GEN
        # Also bound to MOUSEMOVE in the tool keymap (so picking the tool from the toolbar starts it).
        # A live tracker sees every event before the keymap does, so a fresh `last_seen` means one
        # is already running.
        if time.monotonic() - _HOVER["last_seen"] < 0.5:
            return {'CANCELLED', 'PASS_THROUGH'}
        _HOVER_GEN += 1
        self._gen = _HOVER_GEN
        _HOVER["last_seen"] = time.monotonic()
        guide_obj = get_feather_guide_from_context(context)
        if is_in_feather_mode(guide_obj) and guide_obj.name not in _PICKED:
            _start_feather_select(guide_obj)  # entered via Tab or the toolbar, not our buttons
        context.window_manager.modal_handler_add(self)
        return {'RUNNING_MODAL', 'PASS_THROUGH'}

    def modal(self, context, event):
        if self._gen == _HOVER_GEN:
            _HOVER["last_seen"] = time.monotonic()
        guide_obj = get_feather_guide_from_context(context)
        if (
            self._gen != _HOVER_GEN
            or not is_in_feather_mode(guide_obj)
            or _get_active_edit_tool_id(context) != FEATHER_TOOL_ID
        ):
            if self._gen == _HOVER_GEN:
                _set_hover("", -1, False, context)
                _HOVER["last_seen"] = 0.0
                # Another tool was picked from the toolbar while still editing: un-hide the guide
                if is_in_feather_mode(guide_obj) and guide_obj.name in _PICKED:
                    _leave_feather_select(guide_obj, context)
            return {'CANCELLED', 'PASS_THROUGH'}

        if event.type in {'MOUSEMOVE', 'INBETWEEN_MOUSEMOVE'}:
            region, rv3d, coord = _find_view3d_region_at_mouse(context, event)
            if region is None:
                _set_hover("", -1, False, context)
            else:
                c_idx, mirrored = _pick_feather_chain(region, rv3d, coord, guide_obj)
                _set_hover(guide_obj.name, c_idx, mirrored, context)
        return {'PASS_THROUGH'}


class ARANTOOLS_OT_feather_toggle_edit(Operator):
    bl_idname = "arantools.feather_toggle_edit"
    bl_label = "Feather Select"
    bl_description = (
        "Hover and click a feather to edit just that feather (Ctrl+F, or 3 in Edit Mode). "
        "Clicking this again while picking leaves to Object Mode"
    )
    bl_options = {'REGISTER', 'UNDO'}

    def invoke(self, context, event):
        # Ctrl+F lives in generic keymaps: on anything but a Feather Object, let Blender's own
        # Ctrl+F (Face menu in Edit Mode) run.
        if get_feather_guide_from_context(context) is None:
            return {'PASS_THROUGH'}
        return self.execute(context)

    def execute(self, context):
        guide_obj = get_feather_guide_from_context(context)
        if guide_obj is None:
            self.report({'WARNING'}, "Select a Feather Object first.")
            return {'CANCELLED'}

        out_obj = guide_obj.arantools_feather.output_object
        if out_obj is not None:
            # Older versions locked this while in Feather Mode; keep it clickable.
            out_obj.hide_select = False

        if is_in_feather_mode(guide_obj) and get_feather_level(context) == 'FEATHER':
            # Toggle off while picking -> back to Object Mode
            bpy.ops.object.mode_set(mode='OBJECT')
        else:
            _set_feather_level(context, guide_obj, 'FEATHER')
        for area in context.screen.areas if context.screen else []:
            area.tag_redraw()
        return {'FINISHED'}


class ARANTOOLS_OT_feather_set_level(Operator):
    bl_idname = "arantools.feather_set_level"
    bl_label = "Set Feather Mode Level"
    bl_description = "Switch Feather Mode between Vertices (1), Splines (2) and Feather Select (3)"
    bl_options = {'REGISTER', 'UNDO'}

    level: EnumProperty(
        items=[
            ('VERTEX', "Vertices", "Edit individual feather vertices: radius, tilt & smoothing"),
            ('EDGE', "Splines", "Edit feather guide splines"),
            ('FEATHER', "Feather Select", "Hover feathers in the viewport; clicking one leaves Feather Select and edits just that feather"),
        ],
        default='FEATHER',
    )

    def invoke(self, context, event):
        # Keymap entries live in the generic "Mesh" keymap: stay out of the way on non-feather meshes.
        if get_feather_guide_from_context(context) is None:
            return {'PASS_THROUGH'}
        return self.execute(context)

    def execute(self, context):
        guide_obj = get_feather_guide_from_context(context)
        if guide_obj is None:
            return {'CANCELLED'}
        _set_feather_level(context, guide_obj, self.level)
        for area in context.screen.areas if context.screen else []:
            area.tag_redraw()
        return {'FINISHED'}


class ARANTOOLS_OT_feather_pick(Operator):
    bl_idname = "arantools.feather_pick"
    bl_label = "Pick Feather"
    bl_description = (
        "Click: edit the feather under the mouse (isolates it and returns to Vertices / Splines). "
        "Shift = add/remove from selection, Ctrl = deselect"
    )
    bl_options = {'REGISTER', 'UNDO'}

    mode: EnumProperty(
        items=[
            ('EDIT', "Edit", "Isolate the clicked feather and return to the previous level to edit it"),
            ('TOGGLE', "Toggle", "Add / remove the clicked feather, stay in Feather Select"),
            ('SUB', "Subtract", "Deselect the clicked feather, stay in Feather Select"),
        ],
        default='EDIT',
    )

    @classmethod
    def poll(cls, context):
        return is_in_feather_mode(get_feather_guide_from_context(context))

    def invoke(self, context, event):
        guide_obj = get_feather_guide_from_context(context)
        coord = (event.mouse_region_x, event.mouse_region_y)
        c_idx, _ = _pick_feather_chain(context.region, context.region_data, coord, guide_obj)

        if c_idx < 0:
            # Click on empty space: clear the picks, stay in Feather Select
            if self.mode != 'EDIT':
                return {'CANCELLED'}
            _set_picked(guide_obj, [], context)
            return {'FINISHED'}

        cur = set(get_picked_chains(guide_obj))
        if self.mode == 'TOGGLE':
            _set_picked(guide_obj, cur ^ {c_idx}, context)
        elif self.mode == 'SUB':
            _set_picked(guide_obj, cur - {c_idx}, context)
        else:
            _set_picked(guide_obj, [c_idx], context)
            return_level = context.scene.arantools_feather_ui.feather_return_level
            _set_feather_level(context, guide_obj, return_level)
        return {'FINISHED'}


def _picking_with_picks(context):
    guide_obj = get_feather_guide_from_context(context)
    return (
        is_in_feather_mode(guide_obj)
        and get_feather_level(context) == 'FEATHER'
        and bool(get_picked_chains(guide_obj))
    )


class ARANTOOLS_OT_feather_pick_confirm(Operator):
    bl_idname = "arantools.feather_pick_confirm"
    bl_label = "Confirm Picked Feathers"
    bl_description = "Edit the picked feathers: isolate them and go back to Vertices / Splines (Enter)"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return _picking_with_picks(context)

    def execute(self, context):
        guide_obj = get_feather_guide_from_context(context)
        _set_feather_level(context, guide_obj, context.scene.arantools_feather_ui.feather_return_level)
        _tag_view3d_redraw(None, context)
        return {'FINISHED'}


class ARANTOOLS_OT_feather_pick_cancel(Operator):
    bl_idname = "arantools.feather_pick_cancel"
    bl_label = "Cancel Picked Feathers"
    bl_description = "Clear the picked feathers and keep picking (Esc)"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return _picking_with_picks(context)

    def execute(self, context):
        _set_picked(get_feather_guide_from_context(context), [], context)
        return {'FINISHED'}


def _draw_feather_pick_statusbar(self, context):
    """Confirm / Cancel bar in Blender's bottom status bar while feathers are picked."""
    try:
        if not _picking_with_picks(context):
            return
        n_picked = len(get_picked_chains(get_feather_guide_from_context(context)))
    except Exception:
        return
    layout = self.layout
    row = layout.row(align=True)
    row.label(text=f"{n_picked} feather{'s' if n_picked != 1 else ''} picked", icon='STRANDS')
    row.operator("arantools.feather_pick_confirm", text="Confirm  (Enter)", icon='CHECKMARK')
    row.operator("arantools.feather_pick_cancel", text="Cancel  (Esc)", icon='X')


_STATUSBAR_DRAW_KEY = "_arantools_feather_pick_statusbar"


def _install_pick_statusbar():
    _uninstall_pick_statusbar()
    bpy.types.STATUSBAR_HT_header.prepend(_draw_feather_pick_statusbar)
    bpy.app.driver_namespace[_STATUSBAR_DRAW_KEY] = _draw_feather_pick_statusbar


def _uninstall_pick_statusbar():
    for fn in (bpy.app.driver_namespace.pop(_STATUSBAR_DRAW_KEY, None), _draw_feather_pick_statusbar):
        if fn is not None:
            try:
                bpy.types.STATUSBAR_HT_header.remove(fn)
            except Exception:
                pass


class ARANTOOLS_TL_feather_select(bpy.types.WorkSpaceTool):
    bl_space_type = 'VIEW_3D'
    bl_context_mode = 'EDIT_MESH'
    bl_idname = FEATHER_TOOL_ID
    bl_label = "Feather Select"
    bl_description = (
        "Feather Mode: hover a feather to highlight it, click to edit just that feather\n"
        "(back to Vertices / Splines). Shift+Click picks several — then Confirm (Enter) or Cancel (Esc).\n"
        "Ctrl+Click un-picks"
    )
    bl_icon = "ops.generic.select"
    bl_widget = None
    bl_keymap = (
        ("arantools.feather_hover", {"type": 'MOUSEMOVE', "value": 'ANY', "any": True}, None),
        ("arantools.feather_pick", {"type": 'LEFTMOUSE', "value": 'CLICK'},
         {"properties": [("mode", 'EDIT')]}),
        ("arantools.feather_pick", {"type": 'LEFTMOUSE', "value": 'CLICK', "shift": True},
         {"properties": [("mode", 'TOGGLE')]}),
        ("arantools.feather_pick", {"type": 'LEFTMOUSE', "value": 'CLICK', "ctrl": True},
         {"properties": [("mode", 'SUB')]}),
        ("arantools.feather_pick_confirm", {"type": 'RET', "value": 'PRESS'}, None),
        ("arantools.feather_pick_confirm", {"type": 'NUMPAD_ENTER', "value": 'PRESS'}, None),
        ("arantools.feather_pick_cancel", {"type": 'ESC', "value": 'PRESS'}, None),
    )


class ARANTOOLS_OT_feather_duplicate_selected(Operator):
    bl_idname = "arantools.feather_duplicate_selected"
    bl_label = "Duplicate Feather(s)"
    bl_description = "Duplicate the selected feather(s) with all their attributes and select the new copy"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        guide_obj = get_feather_guide_from_context(context)
        if guide_obj is None:
            return {'CANCELLED'}
        sel_chains = _get_selected_chain_indices(guide_obj)
        if not sel_chains:
            self.report({'WARNING'}, "Select at least one feather first.")
            return {'CANCELLED'}

        chains = _get_guide_chains_fast(guide_obj)
        was_edit = (guide_obj.mode == 'EDIT')
        bm = bmesh.from_edit_mesh(guide_obj.data) if was_edit else bmesh.new()
        if not was_edit:
            bm.from_mesh(guide_obj.data)

        bm.verts.ensure_lookup_table()
        bm.edges.ensure_lookup_table()
        orig_verts = list(bm.verts)
        orig_edges = list(bm.edges)

        v_layers = [bm.verts.layers.float.get(n) for n, _ in POINT_FLOAT_ATTRS]
        e_int_layers = [bm.edges.layers.int.get(n) for n, _ in EDGE_INT_ATTRS]
        rev_layer = bm.edges.layers.bool.get("feather_reverse")
        up_layer = bm.edges.layers.float_vector.get("feather_up")

        for v in orig_verts:
            v.select = False
        for e in orig_edges:
            e.select = False

        offset = Vector((0.02, 0.02, 0.02))
        for c_idx in sel_chains:
            if not (0 <= c_idx < len(chains)):
                continue
            v_chain, e_chain = chains[c_idx]
            v_map = {}
            for vi in v_chain:
                src_v = orig_verts[vi]
                nv = bm.verts.new(src_v.co + offset)
                for lyr in v_layers:
                    if lyr is not None:
                        nv[lyr] = src_v[lyr]
                nv.select = True
                v_map[vi] = nv
            for ei in e_chain:
                src_e = orig_edges[ei]
                v0 = v_map.get(src_e.verts[0].index)
                v1 = v_map.get(src_e.verts[1].index)
                if v0 and v1 and v0 != v1:
                    ne = bm.edges.new((v0, v1))
                    for lyr in e_int_layers:
                        if lyr is not None:
                            ne[lyr] = src_e[lyr]
                    if rev_layer is not None:
                        ne[rev_layer] = src_e[rev_layer]
                    if up_layer is not None:
                        ne[up_layer] = src_e[up_layer]
                    ne.select = True

        # New BMesh elements have index -1 until refreshed; chain tracing relies on indices.
        bm.verts.index_update()
        bm.edges.index_update()
        bm.select_flush_mode()
        if was_edit:
            bmesh.update_edit_mesh(guide_obj.data, loop_triangles=False, destructive=True)
        else:
            bm.to_mesh(guide_obj.data)
            bm.free()
            guide_obj.data.update()

        invalidate_feather_cache(guide_obj)
        regenerate_feather_object(guide_obj, force_full_rebuild=True)
        new_chains = _get_guide_chains_fast(guide_obj)
        new_indices = list(range(len(new_chains) - len(sel_chains), len(new_chains)))
        _set_selected_chain_indices(guide_obj, new_indices, context=context)
        self.report({'INFO'}, f"Duplicated {len(sel_chains)} feather(s).")
        return {'FINISHED'}


class ARANTOOLS_OT_feather_delete_selected(Operator):
    bl_idname = "arantools.feather_delete_selected"
    bl_label = "Delete Feather(s)"
    bl_description = "Delete the currently selected feather(s)"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        guide_obj = get_feather_guide_from_context(context)
        if guide_obj is None:
            return {'CANCELLED'}
        sel_chains = _get_selected_chain_indices(guide_obj)
        if not sel_chains:
            self.report({'WARNING'}, "Select at least one feather to delete.")
            return {'CANCELLED'}

        chains = _get_guide_chains_fast(guide_obj)
        del_v_indices = set()
        for c_idx in sel_chains:
            if 0 <= c_idx < len(chains):
                del_v_indices.update(chains[c_idx][0])

        was_edit = (guide_obj.mode == 'EDIT')
        bm = bmesh.from_edit_mesh(guide_obj.data) if was_edit else bmesh.new()
        if not was_edit:
            bm.from_mesh(guide_obj.data)

        bm.verts.ensure_lookup_table()
        verts_to_remove = [bm.verts[vi] for vi in del_v_indices if vi < len(bm.verts)]
        for v in verts_to_remove:
            bm.verts.remove(v)

        if was_edit:
            bmesh.update_edit_mesh(guide_obj.data, loop_triangles=False, destructive=True)
        else:
            bm.to_mesh(guide_obj.data)
            bm.free()
            guide_obj.data.update()

        invalidate_feather_cache(guide_obj)
        regenerate_feather_object(guide_obj, force_full_rebuild=True)
        _set_selected_chain_indices(guide_obj, [], context=context)
        for area in context.screen.areas if context.screen else []:
            area.tag_redraw()
        self.report({'INFO'}, f"Deleted {len(sel_chains)} feather(s).")
        return {'FINISHED'}


class ARANTOOLS_OT_feather_taper_radius(Operator):
    bl_idname = "arantools.feather_taper_radius"
    bl_label = "Taper Root -> Tip"
    bl_description = "Smoothly taper the vertex radius along the selected feather(s) from Root to Tip"
    bl_options = {'REGISTER', 'UNDO'}

    tip_factor: FloatProperty(name="Tip Scale", default=0.55, min=0.05, max=2.0)

    def execute(self, context):
        guide_obj = get_feather_guide_from_context(context)
        if guide_obj is None:
            return {'CANCELLED'}
        chains = _get_guide_chains_fast(guide_obj)
        sel_chains = _get_selected_chain_indices(guide_obj) or list(range(len(chains)))

        was_edit = (guide_obj.mode == 'EDIT')
        bm = bmesh.from_edit_mesh(guide_obj.data) if was_edit else bmesh.new()
        if not was_edit:
            bm.from_mesh(guide_obj.data)

        bm.verts.ensure_lookup_table()
        bm.edges.ensure_lookup_table()
        rad_layer = bm.verts.layers.float.get("radius") or bm.verts.layers.float.new("radius")
        rev_layer = bm.edges.layers.bool.get("feather_reverse")

        for c_idx in sel_chains:
            if not (0 <= c_idx < len(chains)):
                continue
            v_chain, e_chain = chains[c_idx]
            if len(v_chain) < 2:
                continue
            is_rev = any(bool(bm.edges[ei][rev_layer]) for ei in e_chain) if rev_layer else False
            ordered_v = list(reversed(v_chain)) if is_rev else list(v_chain)
            base_rad = float(bm.verts[ordered_v[0]][rad_layer])
            if base_rad <= 0.01:
                base_rad = 1.5
            n_v = len(ordered_v)
            for idx, vi in enumerate(ordered_v):
                t = idx / float(n_v - 1)
                bm.verts[vi][rad_layer] = base_rad * ((1.0 - t) + t * self.tip_factor)

        if was_edit:
            bmesh.update_edit_mesh(guide_obj.data, loop_triangles=False, destructive=False)
        else:
            bm.to_mesh(guide_obj.data)
            bm.free()
            guide_obj.data.update()

        sync_ui_from_selection(context)
        regenerate_feather_object(guide_obj)
        return {'FINISHED'}


class ARANTOOLS_OT_feather_regenerate(Operator):
    bl_idname = "arantools.feather_regenerate"
    bl_label = "Regenerate Feather Mesh"
    bl_description = "Rebuild the 3D feather cards mesh from the Feather Guide edges and attributes"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        guide_obj = get_feather_guide_from_context(context)
        if guide_obj is None:
            self.report({'WARNING'}, "No active Feather Object.")
            return {'CANCELLED'}
        out_obj = regenerate_feather_object(guide_obj, force_full_rebuild=True)
        if out_obj is not None:
            self.report({'INFO'}, f"Regenerated '{out_obj.name}' ({len(out_obj.data.polygons)} polys).")
        return {'FINISHED'}


class ARANTOOLS_OT_feather_flip_direction(Operator):
    bl_idname = "arantools.feather_flip_direction"
    bl_label = "Flip Direction"
    bl_description = (
        "Flip the Root-to-Tip direction of the selected feather(s), "
        "visually reversing the feather along its spline"
    )
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        guide_obj = get_feather_guide_from_context(context)
        if guide_obj is None:
            return {'CANCELLED'}

        if guide_obj.mode == 'EDIT':
            bm = bmesh.from_edit_mesh(guide_obj.data)
            layer = bm.edges.layers.bool.get("feather_reverse")
            if layer is None:
                layer = bm.edges.layers.bool.new("feather_reverse")
            _, target_edges = _get_selected_bm_elements(bm, whole_chain=True)
            if not target_edges:
                target_edges = [e for e in bm.edges if not e.hide]
            if not target_edges:
                self.report({'WARNING'}, "Select at least one feather first.")
                return {'CANCELLED'}
            for e in target_edges:
                e[layer] = not bool(e[layer])
            # Twist is a rotation around the spline tangent; reversing the spline reverses that axis,
            # so negate tilt to keep each card's twist looking the same.
            tilt_layer = bm.verts.layers.float.get("tilt")
            if tilt_layer is not None:
                for v in {v for e in target_edges for v in e.verts}:
                    v[tilt_layer] = -float(v[tilt_layer])
            bmesh.update_edit_mesh(guide_obj.data, loop_triangles=False, destructive=False)
            sync_ui_from_selection(context)
        else:
            ensure_feather_guide_attributes(guide_obj.data)
            target_e = _get_object_mode_selected_edge_indices(guide_obj, whole_chain=True)
            if not target_e:
                self.report({'WARNING'}, "Select a feather in the viewport first.")
                return {'CANCELLED'}
            attr = guide_obj.data.attributes.get("feather_reverse")
            if attr is not None:
                for ei in target_e:
                    attr.data[ei].value = not bool(attr.data[ei].value)
            tilt_attr = guide_obj.data.attributes.get("tilt")
            if tilt_attr is not None:
                edges = guide_obj.data.edges
                for vi in {vi for ei in target_e for vi in edges[ei].vertices}:
                    tilt_attr.data[vi].value = -float(tilt_attr.data[vi].value)
            sync_ui_from_selection(context)

        regenerate_feather_object(guide_obj)
        return {'FINISHED'}


class ARANTOOLS_OT_feather_flip_keep_look(Operator):
    bl_idname = "arantools.feather_flip_keep_look"
    bl_label = "Flip (Keep Look) — Invert Base Meshes"
    bl_description = (
        "Invert the original base feather meshes (180° around normal axis) AND flip all "
        "feather spline directions + negate tilt so the internal Root -> Tip direction is "
        "inverted while keeping the 3D feather look 100% identical!"
    )
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        guide_obj = get_feather_guide_from_context(context)
        if guide_obj is None:
            self.report({'WARNING'}, "Select a Feather Object first.")
            return {'CANCELLED'}

        fprops = guide_obj.arantools_feather
        axis = int(fprops.deform_axis)
        ax0, ax1, ax2 = axis, (axis + 1) % 3, (axis + 2) % 3

        inverted_meshes = set()
        for slot in fprops.mesh_slots:
            mobj = slot.mesh_obj
            if mobj is None or mobj.type != 'MESH' or mobj.data is None:
                continue
            if mobj.data.name in inverted_meshes:
                continue
            inverted_meshes.add(mobj.data.name)
            for v in mobj.data.vertices:
                co = v.co
                co[ax0] = -co[ax0]
                co[ax2] = -co[ax2]
            mobj.data.update()

        if guide_obj.mode == 'EDIT':
            bm = bmesh.from_edit_mesh(guide_obj.data)
            rev_layer = bm.edges.layers.bool.get("feather_reverse")
            if rev_layer is None:
                rev_layer = bm.edges.layers.bool.new("feather_reverse")
            for e in bm.edges:
                e[rev_layer] = not bool(e[rev_layer])

            tilt_layer = bm.verts.layers.float.get("tilt")
            if tilt_layer is not None:
                for v in bm.verts:
                    v[tilt_layer] = -float(v[tilt_layer])

            bmesh.update_edit_mesh(guide_obj.data, loop_triangles=False, destructive=False)
            sync_ui_from_selection(context)
        else:
            ensure_feather_guide_attributes(guide_obj.data)
            rev_attr = guide_obj.data.attributes.get("feather_reverse")
            if rev_attr is not None:
                for d in rev_attr.data:
                    d.value = not bool(d.value)

            tilt_attr = guide_obj.data.attributes.get("tilt")
            if tilt_attr is not None:
                for d in tilt_attr.data:
                    d.value = -float(d.value)
            sync_ui_from_selection(context)

        fprops.invert_curve_profile = not bool(fprops.invert_curve_profile)

        invalidate_feather_cache(guide_obj)
        regenerate_feather_object(guide_obj, force_full_rebuild=True)

        for area in context.screen.areas if context.screen else []:
            area.tag_redraw()

        self.report(
            {'INFO'},
            f"Inverted {len(inverted_meshes)} base mesh(es) & flipped spline directions (visual look preserved).",
        )
        return {'FINISHED'}


class ARANTOOLS_OT_feather_assign_slot(Operator):
    bl_idname = "arantools.feather_assign_slot"
    bl_label = "Assign Feather Mesh"
    bl_description = "Click: Select as ONLY feather mesh  |  Shift+Click (or Checkbox): Toggle random selection between checked meshes"
    bl_options = {'REGISTER', 'UNDO'}

    slot_index: IntProperty(default=-1)
    mesh_name: StringProperty(default="")
    extend_random: BoolProperty(default=False)
    force_random_toggle: BoolProperty(default=False)

    def invoke(self, context, event):
        self.extend_random = bool(event.shift) or bool(self.force_random_toggle)
        return self.execute(context)

    def execute(self, context):
        guide_obj = get_feather_guide_from_context(context)
        if guide_obj is None:
            return {'CANCELLED'}

        slot_idx = self.slot_index
        if self.mesh_name:
            mobj = bpy.data.objects.get(self.mesh_name)
            if mobj is not None:
                slot_idx = find_or_add_mesh_slot(guide_obj, mobj)

        slots = guide_obj.arantools_feather.mesh_slots
        if not (0 <= slot_idx < len(slots)):
            return {'CANCELLED'}

        use_random = bool(self.extend_random) or bool(self.force_random_toggle)
        bit = 1 << max(0, min(29, slot_idx))

        if guide_obj.mode == 'EDIT':
            bm = bmesh.from_edit_mesh(guide_obj.data)
            slot_layer = bm.edges.layers.int.get("feather_mesh_slot") or bm.edges.layers.int.new("feather_mesh_slot")
            mask_layer = bm.edges.layers.int.get("feather_mesh_mask") or bm.edges.layers.int.new("feather_mesh_mask")

            _, target_edges = _get_selected_bm_elements(bm, whole_chain=True)
            if not target_edges:
                target_edges = [e for e in bm.edges if not e.hide]
            if not target_edges:
                self.report({'WARNING'}, "Select a feather in the viewport first.")
                return {'CANCELLED'}

            if use_random:
                all_have_bit = True
                for e in target_edges:
                    cur_m = int(e[mask_layer])
                    if cur_m < 0:
                        cur_m = 1 << max(0, min(29, int(e[slot_layer])))
                    if not (cur_m & bit):
                        all_have_bit = False
                        break

                for e in target_edges:
                    cur_m = int(e[mask_layer])
                    if cur_m < 0:
                        cur_m = 1 << max(0, min(29, int(e[slot_layer])))
                    new_m = (cur_m & ~bit) if all_have_bit else (cur_m | bit)
                    e[mask_layer] = new_m
                    if new_m > 0:
                        for s_i in range(len(slots)):
                            if new_m & (1 << s_i):
                                e[slot_layer] = s_i
                                break
            else:
                for e in target_edges:
                    e[slot_layer] = slot_idx
                    e[mask_layer] = bit

            bmesh.update_edit_mesh(guide_obj.data, loop_triangles=False, destructive=False)
            sync_ui_from_selection(context)
        else:
            ensure_feather_guide_attributes(guide_obj.data)
            target_e = _get_object_mode_selected_edge_indices(guide_obj, whole_chain=True)
            if not target_e:
                self.report({'WARNING'}, "Select a feather in the viewport first.")
                return {'CANCELLED'}
            slot_attr = guide_obj.data.attributes["feather_mesh_slot"]
            mask_attr = guide_obj.data.attributes["feather_mesh_mask"]
            if use_random:
                all_have_bit = True
                for ei in target_e:
                    cur_m = int(mask_attr.data[ei].value)
                    if cur_m < 0:
                        cur_m = 1 << max(0, min(29, int(slot_attr.data[ei].value)))
                    if not (cur_m & bit):
                        all_have_bit = False
                        break
                for ei in target_e:
                    cur_m = int(mask_attr.data[ei].value)
                    if cur_m < 0:
                        cur_m = 1 << max(0, min(29, int(slot_attr.data[ei].value)))
                    new_m = (cur_m & ~bit) if all_have_bit else (cur_m | bit)
                    mask_attr.data[ei].value = new_m
                    if new_m > 0:
                        for s_i in range(len(slots)):
                            if new_m & (1 << s_i):
                                slot_attr.data[ei].value = s_i
                                break
            else:
                for ei in target_e:
                    slot_attr.data[ei].value = slot_idx
                    mask_attr.data[ei].value = bit
            sync_ui_from_selection(context)

        regenerate_feather_object(guide_obj)
        return {'FINISHED'}


class ARANTOOLS_OT_feather_mesh_mask_all_none(Operator):
    bl_idname = "arantools.feather_mesh_mask_all_none"
    bl_label = "Set Feather Cards (All / None / Re-Roll)"
    bl_description = "Enable all card meshes (random between all), None, or Re-Roll random variation on selected feather(s)"
    bl_options = {'REGISTER', 'UNDO'}

    mode: EnumProperty(
        items=[
            ('ALL', "All", "Enable all feather card meshes (randomly pick among all)"),
            ('NONE', "None", "Clear all feather card meshes on selected feather(s)"),
            ('REROLL', "Re-Roll Random", "Roll a new random mesh choice among the checked meshes"),
        ],
        default='ALL',
    )

    def execute(self, context):
        guide_obj = get_feather_guide_from_context(context)
        if guide_obj is None:
            return {'CANCELLED'}

        admitted = sync_admitted_collection_to_slots(guide_obj)
        all_mask = 0
        first_slot = 0
        for idx_item, (s_idx, _) in enumerate(admitted):
            if idx_item == 0:
                first_slot = s_idx
            all_mask |= (1 << max(0, min(29, s_idx)))

        if guide_obj.mode == 'EDIT':
            bm = bmesh.from_edit_mesh(guide_obj.data)
            slot_layer = bm.edges.layers.int.get("feather_mesh_slot") or bm.edges.layers.int.new("feather_mesh_slot")
            mask_layer = bm.edges.layers.int.get("feather_mesh_mask") or bm.edges.layers.int.new("feather_mesh_mask")
            seed_layer = bm.edges.layers.int.get("feather_seed") or bm.edges.layers.int.new("feather_seed")

            _, target_edges = _get_selected_bm_elements(bm, whole_chain=True)
            if not target_edges:
                target_edges = [e for e in bm.edges if not e.hide]

            for e in target_edges:
                if self.mode == 'ALL':
                    e[mask_layer] = all_mask
                    e[slot_layer] = first_slot
                elif self.mode == 'NONE':
                    e[mask_layer] = 0
                elif self.mode == 'REROLL':
                    e[seed_layer] = int(e[seed_layer]) + 1

            bmesh.update_edit_mesh(guide_obj.data, loop_triangles=False, destructive=False)
            sync_ui_from_selection(context)
        else:
            ensure_feather_guide_attributes(guide_obj.data)
            target_e = _get_object_mode_selected_edge_indices(guide_obj, whole_chain=True)
            slot_attr = guide_obj.data.attributes["feather_mesh_slot"]
            mask_attr = guide_obj.data.attributes["feather_mesh_mask"]
            seed_attr = guide_obj.data.attributes["feather_seed"]
            for ei in target_e:
                if self.mode == 'ALL':
                    mask_attr.data[ei].value = all_mask
                    slot_attr.data[ei].value = first_slot
                elif self.mode == 'NONE':
                    mask_attr.data[ei].value = 0
                elif self.mode == 'REROLL':
                    seed_attr.data[ei].value = int(seed_attr.data[ei].value) + 1
            sync_ui_from_selection(context)

        regenerate_feather_object(guide_obj)
        return {'FINISHED'}


class ARANTOOLS_OT_feather_sync_admitted_collection(Operator):
    bl_idname = "arantools.feather_sync_admitted_collection"
    bl_label = "Refresh Feather Cards from Collection"
    bl_description = "Re-read the feather card meshes from the chosen collection"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        guide_obj = get_feather_guide_from_context(context)
        if guide_obj is None:
            return {'CANCELLED'}
        if guide_obj.arantools_feather.admitted_collection is None:
            self.report({'WARNING'}, "Pick a Feather Cards collection first.")
            return {'CANCELLED'}
        cards = sync_admitted_collection_to_slots(guide_obj)
        regenerate_feather_object(guide_obj, force_full_rebuild=True)
        self.report({'INFO'}, f"Loaded {len(cards)} feather card mesh(es) from collection.")
        return {'FINISHED'}


class ARANTOOLS_UL_FeatherAdmittedList(UIList):
    """Native Blender UIList for the Feather Card meshes.
    - In Object Mode: displays the feather card meshes in the chosen collection with polygon counts.
    - In Feather Mode: displays a checkbox on the left (to toggle random inclusion) + clickable mesh name
      (Click = Select as ONLY mesh, Shift+Click = Toggle Random).
    """
    bl_idname = "ARANTOOLS_UL_FeatherAdmittedList"

    def filter_items(self, context, data, propname):
        slots = getattr(data, propname)
        admitted_coll = getattr(data, "admitted_collection", None)
        if admitted_coll is not None:
            admitted_set = set(_get_collection_feather_meshes(admitted_coll, data))
        else:
            admitted_set = {s.mesh_obj for s in slots if s.mesh_obj is not None}

        flt_flags = []
        for s in slots:
            if s.mesh_obj is not None and s.mesh_obj in admitted_set:
                flt_flags.append(self.bitflag_filter_item)
            else:
                flt_flags.append(0)
        return flt_flags, []

    def draw_item(self, context, layout, data, item, icon, active_data, active_propname, index):
        mobj = item.mesh_obj
        if mobj is None:
            layout.label(text="(Empty)", icon='MESH_DATA')
            return

        guide_obj = get_feather_guide_from_context(context)
        row = layout.row(align=True)
        if is_in_feather_mode(guide_obj):
            and_mask, or_mask, num_sel = get_selected_feathers_mask_state(guide_obj)
            bit = 1 << max(0, min(29, index))
            is_all_checked = (and_mask & bit) != 0 and num_sel > 0
            is_some_checked = (or_mask & bit) != 0 and num_sel > 0

            if is_all_checked:
                chk_icon = 'CHECKBOX_HLT'
            elif is_some_checked:
                chk_icon = 'DOT'
            else:
                chk_icon = 'CHECKBOX_DEHLT'

            # 1. Left Checkbox: clicking directly toggles this mesh in/out of the Random Pool
            op_chk = row.operator(
                "arantools.feather_assign_slot",
                text="",
                icon=chk_icon,
                emboss=False,
            )
            op_chk.slot_index = index
            op_chk.mesh_name = mobj.name
            op_chk.force_random_toggle = True

            # 2. Mesh Name: Click = ONLY this mesh  |  Shift+Click = Toggle in Random Pool
            op_sel = row.operator(
                "arantools.feather_assign_slot",
                text=mobj.name,
                icon='OUTLINER_OB_MESH',
                emboss=False,
            )
            op_sel.slot_index = index
            op_sel.mesh_name = mobj.name
            op_sel.force_random_toggle = False
        else:
            # Object Mode: clean list row with mesh name and face count
            row.label(text=mobj.name, icon='OUTLINER_OB_MESH')
            if mobj.data is not None:
                sub = row.row(align=True)
                sub.alignment = 'RIGHT'
                sub.enabled = False
                sub.label(text=f"{len(mobj.data.polygons)} faces")


class ARANTOOLS_OT_feather_select_chain(Operator):
    bl_idname = "arantools.feather_select_chain"
    bl_label = "Select Whole Feather"
    bl_description = "Select all vertices and edges belonging to the currently selected feather chain(s)"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        guide_obj = get_feather_guide_from_context(context)
        if guide_obj is None or guide_obj.mode != 'EDIT':
            return {'CANCELLED'}
        bm = bmesh.from_edit_mesh(guide_obj.data)
        _, chain_edges = _get_selected_bm_elements(bm, whole_chain=True)
        for e in chain_edges:
            e.select = True
            e.verts[0].select = True
            e.verts[1].select = True
        bm.select_flush_mode()
        bmesh.update_edit_mesh(guide_obj.data, loop_triangles=False, destructive=False)
        return {'FINISHED'}


def _feather_point_normals(guide_obj, vert_indices):
    """Local-space card normal at each guide vertex in `vert_indices`: the feather's up vector
    twisted by that point's tilt around the spline tangent (same frame the generator uses)."""
    verts_co, edges_v, pt_layers, _, edge_bool, edge_up = _extract_guide_data(guide_obj)
    chains = _trace_chains_from_edges(len(verts_co), edges_v)
    wanted = set(vert_indices)
    tilt_list = pt_layers["tilt"]
    rev_list = edge_bool["feather_reverse"]
    normals = {}
    for v_chain, e_chain in chains:
        if not e_chain or not wanted.intersection(v_chain):
            continue
        ordered = list(reversed(v_chain)) if any(rev_list[ei] for ei in e_chain) else list(v_chain)
        n_base = Vector(edge_up[e_chain[0]])
        n_base = n_base.normalized() if n_base.length_squared > 1e-12 else Vector((0.0, 1.0, 0.0))
        pts = [Vector(verts_co[vi]) for vi in ordered]
        for k, vi in enumerate(ordered):
            if vi not in wanted:
                continue
            tan = pts[min(k + 1, len(pts) - 1)] - pts[max(k - 1, 0)]
            if tan.length_squared < 1e-12:
                normals[vi] = n_base.copy()
                continue
            tan.normalize()
            nrm = Quaternion(tan, tilt_list[vi]) @ n_base
            nrm -= tan * nrm.dot(tan)
            normals[vi] = nrm.normalized() if nrm.length_squared > 1e-12 else n_base.copy()
    return normals


# ── Push Out: a slider that moves the selected points and springs back to 0 when released ──
#
# Blender sliders can't report "released" to an addon, so the slider's update applies the change
# since the last value, and a passive listener (never consumes events) resets it to 0 on mouse
# release / Enter / Esc. Esc during a drag makes Blender restore 0 itself, which moves the points
# back. If the listener can't run, the slider resets after a short idle time instead.
_PUSH = {"applied": 0.0, "dirs": None, "guide": "", "resetting": False, "last_update": 0.0,
         "watch_seen": 0.0, "watch_gen": 0}


def _push_directions(context, guide_obj, ui):
    """Local-space unit offset (per 1 m of world movement) for each target point, at drag start."""
    bm = bmesh.from_edit_mesh(guide_obj.data)
    sel_verts, chain_edges = _get_selected_bm_elements(bm, whole_chain=ui.apply_whole_chain)
    targets = set(sel_verts)
    if ui.apply_whole_chain:
        for e in chain_edges:
            targets.update(e.verts)
    targets = [v for v in targets if not v.hide]
    if not targets:
        return {}

    mw = guide_obj.matrix_world
    mw3 = mw.to_3x3()
    mw_inv3 = mw.inverted_safe().to_3x3()
    if ui.tweak_direction == 'NORMAL':
        normals = _feather_point_normals(guide_obj, [v.index for v in targets])
        dirs_w = {v.index: (mw3 @ normals.get(v.index, Vector((0.0, 0.0, 1.0)))).normalized() for v in targets}
    else:
        space = context.space_data if getattr(context, "space_data", None) and context.space_data.type == 'VIEW_3D' else None
        if space is None:
            space = next((a.spaces.active for a in (context.screen.areas if context.screen else [])
                          if a.type == 'VIEW_3D'), None)
        rv3d = getattr(space, "region_3d", None)
        if rv3d is None:
            return {}
        view_inv = rv3d.view_matrix.inverted()
        toward_viewer = (view_inv.to_3x3() @ Vector((0.0, 0.0, 1.0))).normalized()
        dirs_w = {}
        for v in targets:
            d = view_inv.translation - mw @ v.co if rv3d.is_perspective else toward_viewer
            dirs_w[v.index] = d.normalized() if d.length_squared > 1e-12 else toward_viewer
    return {vi: mw_inv3 @ d for vi, d in dirs_w.items()}


def _on_push_out(self, context):
    if _PUSH["resetting"] or _IS_SYNCING_UI:
        return
    guide_obj = get_feather_guide_from_context(context)
    if not is_in_feather_mode(guide_obj):
        return
    if _PUSH["dirs"] is None or _PUSH["guide"] != guide_obj.name:
        _PUSH.update(dirs=_push_directions(context, guide_obj, self), guide=guide_obj.name, applied=0.0)
        _start_push_listener(context)
    delta = self.tweak_push_out - _PUSH["applied"]
    _PUSH["applied"] = self.tweak_push_out
    _PUSH["last_update"] = time.monotonic()
    if not _PUSH["dirs"] or delta == 0.0:
        return
    bm = bmesh.from_edit_mesh(guide_obj.data)
    bm.verts.ensure_lookup_table()
    for vi, d in _PUSH["dirs"].items():
        if vi < len(bm.verts):
            bm.verts[vi].co += d * delta
    bmesh.update_edit_mesh(guide_obj.data, loop_triangles=False, destructive=False)
    regenerate_feather_object(guide_obj, coords_only_hint=True)


def _reset_push():
    """Commit the move: slider back to 0 without moving anything."""
    scene = getattr(bpy.context, "scene", None)
    ui = getattr(scene, "arantools_feather_ui", None)
    _PUSH["resetting"] = True
    try:
        if ui is not None and ui.tweak_push_out != 0.0:
            ui.tweak_push_out = 0.0
    finally:
        _PUSH.update(resetting=False, applied=0.0, dirs=None, guide="")
    return None


def _push_idle_fallback():
    """Used only if the release listener isn't running: reset after a short idle time."""
    if _PUSH["dirs"] is None:
        return None
    listener_alive = time.monotonic() - _PUSH["watch_seen"] < 0.5
    if listener_alive:
        return 0.5
    if time.monotonic() - _PUSH["last_update"] > 1.0:
        _reset_push()
        return None
    return 0.25


def _start_push_listener(context):
    win, area = context.window, context.area

    def _start():
        try:
            with bpy.context.temp_override(window=win, area=area):
                bpy.ops.arantools.feather_push_listener('INVOKE_DEFAULT')
        except Exception:
            pass
        return None

    if win is not None and not bpy.app.background:
        bpy.app.timers.register(_start, first_interval=0.0)
    bpy.app.timers.register(_push_idle_fallback, first_interval=0.5)


class ARANTOOLS_OT_feather_push_listener(Operator):
    """Passive: waits for the Push Out slider to be released, then springs it back to 0."""
    bl_idname = "arantools.feather_push_listener"
    bl_label = "Push Out Release Listener"
    bl_options = {'INTERNAL'}

    def invoke(self, context, event):
        _PUSH["watch_gen"] += 1
        self._gen = _PUSH["watch_gen"]
        _PUSH["watch_seen"] = time.monotonic()
        context.window_manager.modal_handler_add(self)
        return {'RUNNING_MODAL', 'PASS_THROUGH'}

    def modal(self, context, event):
        if self._gen != _PUSH["watch_gen"] or _PUSH["dirs"] is None:
            return {'CANCELLED', 'PASS_THROUGH'}
        _PUSH["watch_seen"] = time.monotonic()
        released = (
            (event.type == 'LEFTMOUSE' and event.value == 'RELEASE')
            or (event.type in {'RET', 'NUMPAD_ENTER', 'ESC', 'RIGHTMOUSE'} and event.value == 'PRESS')
        )
        if released:
            # Let the slider finish handling this event first, then reset
            bpy.app.timers.register(_reset_push, first_interval=0.05)
            return {'FINISHED', 'PASS_THROUGH'}
        return {'PASS_THROUGH'}


class ARANTOOLS_OT_feather_adjust_attr(Operator):
    bl_idname = "arantools.feather_adjust_attr"
    bl_label = "Adjust Feather Vertex Attribute"
    bl_description = "Quick-adjust radius or tilt on the selected vertices or feather chain"
    bl_options = {'REGISTER', 'UNDO'}

    attr_name: EnumProperty(
        items=[
            ('radius', "Radius", ""),
            ('tilt', "Tilt", ""),
        ],
        default='radius',
    )
    mode: EnumProperty(
        items=[
            ('SCALE', "Scale", ""),
            ('ADD', "Add", ""),
            ('SET', "Set", ""),
            ('TAPER', "Taper Root->Tip", ""),
        ],
        default='SCALE',
    )
    amount: FloatProperty(default=1.0)

    def execute(self, context):
        guide_obj = get_feather_guide_from_context(context)
        if guide_obj is None or guide_obj.mode != 'EDIT':
            return {'CANCELLED'}
        bm = bmesh.from_edit_mesh(guide_obj.data)
        layer = bm.verts.layers.float.get(self.attr_name)
        if layer is None:
            layer = bm.verts.layers.float.new(self.attr_name)

        ui = context.scene.arantools_feather_ui
        sel_verts, chain_edges = _get_selected_bm_elements(bm, whole_chain=ui.apply_whole_chain)
        if self.mode == 'TAPER':
            _, full_edges = _get_selected_bm_elements(bm, whole_chain=True)
            v_set = set()
            for e in full_edges:
                v_set.add(e.verts[0])
                v_set.add(e.verts[1])
            for v in v_set:
                v[layer] = max(0.05, v[layer] * self.amount)
        else:
            target_verts = sel_verts
            if ui.apply_whole_chain and chain_edges:
                vset = set(sel_verts)
                for e in chain_edges:
                    vset.add(e.verts[0])
                    vset.add(e.verts[1])
                target_verts = list(vset)
            for v in target_verts:
                if self.mode == 'SCALE':
                    v[layer] = max(0.0, v[layer] * self.amount)
                elif self.mode == 'ADD':
                    v[layer] += self.amount
                elif self.mode == 'SET':
                    v[layer] = self.amount

        bmesh.update_edit_mesh(guide_obj.data, loop_triangles=False, destructive=False)
        sync_ui_from_selection(context)
        regenerate_feather_object(guide_obj)
        return {'FINISHED'}


class ARANTOOLS_OT_feather_add_chain(Operator):
    bl_idname = "arantools.feather_add_chain"
    bl_label = "Add Feather at Cursor"
    bl_description = "Add a new 4-vertex feather spline at the 3D Cursor using the active Feather Mesh"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        guide_obj = get_feather_guide_from_context(context)
        if guide_obj is None:
            return {'CANCELLED'}

        ui = context.scene.arantools_feather_ui
        fprops = guide_obj.arantools_feather
        slot_idx = fprops.active_slot_index if (0 <= fprops.active_slot_index < len(fprops.mesh_slots)) else 0
        if ui.edge_mesh_obj is not None:
            slot_idx = find_or_add_mesh_slot(guide_obj, ui.edge_mesh_obj)
        cursor_local = guide_obj.matrix_world.inverted_safe() @ context.scene.cursor.location

        was_edit = (guide_obj.mode == 'EDIT')
        bm = bmesh.from_edit_mesh(guide_obj.data) if was_edit else bmesh.new()
        if not was_edit:
            bm.from_mesh(guide_obj.data)

        for v in bm.verts:
            v.select = False
        for e in bm.edges:
            e.select = False

        rad_layer = bm.verts.layers.float.get("radius") or bm.verts.layers.float.new("radius")
        tilt_layer = bm.verts.layers.float.get("tilt") or bm.verts.layers.float.new("tilt")
        slot_layer = bm.edges.layers.int.get("feather_mesh_slot") or bm.edges.layers.int.new("feather_mesh_slot")
        mask_layer = bm.edges.layers.int.get("feather_mesh_mask") or bm.edges.layers.int.new("feather_mesh_mask")
        seed_layer = bm.edges.layers.int.get("feather_seed") or bm.edges.layers.int.new("feather_seed")
        subdiv_layer = bm.edges.layers.int.get("feather_subdiv") or bm.edges.layers.int.new("feather_subdiv")
        st_layer = bm.edges.layers.int.get("feather_smooth_tilt") or bm.edges.layers.int.new("feather_smooth_tilt")
        ss_layer = bm.edges.layers.int.get("feather_smooth_spline") or bm.edges.layers.int.new("feather_smooth_spline")
        up_layer = bm.edges.layers.float_vector.get("feather_up") or bm.edges.layers.float_vector.new("feather_up")
        mirror_layer = bm.edges.layers.bool.get("feather_mirror") or bm.edges.layers.bool.new("feather_mirror")

        new_verts = []
        for i in range(4):
            v = bm.verts.new(cursor_local + Vector((0.0, i * 0.08, 0.0)))
            v[rad_layer] = ui.vert_radius if ui.vert_radius > 0.0 else 1.5
            v[tilt_layer] = ui.vert_tilt
            v.select = True
            new_verts.append(v)

        chain_seed = len(bm.edges) + 1
        for i in range(3):
            e = bm.edges.new((new_verts[i], new_verts[i + 1]))
            e[slot_layer] = slot_idx
            e[mask_layer] = 1 << max(0, slot_idx)
            e[seed_layer] = chain_seed
            e[subdiv_layer] = ui.edge_subdiv
            e[st_layer] = ui.edge_smooth_tilt
            e[ss_layer] = ui.edge_smooth_spline
            # Up must not be parallel to the spline (+Y here) or the card collapses to zero width
            e[up_layer] = (0.0, 0.0, 1.0)
            e[mirror_layer] = True
            e.select = True

        # New BMesh elements have index -1 until refreshed; chain tracing relies on indices.
        bm.verts.index_update()
        bm.edges.index_update()
        bm.select_flush_mode()
        if was_edit:
            bmesh.update_edit_mesh(guide_obj.data, loop_triangles=False, destructive=True)
        else:
            bm.to_mesh(guide_obj.data)
            bm.free()
            guide_obj.data.update()

        invalidate_feather_cache(guide_obj)
        regenerate_feather_object(guide_obj, force_full_rebuild=True)
        chains = _get_guide_chains_fast(guide_obj)
        if chains:
            _set_selected_chain_indices(guide_obj, [len(chains) - 1], context=context)
        return {'FINISHED'}


class ARANTOOLS_OT_feather_create_object(Operator):
    bl_idname = "arantools.feather_create_object"
    bl_label = "Create New Feather Object"
    bl_description = "Create a new empty Feather Object (or convert the active edge mesh into a Feather Object)"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        ui = context.scene.arantools_feather_ui
        target_coll = context.collection

        obj = context.active_object
        if obj is not None and obj.type == 'MESH' and len(obj.data.polygons) == 0 and not obj.arantools_feather.is_feather_guide:
            guide_obj = obj
        else:
            mesh = bpy.data.meshes.new("AranFeather_GuideMesh")
            guide_obj = bpy.data.objects.new("AranFeather", mesh)
            target_coll.objects.link(guide_obj)

        # Material / mirror / surface are left for the artist to set per Feather Object.
        guide_obj.show_in_front = True
        fprops = guide_obj.arantools_feather
        fprops.is_feather_guide = True

        if ui.edge_mesh_obj is not None:
            find_or_add_mesh_slot(guide_obj, ui.edge_mesh_obj)

        ensure_feather_guide_attributes(guide_obj.data)
        regenerate_feather_object(guide_obj, force_full_rebuild=True)

        if context.mode != 'OBJECT':
            bpy.ops.object.mode_set(mode='OBJECT')
        bpy.ops.object.select_all(action='DESELECT')
        guide_obj.select_set(True)
        context.view_layer.objects.active = guide_obj
        return {'FINISHED'}


# ============================================================================
# Depsgraph Handler, 3D Viewport Header & GPU Overlay
# ============================================================================

@bpy.app.handlers.persistent
def _feather_undo_post(scene, *_args):
    """Resync after Ctrl+Z / Ctrl+Shift+Z.

    Undo restores the guide mesh, but the runtime cache still describes the pre-undo state, and in
    Edit Mode the generated cards (a separate object) aren't part of the edit-mesh undo step at all.
    The depsgraph handler won't catch it either when only attributes (flip, tilt, mesh slot, ...)
    changed, since it only regenerates on moved vertices. So rebuild from scratch here.
    """
    if _IS_GENERATING:
        return
    invalidate_feather_cache()
    context = bpy.context
    guide_obj = get_feather_guide_from_context(context)
    if guide_obj is None:
        return
    try:
        regenerate_feather_object(guide_obj, force_full_rebuild=True)
        sync_ui_from_selection(context)
    except Exception as exc:
        print(f"[AranTools] Feather undo resync warning: {exc}")


@bpy.app.handlers.persistent
def _feather_depsgraph_update_post(scene, depsgraph):
    global _LAST_SELECTION_SIGNATURE
    if _IS_GENERATING or _IS_SYNCING_UI:
        return

    context = bpy.context
    guide_obj = get_feather_guide_from_context(context)
    if guide_obj is None:
        return

    fprops = guide_obj.arantools_feather

    if guide_obj.mode == 'EDIT':
        touched = False
        mesh_updated = False
        for update in depsgraph.updates:
            orig_id = update.id.original
            if orig_id == guide_obj or orig_id == guide_obj.data:
                touched = True
                if update.is_updated_geometry:
                    mesh_updated = True
                    break

        if not touched:
            return

        try:
            bm = bmesh.from_edit_mesh(guide_obj.data)
            # Selection changes don't count as geometry updates, but the panel sliders must show
            # the newly selected point's values — otherwise dragging one applies a stale value.
            sel_v = tuple(v.index for v in bm.verts if v.select)
            sel_e = tuple(e.index for e in bm.edges if e.select)
            act_id = getattr(bm.select_history.active, "index", -1)
            sig = (guide_obj.name, sel_v, sel_e, act_id)
            if sig != _LAST_SELECTION_SIGNATURE:
                _LAST_SELECTION_SIGNATURE = sig
                sync_ui_from_selection(context)

            if not mesh_updated:
                return
            cur_verts_co = [(v.co.x, v.co.y, v.co.z) for v in bm.verts]

            # Skip redundant regeneration if vertex coordinates haven't changed since last run
            obj_cache = _RUNTIME_CACHE.get(guide_obj.name)
            if obj_cache is not None and obj_cache.get("last_verts_co") == cur_verts_co:
                return
        except Exception:
            pass

        if fprops.auto_update:
            regenerate_feather_object(guide_obj, coords_only_hint=True)


def _append_chain_card_geometry(out_obj, target_range, xform_mat, tri_coords, wire_coords):
    """Append world-space triangles and wireframe lines for a single feather chain range."""
    if out_obj is None or out_obj.data is None or target_range is None:
        return
    _, _, _, p_start, p_end, _, _ = target_range
    out_mesh = out_obj.data
    polys = out_mesh.polygons
    verts = out_mesh.vertices
    p_end_clamped = min(p_end, len(polys))
    for pi in range(p_start, p_end_clamped):
        p_verts = [xform_mat @ verts[vi].co for vi in polys[pi].vertices]
        if len(p_verts) >= 3:
            v0 = p_verts[0]
            for k in range(1, len(p_verts) - 1):
                tri_coords.extend((v0, p_verts[k], p_verts[k + 1]))
            for k in range(len(p_verts)):
                wire_coords.extend((p_verts[k], p_verts[(k + 1) % len(p_verts)]))


def _draw_feather_directions_viewport():
    """Draw Feather Mode viewport overlays (antialiased, occlusion-aware):
    0. Feather Select: faint fill on every unpicked card (no wires)
    1. Feather Select: violet fill on picked feathers, incl. their mirror copies
    2. Feather Select: hovered feather — the only one with outline, spline and arrows
    3. Vertices / Splines: Root-to-Tip direction chevrons along visible feather chains

    Everything draws twice: depth-tested at full strength, then the hidden parts (behind the body
    or other cards) at the `behind_opacity` from the add-on preferences (Feather Mode look).
    """
    if bpy.app.background:
        return
    context = bpy.context
    guide_obj = get_feather_guide_from_context(context)
    if guide_obj is None:
        return
    fprops = guide_obj.arantools_feather
    in_fm = is_in_feather_mode(guide_obj)
    region = context.region
    if region is None:
        return

    try:
        import gpu
        from gpu_extras.batch import batch_for_shader
    except Exception:
        return

    obj_cache = _RUNTIME_CACHE.get(guide_obj.name)
    if obj_cache is None or obj_cache.get("overlay_data") is None:
        verts_co, edges_v, _, _, edge_bool, _ = _extract_guide_data(guide_obj)
        chains = _trace_chains_from_edges(len(verts_co), edges_v)
        rev_list = edge_bool["feather_reverse"]
        if obj_cache is not None:
            obj_cache["overlay_data"] = (verts_co, chains, rev_list)
    else:
        verts_co, chains, rev_list = obj_cache["overlay_data"]

    if not chains:
        return

    mw = guide_obj.matrix_world
    out_obj = fprops.output_object
    range_map = {r[0]: r for r in (obj_cache.get("chain_ranges") or [])} if obj_cache else {}
    mirror_map = {r[0]: r for r in (obj_cache.get("mirror_ranges") or [])} if obj_cache else {}
    feather_select = in_fm and get_feather_level(context) == 'FEATHER'
    look = get_feather_look(context)

    def faded(rgba, alpha):
        return (rgba[0], rgba[1], rgba[2], alpha)

    tri_shader = gpu.shader.from_builtin('UNIFORM_COLOR')
    line_shader = gpu.shader.from_builtin('POLYLINE_UNIFORM_COLOR')
    point_shader = gpu.shader.from_builtin('POINT_UNIFORM_COLOR')
    viewport_size = (float(region.width), float(region.height))

    # (kind, batch, rgba, size) — kind: 'TRIS' / 'LINES' / 'POINTS'
    items = []

    def add_tris(coords, rgba):
        if coords:
            items.append(('TRIS', batch_for_shader(tri_shader, 'TRIS', {"pos": coords}), rgba, 0.0))

    def add_lines(coords, rgba, width):
        if coords:
            items.append(('LINES', batch_for_shader(line_shader, 'LINES', {"pos": coords}), rgba, width))

    def add_points(coords, rgba, size):
        if coords:
            items.append(('POINTS', batch_for_shader(point_shader, 'POINTS', {"pos": coords}), rgba, size))

    def spline_lines(pts):
        out = []
        for i in range(len(pts) - 1):
            out.extend((pts[i], pts[i + 1]))
        return out

    def card_geometry(rng):
        tris, wires = [], []
        if out_obj is not None and out_obj.data is not None and rng is not None:
            _append_chain_card_geometry(out_obj, rng, out_obj.matrix_world, tris, wires)
        return tris, wires

    def chevrons(v_chain, e_chain, xform):
        """Direction chevron line coords + root / tip point for one feather (world space)."""
        is_rev = any(rev_list[ei] for ei in e_chain)
        ordered_v = list(reversed(v_chain)) if is_rev else list(v_chain)
        w_pts = [xform @ Vector(verts_co[vi]) for vi in ordered_v]
        lines = []
        for i in range(len(w_pts) - 1):
            p0, p1 = w_pts[i], w_pts[i + 1]
            seg = p1 - p0
            length = seg.length
            if length < 1e-5:
                continue
            d = seg / length
            mid = p0.lerp(p1, 0.6)
            up = Vector((0.0, 0.0, 1.0)) if abs(d.z) < 0.9 else Vector((1.0, 0.0, 0.0))
            side = d.cross(up).normalized() * (length * look.arrow_size * 0.875)
            wing_back = mid - d * (length * look.arrow_size * 1.125)
            lines.extend((mid, wing_back + side, mid, wing_back - side))
        return lines, w_pts[0], w_pts[-1]

    refl_world = _get_mirror_reflection_matrix(fprops) if fprops.use_mirror else None
    hov_idx = _HOVER["chain"]
    hovering = feather_select and _HOVER["guide"] == guide_obj.name and 0 <= hov_idx < len(chains)
    picked = set(get_picked_chains(guide_obj)) if feather_select else set()

    # ── 0. Feather Select: every unpicked card gets a very faint fill (no wires) ─
    if feather_select and out_obj is not None and out_obj.data is not None and obj_cache:
        fill_rgba = tuple(look.unpicked_fill_color)
        if fill_rgba[3] > 0.0:
            fill_key = (
                obj_cache.get("gen_id"),
                frozenset(picked),
                tuple(round(x, 4) for row in out_obj.matrix_world for x in row),
            )
            cached_fill = obj_cache.get("unpicked_fill")
            if cached_fill is None or cached_fill[0] != fill_key:
                tris = []
                for ranges in (obj_cache.get("chain_ranges") or [], obj_cache.get("mirror_ranges") or []):
                    for rng in ranges:
                        if rng[0] not in picked:
                            _append_chain_card_geometry(out_obj, rng, out_obj.matrix_world, tris, [])
                batch = batch_for_shader(tri_shader, 'TRIS', {"pos": tris}) if tris else None
                cached_fill = (fill_key, batch)
                obj_cache["unpicked_fill"] = cached_fill
            if cached_fill[1] is not None:
                items.append(('TRIS', cached_fill[1], fill_rgba, 0.0))

    # ── 1. Picked feather(s) in Feather Select: soft violet fill only ────────
    if picked:
        tris = []
        for c_idx in picked:
            if 0 <= c_idx < len(chains):
                for rng in (range_map.get(c_idx), mirror_map.get(c_idx)):
                    tris += card_geometry(rng)[0]
        add_tris(tris, faded(tuple(look.picked_color), look.picked_fill))

    # ── 2. Hovered feather: the only one with lines (outline, spline, arrows) ─
    if hovering:
        mirrored = _HOVER["mirrored"]
        t, w = card_geometry((mirror_map if mirrored else range_map).get(hov_idx))
        hov_rgba = tuple(look.hover_color)
        add_tris(t, faded(hov_rgba, look.hover_fill))
        if look.outline_width > 0.0:
            add_lines(w, faded(hov_rgba, hov_rgba[3] * 0.65), look.outline_width)
        xform = (refl_world @ mw) if (mirrored and refl_world is not None) else mw
        pts = [xform @ Vector(verts_co[vi]) for vi in chains[hov_idx][0]]
        add_lines(spline_lines(pts), hov_rgba, look.spline_width)
        v_chain, e_chain = chains[hov_idx]
        if fprops.show_direction_overlay and len(v_chain) >= 2 and e_chain:
            lines, root, tip = chevrons(v_chain, e_chain, xform)
            add_lines(lines, tuple(look.arrow_color), look.arrow_width)
            if look.point_size > 0.0:
                add_points([root], tuple(look.root_color), look.point_size)
                add_points([tip], tuple(look.tip_color), look.point_size)

    # ── 3. Direction chevrons on every visible feather (not while picking) ───
    if fprops.show_direction_overlay and not feather_select:
        hidden_v = set()
        if guide_obj.mode == 'EDIT':
            try:
                bm = bmesh.from_edit_mesh(guide_obj.data)
                hidden_v = {v.index for v in bm.verts if v.hide}
            except Exception:
                pass

        mw_key = (tuple(round(x, 4) for row in mw for x in row), len(hidden_v), round(look.arrow_size, 4))
        gpu_batches = obj_cache.get("gpu_batches") if obj_cache else None
        if gpu_batches is None or gpu_batches[0] != mw_key:
            line_coords = []
            root_points = []
            tip_points = []
            for v_chain, e_chain in chains:
                if len(v_chain) < 2 or not e_chain:
                    continue
                if hidden_v and any(vi in hidden_v for vi in v_chain):
                    continue
                lines, root, tip = chevrons(v_chain, e_chain, mw)
                line_coords += lines
                root_points.append(root)
                tip_points.append(tip)

            gpu_batches = (
                mw_key,
                batch_for_shader(line_shader, 'LINES', {"pos": line_coords}) if line_coords else None,
                batch_for_shader(point_shader, 'POINTS', {"pos": root_points}) if root_points else None,
                batch_for_shader(point_shader, 'POINTS', {"pos": tip_points}) if tip_points else None,
            )
            if obj_cache is not None:
                obj_cache["gpu_batches"] = gpu_batches

        _, b_lines, b_roots, b_tips = gpu_batches
        if b_lines is not None:
            items.append(('LINES', b_lines, tuple(look.arrow_color), look.arrow_width))
        if look.point_size > 0.0:
            if b_roots is not None:
                items.append(('POINTS', b_roots, tuple(look.root_color), look.point_size))
            if b_tips is not None:
                items.append(('POINTS', b_tips, tuple(look.tip_color), look.point_size))

    if not items:
        return

    def draw_pass(alpha_mult):
        for kind, batch, rgba, size in items:
            color = (rgba[0], rgba[1], rgba[2], rgba[3] * alpha_mult)
            if kind == 'LINES':
                line_shader.bind()
                line_shader.uniform_float("viewportSize", viewport_size)
                line_shader.uniform_float("lineWidth", size)
                line_shader.uniform_float("color", color)
                batch.draw(line_shader)
            elif kind == 'POINTS':
                gpu.state.point_size_set(size)
                point_shader.bind()
                point_shader.uniform_float("color", color)
                batch.draw(point_shader)
            else:
                tri_shader.bind()
                tri_shader.uniform_float("color", color)
                batch.draw(tri_shader)

    behind = float(look.behind_opacity)
    # Overlay geometry lies exactly on the cards, so equal depths flicker between the front and
    # behind passes (striping). Pull everything a hair toward the camera in clip space:
    # z' = z - bias * w  ->  constant NDC offset (~0.1 mm at 1 m with default clipping).
    depth_bias = Matrix.Identity(4)
    depth_bias[2][3] = -4e-6
    gpu.matrix.push_projection()
    gpu.matrix.load_projection_matrix(depth_bias @ gpu.matrix.get_projection_matrix())
    gpu.state.blend_set('ALPHA')
    gpu.state.depth_mask_set(False)
    try:
        gpu.state.depth_test_set('LESS_EQUAL')
        draw_pass(1.0)
        if behind > 0.0:
            gpu.state.depth_test_set('GREATER')
            draw_pass(behind)
    finally:
        gpu.matrix.pop_projection()
        gpu.state.depth_test_set('NONE')
        gpu.state.point_size_set(1.0)
        gpu.state.blend_set('NONE')


def _selected_chain_edge_values(guide_obj, attr_name):
    """Value of edge attribute `attr_name` on the first edge of each selected feather."""
    chains = _get_guide_chains_fast(guide_obj)
    sel = _get_selected_chain_indices(guide_obj)
    if not sel:
        return []
    if guide_obj.mode == 'EDIT':
        bm = bmesh.from_edit_mesh(guide_obj.data)
        bm.edges.ensure_lookup_table()
        layer = bm.edges.layers.bool.get(attr_name) or bm.edges.layers.int.get(attr_name)
        if layer is None:
            return []
        return [bm.edges[chains[ci][1][0]][layer] for ci in sel if chains[ci][1]]
    attr = guide_obj.data.attributes.get(attr_name)
    if attr is None:
        return []
    return [attr.data[chains[ci][1][0]].value for ci in sel if chains[ci][1]]


def _write_selected_chains_edge_attr(guide_obj, attr_name, attr_type, fn):
    """Set edge attribute `attr_name` on every edge of the selected feathers to `fn(old_value)`."""
    chains = _get_guide_chains_fast(guide_obj)
    sel = _get_selected_chain_indices(guide_obj)
    edge_ids = [ei for ci in sel for ei in chains[ci][1]]
    if not edge_ids:
        return 0
    if guide_obj.mode == 'EDIT':
        bm = bmesh.from_edit_mesh(guide_obj.data)
        bm.edges.ensure_lookup_table()
        layers = bm.edges.layers.bool if attr_type == 'BOOLEAN' else bm.edges.layers.int
        layer = layers.get(attr_name) or layers.new(attr_name)
        for ei in edge_ids:
            e = bm.edges[ei]
            e[layer] = fn(e[layer])
        bmesh.update_edit_mesh(guide_obj.data, loop_triangles=False, destructive=False)
    else:
        ensure_feather_guide_attributes(guide_obj.data)
        data = guide_obj.data.attributes[attr_name].data
        for ei in edge_ids:
            data[ei].value = fn(data[ei].value)
    return len(sel)


class ARANTOOLS_OT_feather_set_mirror(Operator):
    bl_idname = "arantools.feather_set_mirror"
    bl_label = "Mirror Copy On / Off"
    bl_description = "Turn the internal mirror copy on or off for the selected feather(s)"
    bl_options = {'REGISTER', 'UNDO'}

    enable: BoolProperty(default=True)

    def execute(self, context):
        guide_obj = get_feather_guide_from_context(context)
        if guide_obj is None:
            return {'CANCELLED'}
        if not _write_selected_chains_edge_attr(guide_obj, "feather_mirror", 'BOOLEAN', lambda _v: self.enable):
            self.report({'WARNING'}, "Select at least one feather first.")
            return {'CANCELLED'}
        regenerate_feather_object(guide_obj, force_full_rebuild=True)
        return {'FINISHED'}


class ARANTOOLS_OT_feather_mirror_card(Operator):
    bl_idname = "arantools.feather_mirror_card"
    bl_label = "Mirror Copy Card"
    bl_description = (
        "Card used by the selected feathers' mirror copy: Same as Original, or tick one or more "
        "cards (one = that card, several = random)"
    )
    bl_options = {'REGISTER', 'UNDO'}

    slot_index: IntProperty(default=-1, description="-1 = same card as the original, else toggle this slot")

    def execute(self, context):
        guide_obj = get_feather_guide_from_context(context)
        if guide_obj is None:
            return {'CANCELLED'}
        if self.slot_index < 0:
            fn = lambda _v: 0
        else:
            bit = 1 << max(0, min(29, self.slot_index))
            fn = lambda v: (max(0, int(v)) ^ bit)
        if not _write_selected_chains_edge_attr(guide_obj, "feather_mirror_mask", 'INT', fn):
            self.report({'WARNING'}, "Select at least one feather first.")
            return {'CANCELLED'}
        regenerate_feather_object(guide_obj, force_full_rebuild=True)
        return {'FINISHED'}


def _draw_mirror_copy_box(layout, guide_obj):
    """Per-feather mirror copy controls for the selected feather(s)."""
    fprops = guide_obj.arantools_feather
    box = layout.box()
    col = box.column(align=True)
    col.label(text="Mirror Copy", icon='MOD_MIRROR')
    if not fprops.use_mirror:
        sub = col.row(align=True)
        sub.enabled = False
        sub.label(text="Mirror is off for this Feather Object (Advanced tab)")
        return

    mir_vals = [bool(v) for v in _selected_chain_edge_values(guide_obj, "feather_mirror")]
    if not mir_vals:
        sub = col.row(align=True)
        sub.enabled = False
        sub.label(text="No feather selected", icon='INFO')
        return

    row = col.row(align=True)
    row.operator("arantools.feather_set_mirror", text="Mirrored", icon='MOD_MIRROR',
                 depress=all(mir_vals)).enable = True
    row.operator("arantools.feather_set_mirror", text="Not Mirrored", icon='X',
                 depress=not any(mir_vals)).enable = False

    if not any(mir_vals):
        return

    masks = [int(v) for v in _selected_chain_edge_values(guide_obj, "feather_mirror_mask")]
    col.separator(factor=0.4)
    col.operator("arantools.feather_mirror_card", text="Same Card as Original", icon='LINKED',
                 depress=all(m <= 0 for m in masks)).slot_index = -1
    for s_idx, mobj in get_admitted_mesh_slots(guide_obj):
        if s_idx < 0:
            continue
        bit = 1 << max(0, min(29, s_idx))
        n_on = sum(1 for m in masks if m > 0 and (m & bit))
        icon = 'CHECKBOX_HLT' if n_on == len(masks) else ('DOT' if n_on else 'CHECKBOX_DEHLT')
        col.operator("arantools.feather_mirror_card", text=mobj.name, icon=icon, emboss=False).slot_index = s_idx


# ============================================================================
# 3D Viewport Header Integration (Top-Left Mode Bar next to Blender Mode)
# ============================================================================

def _draw_feather_select_button(layout, context, guide_obj, text="Feather Select"):
    """Main toggle: highlighted only while actually picking feathers."""
    picking = is_in_feather_mode(guide_obj) and get_feather_level(context) == 'FEATHER'
    layout.operator("arantools.feather_toggle_edit", text=text, icon='STRANDS', depress=picking)


def _draw_feather_level_buttons(layout, context, guide_obj):
    """Splines | Vertices editing levels (depressed = current level)."""
    level = get_feather_level(context) if is_in_feather_mode(guide_obj) else None
    for lvl, text, icon in (
        ('EDGE', "Splines", 'CURVE_PATH'),
        ('VERTEX', "Vertices", 'VERTEXSEL'),
    ):
        op = layout.operator(
            "arantools.feather_set_level",
            text=text,
            icon=icon,
            depress=(level == lvl),
        )
        op.level = lvl


def _draw_feather_mode_view3d_header(self, context):
    """Feather Mode bar in the 3D Viewport header while a Feather Object is active."""
    guide_obj = get_feather_guide_from_context(context)
    if guide_obj is None:
        return

    fprops = guide_obj.arantools_feather
    in_fm = is_in_feather_mode(guide_obj)

    layout = self.layout
    layout.separator_spacer()

    row = layout.row(align=True)
    _draw_feather_select_button(row, context, guide_obj)

    if in_fm:
        _draw_feather_level_buttons(layout.row(align=True), context, guide_obj)
        layout.operator("object.mode_set", text="", icon='OBJECT_DATAMODE').mode = 'OBJECT'

        tools_row = layout.row(align=True)
        tools_row.prop(fprops, "show_direction_overlay", text="", icon='FORWARD', toggle=True)
        tools_row.prop(fprops, "view_mode", text="")


def _install_view3d_header():
    old_fn = bpy.app.driver_namespace.get(_VIEW3D_HEADER_DRAW_KEY)
    if old_fn is not None:
        try:
            bpy.types.VIEW3D_HT_header.remove(old_fn)
        except Exception:
            pass
    bpy.types.VIEW3D_HT_header.append(_draw_feather_mode_view3d_header)
    bpy.app.driver_namespace[_VIEW3D_HEADER_DRAW_KEY] = _draw_feather_mode_view3d_header


def _uninstall_view3d_header():
    old_fn = bpy.app.driver_namespace.pop(_VIEW3D_HEADER_DRAW_KEY, None)
    if old_fn is not None:
        try:
            bpy.types.VIEW3D_HT_header.remove(old_fn)
        except Exception:
            pass
    try:
        bpy.types.VIEW3D_HT_header.remove(_draw_feather_mode_view3d_header)
    except Exception:
        pass


# ============================================================================
# N-Panel UI Drawing (UX-Designed Layout with Controls & Advanced Tabs)
# ============================================================================

def draw_feather_cards_ui(layout, context):
    ui = context.scene.arantools_feather_ui
    guide_obj = get_feather_guide_from_context(context)

    if guide_obj is None:
        setup_box = layout.box()
        scol = setup_box.column(align=True)
        scol.label(text="Feather Object Setup", icon='STRANDS')
        scol.separator(factor=0.4)
        scol.prop(ui, "convert_source_collection", text="", icon='OUTLINER_COLLECTION')
        row = scol.row(align=True)
        row.scale_y = 1.3
        row.enabled = ui.convert_source_collection is not None
        op = row.operator(
            "arantools.feather_convert_scene",
            text="Convert Feather Collection",
            icon='MOD_PARTICLES',
        )
        if ui.convert_source_collection is not None:
            op.source_collection = ui.convert_source_collection.name
        scol.separator(factor=0.3)
        scol.operator(
            "arantools.feather_create_object",
            text="New Feather Object",
            icon='ADD',
        )
        scol.separator(factor=0.4)
        scol.label(text="Select a Feather Object in the viewport.", icon='INFO')
        return

    fprops = guide_obj.arantools_feather
    out_obj = fprops.output_object
    in_fm = is_in_feather_mode(guide_obj)
    level = get_feather_level(context) if in_fm else None
    in_feather_submode = level == 'EDGE'
    in_vertex_submode = level == 'VERTEX'

    # ── 1. Top Mode & Viewport Bar ──────────────────────────────────────────
    top_box = layout.box()
    tcol = top_box.column(align=True)

    # Feather Select (pick a feather) + Object Mode while editing
    mode_row = tcol.row(align=True)
    mode_row.scale_y = 1.35
    _draw_feather_select_button(mode_row, context, guide_obj)
    if in_fm:
        mode_row.operator("object.mode_set", text="Object Mode", icon='OBJECT_DATAMODE').mode = 'OBJECT'
    mode_row.operator("arantools.feather_regenerate", text="", icon='FILE_REFRESH')
    # Opens Preferences > Add-ons with Aran Tools expanded (Feather Mode look settings)
    mode_row.operator("preferences.addon_show", text="", icon='PREFERENCES').module = __package__

    # Editing levels: Splines | Vertices (keys 2 / 1; Feather Select = 3)
    if in_fm:
        tcol.separator(factor=0.4)
        sub_row = tcol.row(align=True)
        sub_row.scale_y = 1.3
        _draw_feather_level_buttons(sub_row, context, guide_obj)

    # Compact Viewport Display Bar (Shading dropdown + Arrows + Live toggles)
    tcol.separator(factor=0.5)
    disp_row = tcol.row(align=True)
    disp_row.prop(fprops, "view_mode", text="")
    disp_row.prop(fprops, "show_direction_overlay", text="Arrows", toggle=True, icon='FORWARD')
    disp_row.prop(fprops, "auto_update", text="Live", toggle=True, icon='AUTO')
    look = get_feather_look(context)
    if isinstance(look, ARANTOOLS_PG_FeatherLook):
        tcol.prop(look, "behind_opacity", text="Overlay Behind", slider=True)

    # ── 2. Section Tab Switcher: [ Controls ]  |  [ Advanced ] ──────────────
    tab_row = layout.row(align=True)
    tab_row.scale_y = 1.2
    tab_row.prop(ui, "panel_tab", expand=True)

    # ========================================================================
    # TAB A: CONTROLS (Contextual to Object Mode / Feathers Mode / Vertices Mode)
    # ========================================================================
    if ui.panel_tab == 'CONTROLS':

        # ── CASE 1: OBJECT MODE (Pick the Feather Cards collection) ─────────
        if not in_fm:
            adm_box = layout.box()
            acol = adm_box.column(align=True)
            acol.label(text="Feather Cards", icon='OUTLINER_COLLECTION')
            acol.separator(factor=0.3)

            crow = acol.row(align=True)
            crow.prop(fprops, "admitted_collection", text="Collection")
            crow.operator("arantools.feather_sync_admitted_collection", text="", icon='FILE_REFRESH')

            acol.separator(factor=0.4)
            acol.template_list(
                "ARANTOOLS_UL_FeatherAdmittedList",
                "",
                fprops,
                "mesh_slots",
                fprops,
                "active_slot_index",
                rows=4,
            )

            chains = _get_guide_chains_fast(guide_obj)
            n_polys = len(out_obj.data.polygons) if (out_obj and out_obj.data) else 0
            sub_info = acol.row(align=True)
            sub_info.enabled = False
            sub_info.label(text=f"{len(chains)} Feathers  •  {n_polys} Faces")

            from . import feather_overlaps  # late import: feather_overlaps imports this module
            feather_overlaps.draw_fix_overlaps_ui(layout, context, guide_obj)

        # ── CASE 2a: FEATHER SELECT (pick feathers; editing happens after the click) ──
        elif level == 'FEATHER':
            picked = get_picked_chains(guide_obj)
            pick_box = layout.box()
            p_col = pick_box.column(align=True)
            p_col.label(text="Hover a feather, click to edit it", icon='RESTRICT_SELECT_OFF')
            hint = p_col.row(align=True)
            hint.enabled = False
            if picked:
                hint.label(text=f"{len(picked)} picked")
                cc = p_col.row(align=True)
                cc.scale_y = 1.3
                cc.operator("arantools.feather_pick_confirm", text="Confirm", icon='CHECKMARK')
                cc.operator("arantools.feather_pick_cancel", text="Cancel", icon='X')
            else:
                hint.label(text="Shift+Click picks several")
            p_col.separator(factor=0.3)
            p_col.operator("arantools.feather_add_chain", text="Add Feather", icon='ADD')

        # ── CASE 2b: SPLINES MODE (Entire-Feather Controls Only) ─────────────
        elif in_feather_submode:
            sel_chains = _get_selected_chain_indices(guide_obj)
            n_sel = len(sel_chains)

            # A. Feather Tools Card
            tool_box = layout.box()
            t_col = tool_box.column(align=True)
            hdr_row = t_col.row(align=True)
            if n_sel == 0:
                hdr_row.label(
                    text="Hover a feather, click to edit it" if level == 'FEATHER' else "Select a spline",
                    icon='RESTRICT_SELECT_OFF',
                )
            elif n_sel == 1:
                hdr_row.label(text=f"1 Feather Selected (#{sel_chains[0] + 1})", icon='STRANDS')
            else:
                hdr_row.label(text=f"{n_sel} Feathers Selected", icon='STRANDS')

            t_col.separator(factor=0.3)
            r1 = t_col.row(align=True)
            r1.scale_y = 1.2
            r1.operator("mesh.select_all", text="Select All", icon='SELECT_EXTEND').action = 'SELECT'
            r1.operator("mesh.select_all", text="Clear", icon='X').action = 'DESELECT'
            r1.operator("mesh.reveal", text="Show All", icon='HIDE_OFF')

            t_col.separator(factor=0.25)
            r2 = t_col.row(align=True)
            r2.scale_y = 1.2
            r2.operator("arantools.feather_add_chain", text="Add Feather", icon='ADD')
            r2.operator("arantools.feather_duplicate_selected", text="Duplicate", icon='DUPLICATE')
            r2.operator("arantools.feather_delete_selected", text="Delete", icon='TRASH')

            # B. Feather Mesh List (Native UIList — Click = Only, Shift+Click/Checkbox = Random)
            mesh_box = layout.box()
            mcol = mesh_box.column(align=True)
            mcol.label(text="Feather Mesh", icon='OUTLINER_OB_MESH')

            fm_crow = mcol.row(align=True)
            fm_crow.prop(fprops, "admitted_collection", text="Collection")
            fm_crow.operator("arantools.feather_sync_admitted_collection", text="", icon='FILE_REFRESH')
            mcol.separator(factor=0.2)

            hint_row = mcol.row(align=True)
            hint_row.enabled = False
            hint_row.label(text="Click name = Single   •   Check / Shift = Random")

            mcol.separator(factor=0.2)
            mcol.template_list(
                "ARANTOOLS_UL_FeatherAdmittedList",
                "",
                fprops,
                "mesh_slots",
                fprops,
                "active_slot_index",
                rows=4,
            )

            # Quick Pool Actions: All | None | Re-Roll
            pool_row = mcol.row(align=True)
            pool_row.scale_y = 1.15
            op_all = pool_row.operator("arantools.feather_mesh_mask_all_none", text="All", icon='CHECKBOX_HLT')
            op_all.mode = 'ALL'
            op_none = pool_row.operator("arantools.feather_mesh_mask_all_none", text="None", icon='CHECKBOX_DEHLT')
            op_none.mode = 'NONE'
            op_reroll = pool_row.operator("arantools.feather_mesh_mask_all_none", text="Re-Roll", icon='FILE_REFRESH')
            op_reroll.mode = 'REROLL'

            # Clean 1-line summary of active mesh assignment
            and_mask, or_mask, num_sel_edges = get_selected_feathers_mask_state(guide_obj)
            admitted_slots = get_admitted_mesh_slots(guide_obj)
            checked_names = [
                mobj.name for s_idx, mobj in admitted_slots
                if s_idx >= 0 and (or_mask & (1 << s_idx)) != 0 and num_sel_edges > 0
            ]
            stat_row = mcol.row(align=True)
            if num_sel_edges == 0:
                stat_row.enabled = False
                stat_row.label(text="No feather selected", icon='INFO')
            elif len(checked_names) == 0:
                stat_row.label(text="Assigned: None (Hidden)", icon='ERROR')
            elif len(checked_names) == 1:
                stat_row.label(text=f"Assigned: Only {checked_names[0]}", icon='PINNED')
            else:
                stat_row.label(text=f"Assigned: Random ({len(checked_names)} meshes)", icon='MOD_PARTICLES')

            # C. Mirror Copy Card
            _draw_mirror_copy_box(layout, guide_obj)

            # D. Direction Card
            dir_box = layout.box()
            dcol = dir_box.column(align=True)
            dcol.label(text="Direction", icon='FORWARD')
            dcol.separator(factor=0.3)
            drow = dcol.row(align=True)
            drow.scale_y = 1.25
            drow.operator("arantools.feather_flip_direction", text="Flip Direction", icon='FILE_REFRESH')
            drow.operator("arantools.feather_flip_keep_look", text="Flip (Keep Look)", icon='ARROW_LEFTRIGHT')

            # E. Spline Shape Card
            spline_box = layout.box()
            spcol = spline_box.column(align=True)
            spcol.label(text="Spline Shape", icon='CURVE_PATH')
            spcol.separator(factor=0.3)
            spcol.prop(ui, "edge_subdiv", text="Subdivisions", slider=True)
            spcol.prop(ui, "edge_smooth_spline", text="Smooth Spline", slider=True)
            spcol.prop(ui, "edge_smooth_tilt", text="Smooth Tilt", slider=True)

        # ── CASE 3: VERTICES MODE (Per-Vertex Controls Only) ────────────────
        elif in_vertex_submode:
            vtool_box = layout.box()
            vtcol = vtool_box.column(align=True)
            vtcol.label(text="Vertex Tools", icon='VERTEXSEL')
            vtcol.separator(factor=0.3)

            vtr1 = vtcol.row(align=True)
            vtr1.scale_y = 1.2
            vtr1.operator("arantools.feather_taper_radius", text="Taper Root -> Tip", icon='IPO_LINEAR')
            vtr1.operator("arantools.feather_select_chain", text="Select Linked", icon='LINKED')
            vtcol.separator(factor=0.25)
            vtcol.operator("mesh.reveal", text="Show All", icon='HIDE_OFF')

            # The sliders' target is stated right where the sliders are, never as a lone toggle
            whole = ui.apply_whole_chain
            vert_box = layout.box()
            vcol = vert_box.column(align=True)
            vcol.label(
                text="Vertex Shape — whole feather" if whole else "Vertex Shape — selected points",
                icon='LINKED' if whole else 'VERTEXSEL',
            )
            vcol.separator(factor=0.3)
            apply_row = vcol.row(align=True)
            apply_row.label(text="Apply to:")
            apply_row.prop(ui, "apply_whole_chain", text="Selected Points", toggle=True, invert_checkbox=True)
            apply_row.prop(ui, "apply_whole_chain", text="Whole Feather", toggle=True)
            vcol.separator(factor=0.3)
            vcol.prop(ui, "vert_radius", text="Radius (Width)", slider=True)
            vcol.prop(ui, "vert_tilt", text="Tilt (Twist)", slider=True)
            vcol.prop(ui, "vert_smooth", text="Smooth", slider=True)

            # Tweaking: drag controls that move the points and leave nothing behind (no stored value)
            tw_box = layout.box()
            twcol = tw_box.column(align=True)
            twcol.label(text="Tweaking", icon='ORIENTATION_NORMAL')
            twcol.separator(factor=0.3)
            dir_row = twcol.row(align=True)
            dir_row.label(text="Direction:")
            dir_row.prop(ui, "tweak_direction", expand=True)
            twcol.separator(factor=0.3)
            push_row = twcol.row(align=True)
            push_row.scale_y = 1.3
            push_row.prop(ui, "tweak_push_out", text="Push Out", slider=True)

        if level in ('EDGE', 'VERTEX'):
            from . import feather_overlaps  # late import: feather_overlaps imports this module
            feather_overlaps.draw_fix_overlaps_ui(layout, context, guide_obj)

    # ========================================================================
    # TAB B: ADVANCED (Surface Projection, Baking, Mirror & Scene Setup)
    # ========================================================================
    else:
        # 1. Surface Follow & Push Offsets
        surf_box = layout.box()
        scol = surf_box.column(align=True)
        scol.label(text="Surface Follow & Push", icon='SNAP_FACE')
        scol.separator(factor=0.3)
        scol.prop(fprops, "surface_object", text="Body Mesh")
        scol.separator(factor=0.3)
        scol.prop(ui, "vert_pushout", text="Push Out", slider=True)
        scol.prop(ui, "vert_pushtip", text="Push Tip", slider=True)
        scol.prop(ui, "vert_followsurface", text="Follow Surface", slider=True)
        scol.prop(ui, "vert_tilttosurface", text="Tilt to Surface", slider=True)

        # 2. Bake Procedural Offsets to Vertices
        bake_box = layout.box()
        bcol = bake_box.column(align=True)
        bcol.label(text="Bake Offsets to Vertices", icon='MOD_DATA_TRANSFER')
        bcol.separator(factor=0.3)
        brow1 = bcol.row(align=True)
        brow1.scale_y = 1.25
        op_b_all = brow1.operator(
            "arantools.feather_bake_parameters",
            text="Bake All Feathers",
            icon='CHECKMARK',
        )
        op_b_all.only_selected = False
        op_b_all.bake_spline_smoothing = False

        op_b_sel = brow1.operator(
            "arantools.feather_bake_parameters",
            text="Bake Selected",
            icon='RESTRICT_SELECT_OFF',
        )
        op_b_sel.only_selected = True
        op_b_sel.bake_spline_smoothing = False

        bcol.separator(factor=0.25)
        brow2 = bcol.row(align=True)
        brow2.scale_y = 1.2
        op_b_full = brow2.operator(
            "arantools.feather_bake_parameters",
            text="Bake All + Spline Smoothing -> 0",
            icon='MOD_SMOOTH',
        )
        op_b_full.only_selected = in_fm
        op_b_full.bake_spline_smoothing = True

        # 3. Output & Mirror Settings
        out_box = layout.box()
        ocol = out_box.column(align=True)
        ocol.label(text="Generator & Mirror", icon='MODIFIER')
        ocol.separator(factor=0.3)
        ocol.prop(fprops, "material", text="Material")
        ocol.prop(fprops, "deform_axis", text="Deform Axis")
        ocol.prop(fprops, "random_seed", text="Global Seed")
        mrow = ocol.row(align=True)
        mrow.prop(fprops, "use_mirror", text="Mirror")
        sub = mrow.row(align=True)
        sub.enabled = fprops.use_mirror
        sub.prop(fprops, "mirror_object", text="")
        ocol.prop(fprops, "random_flip", text="Random Flip", slider=True)
        if fprops.use_mirror and out_obj is not None and any(
            m.type == 'MIRROR' and m.show_viewport for m in out_obj.modifiers
        ):
            warn = ocol.box()
            warn.label(text="The generated mesh also has a Mirror modifier", icon='ERROR')
            warn.label(text="Mirror is now built in; remove or disable that modifier")
        ocol.separator(factor=0.3)
        ocol.prop(ui, "edge_reverse", text="Reversed Direction Flag")

        # 4. Scene Setup & Conversion
        conv_box = layout.box()
        ccol = conv_box.column(align=True)
        ccol.label(text="Scene Setup", icon='COLLECTION_NEW')
        ccol.separator(factor=0.3)
        ccol.prop(ui, "convert_source_collection", text="", icon='OUTLINER_COLLECTION')
        crow = ccol.row(align=True)
        crow.enabled = ui.convert_source_collection is not None
        cop = crow.operator(
            "arantools.feather_convert_scene",
            text="Convert Feather Collection",
            icon='MOD_PARTICLES',
        )
        if ui.convert_source_collection is not None:
            cop.source_collection = ui.convert_source_collection.name
        ccol.operator(
            "arantools.feather_create_object",
            text="New Custom Feather Object",
            icon='ADD',
        )


# ============================================================================
# Registration
# ============================================================================

classes = (
    ARANTOOLS_PG_FeatherLook,
    ARANTOOLS_OT_feather_look_reset,
    ARANTOOLS_PG_FeatherMeshSlot,
    ARANTOOLS_PG_FeatherObject,
    ARANTOOLS_PG_FeatherSceneUI,
    ARANTOOLS_UL_FeatherAdmittedList,
    ARANTOOLS_OT_feather_convert_scene,
    ARANTOOLS_OT_feather_bake_parameters,
    ARANTOOLS_OT_feather_toggle_edit,
    ARANTOOLS_OT_feather_set_level,
    ARANTOOLS_OT_feather_pick,
    ARANTOOLS_OT_feather_pick_confirm,
    ARANTOOLS_OT_feather_pick_cancel,
    ARANTOOLS_OT_feather_hover,
    ARANTOOLS_OT_feather_set_mirror,
    ARANTOOLS_OT_feather_mirror_card,
    ARANTOOLS_OT_feather_duplicate_selected,
    ARANTOOLS_OT_feather_delete_selected,
    ARANTOOLS_OT_feather_taper_radius,
    ARANTOOLS_OT_feather_regenerate,
    ARANTOOLS_OT_feather_flip_direction,
    ARANTOOLS_OT_feather_flip_keep_look,
    ARANTOOLS_OT_feather_assign_slot,
    ARANTOOLS_OT_feather_mesh_mask_all_none,
    ARANTOOLS_OT_feather_sync_admitted_collection,
    ARANTOOLS_OT_feather_select_chain,
    ARANTOOLS_OT_feather_adjust_attr,
    ARANTOOLS_OT_feather_push_listener,
    ARANTOOLS_OT_feather_add_chain,
    ARANTOOLS_OT_feather_create_object,
)


def _register_feather_keymaps():
    """Feather Mode shortcuts: Ctrl+F = Feather Select; in Edit Mode 1 = Vertices, 2 = Splines, 3 = Feather Select.

    They sit in the generic "Mesh" keymap but pass through on non-feather meshes, so the native
    1/2/3 select-mode keys keep working everywhere else.
    """
    _unregister_feather_keymaps()
    wm = getattr(bpy.context, "window_manager", None)
    if wm is None or wm.keyconfigs.addon is None:
        return
    km = wm.keyconfigs.addon.keymaps.new(name="Mesh", space_type='EMPTY')
    for key, level in (('ONE', 'VERTEX'), ('TWO', 'EDGE'), ('THREE', 'FEATHER')):
        kmi = km.keymap_items.new("arantools.feather_set_level", key, 'PRESS', head=True)
        kmi.properties.level = level
        _ADDON_KEYMAPS.append((km, kmi))

    # Ctrl+F: Feather Select from Object or Edit Mode (toggles back to Object Mode while picking).
    # "Mesh" beats Edit Mode's own Ctrl+F (Face menu); "3D View" covers Object Mode.
    km_view = wm.keyconfigs.addon.keymaps.new(name="3D View", space_type='VIEW_3D')
    for keymap in (km, km_view):
        kmi = keymap.keymap_items.new("arantools.feather_toggle_edit", 'F', 'PRESS', ctrl=True, head=True)
        _ADDON_KEYMAPS.append((keymap, kmi))


def _unregister_feather_keymaps():
    for km, kmi in reversed(_ADDON_KEYMAPS):
        try:
            km.keymap_items.remove(kmi)
        except Exception:
            pass
    _ADDON_KEYMAPS.clear()


def _auto_setup_open_scene_timer():
    """Run on the next main-thread tick after addon reload/register to update the live scene & UI."""
    try:
        scene = getattr(bpy.context, "scene", None)
        if scene is not None:
            try:
                scene.arantools_active_tab = 'HAIRCARDS'
                scene.arantools_open_feather_cards = True
            except Exception:
                pass
            # Reload never converts, creates, or regenerates feather data — runtime caches rebuild
            # lazily on first use (draw handler / picker / edit).

        wm = getattr(bpy.context, "window_manager", None)
        if wm is not None:
            for win in wm.windows:
                for area in win.screen.areas:
                    area.tag_redraw()
    except Exception as exc:
        print(f"[AranTools] Post-register UI refresh warning: {exc}")
    return None


def register():
    global _DRAW_HANDLER
    for cls in classes:
        try:
            bpy.utils.register_class(cls)
        except Exception:
            try:
                bpy.utils.unregister_class(cls)
                bpy.utils.register_class(cls)
            except Exception:
                pass

    bpy.types.Object.arantools_feather = PointerProperty(type=ARANTOOLS_PG_FeatherObject)
    bpy.types.Scene.arantools_feather_ui = PointerProperty(type=ARANTOOLS_PG_FeatherSceneUI)

    if _feather_depsgraph_update_post not in bpy.app.handlers.depsgraph_update_post:
        bpy.app.handlers.depsgraph_update_post.append(_feather_depsgraph_update_post)
    for handlers in (bpy.app.handlers.undo_post, bpy.app.handlers.redo_post):
        if _feather_undo_post not in handlers:
            handlers.append(_feather_undo_post)

    if not bpy.app.background and _DRAW_HANDLER is None:
        try:
            _DRAW_HANDLER = bpy.types.SpaceView3D.draw_handler_add(
                _draw_feather_directions_viewport, (), 'WINDOW', 'POST_VIEW'
            )
        except Exception:
            _DRAW_HANDLER = None

    if not bpy.app.background:
        try:
            bpy.utils.register_tool(
                ARANTOOLS_TL_feather_select,
                after={"builtin.select_lasso"},
                separator=True,
                group=False,
            )
        except Exception as exc:
            print(f"[AranTools] Feather Select tool registration warning: {exc}")

    _install_view3d_header()
    _install_pick_statusbar()
    _register_feather_keymaps()

    try:
        bpy.app.timers.register(_auto_setup_open_scene_timer, first_interval=0.05)
    except Exception:
        pass


def unregister():
    global _DRAW_HANDLER
    _unregister_feather_keymaps()
    try:
        bpy.utils.unregister_tool(ARANTOOLS_TL_feather_select)
    except Exception:
        pass
    _uninstall_view3d_header()
    _uninstall_pick_statusbar()
    if _DRAW_HANDLER is not None:
        try:
            bpy.types.SpaceView3D.draw_handler_remove(_DRAW_HANDLER, 'WINDOW')
        except Exception:
            pass
        _DRAW_HANDLER = None

    if _feather_depsgraph_update_post in bpy.app.handlers.depsgraph_update_post:
        bpy.app.handlers.depsgraph_update_post.remove(_feather_depsgraph_update_post)
    for handlers in (bpy.app.handlers.undo_post, bpy.app.handlers.redo_post):
        if _feather_undo_post in handlers:
            handlers.remove(_feather_undo_post)

    if hasattr(bpy.types.Scene, "arantools_feather_ui"):
        try:
            del bpy.types.Scene.arantools_feather_ui
        except Exception:
            pass
    if hasattr(bpy.types.Object, "arantools_feather"):
        try:
            del bpy.types.Object.arantools_feather
        except Exception:
            pass

    for cls in reversed(classes):
        try:
            bpy.utils.unregister_class(cls)
        except Exception:
            pass
