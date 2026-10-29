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
import numpy as np
import bpy
import bmesh
from bpy_extras import view3d_utils
from bpy.props import (
    BoolProperty,
    CollectionProperty,
    EnumProperty,
    FloatProperty,
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
_FEATHER_PICK_MODAL_ACTIVE = False
# Bumped on every picker invoke; older modal instances see a stale generation and exit themselves.
_FEATHER_PICK_MODAL_GEN = 0
_HOVERED_CHAIN_INDEX = -1
_HOVERED_GUIDE_NAME = ""

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


def _evaluate_single_chain(
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
    bcache,
    pushout_arr=None,
    pushtip_arr=None,
    follow_arr=None,
    tiltsurf_arr=None,
    surf_bvh=None,
):
    """Evaluate a single feather spline and return its flat `[x0, y0, z0, x1, y1, z1, ...]` coords."""
    res_pts, c_tan, c_nor, c_rad, _, _, _ = _evaluate_chain_50pts(
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

    # Deform Match Curve for this feather's assigned Mesh
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
        fx = pos.x + rad * (sv1 * nor.x + sv2 * binor.x)
        fy = pos.y + rad * (sv1 * nor.y + sv2 * binor.y)
        fz = pos.z + rad * (sv1 * nor.z + sv2 * binor.z)
        out_flat.extend((fx, fy, fz))

    return out_flat


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

    sharp_attr = bm_data.attributes.get("sharp_edge")
    if sharp_attr is not None:
        sharp_edges = [bool(d.value) for d in sharp_attr.data]
    else:
        sharp_edges = [bool(e.use_sharp) for e in bm_data.edges]

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
        "edges": edges,
        "polys": polys,
        "sharp_edges": sharp_edges,
        "loop_uvs": loop_uvs,
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
            },
        )
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
        flat_all_verts = []
        chain_slot_list = []
        chain_ranges = []
        cur_v_ofs = 0
        cur_p_ofs = 0
        cur_l_ofs = 0

        rad_list = pt_layers["radius"]
        tilt_list = pt_layers["tilt"]
        smooth_list = pt_layers["smooth"]
        pushout_list = pt_layers["pushout"]
        pushtip_list = pt_layers["pushtip"]
        follow_list = pt_layers["followsurface"]
        tiltsurf_list = pt_layers["tilttosurface"]
        slot_list = edge_int["feather_mesh_slot"]
        mask_list = edge_int["feather_mesh_mask"]
        seed_list = edge_int["feather_seed"]
        subdiv_list = edge_int["feather_subdiv"]
        st_list = edge_int["feather_smooth_tilt"]
        ss_list = edge_int["feather_smooth_spline"]
        rev_list = edge_bool["feather_reverse"]
        global_seed = int(getattr(fprops, "random_seed", 1))

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
            elif mask_val > 0:
                enabled_slots = [
                    s_i
                    for s_i in range(len(base_caches))
                    if (mask_val & (1 << s_i)) and base_caches[s_i] is not None
                ]
                if len(enabled_slots) == 1:
                    slot_idx = enabled_slots[0]
                elif len(enabled_slots) > 1:
                    slot_idx = _pick_random_slot_from_mask(
                        enabled_slots, c_idx, seed_list[e0], global_seed
                    )
                else:
                    slot_idx = slot_list[e0]
            else:
                slot_idx = slot_list[e0]

            if slot_idx < 0 or slot_idx >= len(base_caches):
                slot_idx = 0
            bcache = base_caches[slot_idx] if base_caches else None
            if bcache is None:
                continue

            chain_slot_list.append(slot_idx)
            chain_ranges.append((
                c_idx,
                cur_v_ofs,
                cur_v_ofs + bcache["n_verts"],
                cur_p_ofs,
                cur_p_ofs + bcache["n_polys"],
                cur_l_ofs,
                cur_l_ofs + bcache["n_loops"],
            ))
            cur_v_ofs += bcache["n_verts"]
            cur_p_ofs += bcache["n_polys"]
            cur_l_ofs += bcache["n_loops"]

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
                c_flat = cached[1]
            else:
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

                c_flat = _evaluate_single_chain(
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
                    bcache,
                    pushout_arr=pushout_arr,
                    pushtip_arr=pushtip_arr,
                    follow_arr=follow_arr,
                    tiltsurf_arr=tiltsurf_arr,
                    surf_bvh=surf_bvh,
                )
            new_chain_cache[c_idx] = (c_key, c_flat)
            flat_all_verts.extend(c_flat)

        obj_cache["chain_cache"] = new_chain_cache
        obj_cache["chain_ranges"] = chain_ranges
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
        topo_sig = (tuple(chain_slot_list), tuple(base_sigs), mat_name, view_mode)

        out_mesh = out_obj.data
        total_verts = len(flat_all_verts) // 3

        # ── FAST PATH: Topology is unchanged -> update vertex positions in-place! ──
        if (
            not force_full_rebuild
            and obj_cache["topo_sig"] == topo_sig
            and len(out_mesh.vertices) == total_verts
            and total_verts > 0
        ):
            out_mesh.vertices.foreach_set("co", flat_all_verts)
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

        for slot_idx in chain_slot_list:
            bcache = base_caches[slot_idx]
            for e_u, e_v in bcache["edges"]:
                all_edges.append((e_u + v_offset, e_v + v_offset))
            for poly in bcache["polys"]:
                all_polys.append(tuple(vi + v_offset for vi in poly))
            all_sharp.extend(bcache["sharp_edges"])
            all_uvs.extend(bcache["loop_uvs"])
            v_offset += bcache["n_verts"]

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
                    for c_idx, _, _, _, _, l_start, l_end in chain_ranges:
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

        # Configure standard Hops_Mirror modifier (NOT Geometry Nodes)
        if fprops.use_mirror:
            mmod = out_obj.modifiers.get("Hops_Mirror")
            if mmod is None or mmod.type != 'MIRROR':
                mmod = out_obj.modifiers.new(name="Hops_Mirror", type='MIRROR')
            mmod.use_axis = (True, False, False)
            mmod.use_bisect_axis = (True, False, False)
            mmod.use_bisect_flip_axis = (False, False, False)
            mmod.mirror_object = fprops.mirror_object
            mmod.use_mirror_merge = True
            mmod.merge_threshold = 0.001
            mmod.show_viewport = True
            mmod.show_render = True
        else:
            mmod = out_obj.modifiers.get("Hops_Mirror")
            if mmod is not None:
                mmod.show_viewport = False
                mmod.show_render = False

        for o in (guide_obj, out_obj):
            for mod in list(o.modifiers):
                if mod.type == 'NODES':
                    o.modifiers.remove(mod)

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


def ensure_default_admitted_collection(guide_obj):
    """Create a per-guide admitted collection if `admitted_collection` is unset.

    Each Feather Object gets its own collection (never shared by name between guides), seeded only
    from that guide's existing mesh slots.
    """
    if guide_obj is None:
        return
    fprops = guide_obj.arantools_feather
    if fprops.admitted_collection is None:
        coll = bpy.data.collections.new(f"{guide_obj.name}_Admitted")
        if bpy.context.scene and coll.name not in bpy.context.scene.collection.children:
            bpy.context.scene.collection.children.link(coll)
        # Hide the source card collection in the viewport so origin cards stay hidden
        try:
            vl = bpy.context.view_layer
            if vl and coll.name in vl.layer_collection.children:
                vl.layer_collection.children[coll.name].hide_viewport = True
        except Exception:
            pass

        seed_meshes = []
        for s in fprops.mesh_slots:
            if s.mesh_obj is not None and s.mesh_obj not in seed_meshes:
                seed_meshes.append(s.mesh_obj)

        for mobj in seed_meshes:
            if mobj.name not in coll.objects:
                coll.objects.link(mobj)

        fprops.admitted_collection = coll

    sync_admitted_collection_to_slots(guide_obj)


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
    If not in Feather Mode and no edges are selected, returns all edge indices.
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
        if getattr(guide_obj.arantools_feather, "in_feather_mode", False):
            # Check cached selected_chains if available
            obj_cache = _RUNTIME_CACHE.get(guide_obj.name)
            sel_chains = obj_cache.get("selected_chains") if obj_cache else None
            if sel_chains:
                chains = _get_guide_chains_fast(guide_obj)
                out_e = []
                for ci in sel_chains:
                    if 0 <= ci < len(chains):
                        out_e.extend(chains[ci][1])
                return out_e
            return []
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
            if not sel_v and not sel_e:
                # Fall back to unhidden chains if isolated
                unhidden_e = {e.index for e in bm.edges if not e.hide}
                if 0 < len(unhidden_e) < len(bm.edges):
                    sel_e = unhidden_e
            return [
                ci for ci, (vc, ec) in enumerate(chains)
                if any(vi in sel_v for vi in vc) or any(ei in sel_e for ei in ec)
            ]
        except Exception:
            return []
    else:
        obj_cache = _RUNTIME_CACHE.get(guide_obj.name)
        if obj_cache is not None and obj_cache.get("selected_chains") is not None:
            valid = [ci for ci in sorted(obj_cache["selected_chains"]) if 0 <= ci < len(chains)]
            if valid:
                return valid
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
            # Object Mode / Feathers Submode: read from the first selected edge & vertex
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
                if not target_v and not guide_obj.arantools_feather.in_feather_mode:
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
    """When clicking a row in the Admitted Feather Meshes UIList in Feather Mode, assign it as ONLY mesh."""
    if _IS_GENERATING or _IS_SYNCING_UI:
        return
    guide_obj = _resolve_owner_guide_obj(self, context)
    if guide_obj is None:
        return
    fprops = guide_obj.arantools_feather
    if not fprops.in_feather_mode:
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
    in_feather_mode: BoolProperty(
        name="Feather Mode",
        description="Whether this Feather Object is currently in the interactive Feather Mode",
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
        name="Admitted Feathers Collection",
        description="Collection of feather card meshes admitted for selection on this Feather Object (authored in Object Mode)",
        type=bpy.types.Collection,
        update=_update_admitted_collection,
    )
    random_seed: IntProperty(
        name="Random Seed",
        description="Global random seed used when a feather has multiple admitted meshes checked",
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
    feather_submode: EnumProperty(
        name="Feather Sub-Mode",
        description="Switch between Feathers (whole-feather selection), Splines, and Vertices",
        items=[
            ('FEATHER_SELECT', "Feathers", "Select whole feathers in the viewport and edit feather-level settings (Shortcut: 3)"),
            ('EDGE', "Splines", "Edit feather guide splines (Shortcut: 2)"),
            ('VERTEX', "Vertices", "Edit individual feather vertices, radius, tilt & smoothing (Shortcut: 1)"),
        ],
        default='FEATHER_SELECT',
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
        default=True,
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
        description="Apply vertex/edge edits to the entire connected feather chain",
        default=False,
    )
    show_extra_attrs: BoolProperty(
        name="Extra Vertex Attributes",
        default=True,
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
        name="Tilt (rad)",
        description="Feather twist angle in radians at the selected vertex(es)",
        default=0.0,
        soft_min=-6.283,
        soft_max=6.283,
        step=5,
        precision=3,
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

    ensure_default_admitted_collection(guide_obj)

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
        if not self.source_collection and context.collection is not None:
            self.source_collection = context.collection.name
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


_HOVERED_MIRRORED = False
_VIEW3D_HEADER_DRAW_KEY = "_arantools_feather_view3d_header"


def _get_guide_chains_fast(guide_obj):
    """Return `chains` `[(v_chain, e_chain), ...]` for `guide_obj` using cache if available."""
    obj_cache = _RUNTIME_CACHE.get(guide_obj.name)
    if obj_cache is not None and obj_cache.get("guide_meta") is not None:
        return obj_cache["guide_meta"]["chains"]
    verts_co, edges_v, _, _, _, _ = _extract_guide_data(guide_obj)
    return _trace_chains_from_edges(len(verts_co), edges_v)


def _set_selected_chain_indices(guide_obj, chain_indices, isolate_in_edit=True, context=None):
    """Unified selection setter across Object Mode (`Feathers` sub-mode) and Edit Mode (`Vertices`/`Splines`)."""
    if guide_obj is None or guide_obj.data is None:
        return
    chains = _get_guide_chains_fast(guide_obj)
    valid_chains = [ci for ci in sorted(set(chain_indices)) if 0 <= ci < len(chains)]

    obj_cache = _RUNTIME_CACHE.get(guide_obj.name)
    if obj_cache is not None:
        obj_cache["selected_chains"] = set(valid_chains)
        obj_cache["gpu_batches"] = None

    keep_v = set()
    keep_e = set()
    first_v_idx = None
    first_e_idx = None
    for c_idx in valid_chains:
        v_chain, e_chain = chains[c_idx]
        keep_v.update(v_chain)
        keep_e.update(e_chain)
        if first_v_idx is None and v_chain:
            first_v_idx = v_chain[0]
        if first_e_idx is None and e_chain:
            first_e_idx = e_chain[0]

    should_isolate = isolate_in_edit and (0 < len(valid_chains) < len(chains))

    if guide_obj.mode == 'EDIT':
        bm = bmesh.from_edit_mesh(guide_obj.data)
        bm.verts.ensure_lookup_table()
        bm.edges.ensure_lookup_table()

        for v in bm.verts:
            if v.index in keep_v:
                v.hide = False
                v.select = True
            else:
                v.select = False
                v.hide = should_isolate

        for e in bm.edges:
            if e.index in keep_e:
                e.hide = False
                e.select = True
            else:
                e.select = False
                e.hide = should_isolate

        bm.select_history.clear()
        if first_v_idx is not None and 0 <= first_v_idx < len(bm.verts):
            bm.select_history.add(bm.verts[first_v_idx])
        elif first_e_idx is not None and 0 <= first_e_idx < len(bm.edges):
            bm.select_history.add(bm.edges[first_e_idx])

        bm.select_flush_mode()
        bmesh.update_edit_mesh(guide_obj.data, loop_triangles=False, destructive=False)
    else:
        # In Object Mode (`Feathers` sub-mode): update Mesh vertex/edge select flags without Edit Mode flicker!
        mesh = guide_obj.data
        for v in mesh.vertices:
            v.hide = False
            v.select = (v.index in keep_v)
        for e in mesh.edges:
            e.hide = False
            e.select = (e.index in keep_e)

    if context is not None:
        sync_ui_from_selection(context)


def _unhide_all_feathers(guide_obj, select_all=False):
    """Unhide all vertices and edges in Edit Mode or Object Mode on `guide_obj`."""
    if guide_obj is None or guide_obj.data is None:
        return
    if guide_obj.mode == 'EDIT':
        bm = bmesh.from_edit_mesh(guide_obj.data)
        bm.verts.ensure_lookup_table()
        bm.edges.ensure_lookup_table()
        for v in bm.verts:
            v.hide = False
            if select_all:
                v.select = True
        for e in bm.edges:
            e.hide = False
            if select_all:
                e.select = True
        bm.select_flush_mode()
        bmesh.update_edit_mesh(guide_obj.data, loop_triangles=False, destructive=False)
    else:
        mesh = guide_obj.data
        for v in mesh.vertices:
            v.hide = False
            if select_all:
                v.select = True
        for e in mesh.edges:
            e.hide = False
            if select_all:
                e.select = True
        if select_all:
            chains = _get_guide_chains_fast(guide_obj)
            obj_cache = _RUNTIME_CACHE.get(guide_obj.name)
            if obj_cache is not None:
                obj_cache["selected_chains"] = set(range(len(chains)))

    obj_cache = _RUNTIME_CACHE.get(guide_obj.name)
    if obj_cache is not None:
        obj_cache["gpu_batches"] = None


def _isolate_feather_chains(guide_obj, chain_indices, context=None):
    """Select `chain_indices` and isolate them when in Edit Mode."""
    _set_selected_chain_indices(guide_obj, chain_indices, isolate_in_edit=True, context=context)


def _find_view3d_region_at_mouse(context, event):
    """Return `(area, window_region, rv3d, (local_mx, local_my))` for the 3D Viewport under the cursor."""
    screen = getattr(context, "screen", None)
    if screen is None:
        return None, None, None, None
    mx, my = event.mouse_x, event.mouse_y
    for area in screen.areas:
        if area.type != 'VIEW_3D':
            continue
        space = area.spaces.active
        rv3d = getattr(space, "region_3d", None)
        if rv3d is None:
            continue
        # Ignore clicks inside N-panel (UI), Header, or Toolbar (TOOLS) regions so UI buttons always work!
        for reg in area.regions:
            if reg.type in {'UI', 'TOOLS', 'HEADER', 'TOOL_HEADER', 'HUD'}:
                if reg.x <= mx <= reg.x + reg.width and reg.y <= my <= reg.y + reg.height:
                    return None, None, None, None
        for reg in area.regions:
            if reg.type == 'WINDOW':
                if reg.x <= mx <= reg.x + reg.width and reg.y <= my <= reg.y + reg.height:
                    return area, reg, rv3d, (mx - reg.x, my - reg.y)
    return None, None, None, None


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


def _pick_feather_chain_at_mouse(context, event, guide_obj):
    """Find the feather chain index `c_idx` under the mouse cursor using depth-occluded 3D raycast
    against the generated feather cards (`out_obj` + mirrored half), with depth-culled spline fallback.
    """
    global _HOVERED_MIRRORED
    _, region, rv3d, coord = _find_view3d_region_at_mouse(context, event)
    if region is None or rv3d is None or guide_obj is None:
        _HOVERED_MIRRORED = False
        return -1

    fprops = guide_obj.arantools_feather
    out_obj = fprops.output_object
    obj_cache = _RUNTIME_CACHE.get(guide_obj.name)
    if obj_cache is None or obj_cache.get("chain_ranges") is None:
        regenerate_feather_object(guide_obj)
        obj_cache = _RUNTIME_CACHE.get(guide_obj.name)

    chain_ranges = obj_cache.get("chain_ranges", []) if obj_cache else []

    ray_origin = view3d_utils.region_2d_to_origin_3d(region, rv3d, coord)
    ray_dir = view3d_utils.region_2d_to_vector_3d(region, rv3d, coord).normalized()

    # 0. Depth Occlusion Check against the Bird Body (`surface_object`) so back-facing feathers
    #    on the far side of the bird are NEVER picked through the body!
    max_depth = 1e30
    surf_obj = fprops.surface_object
    if surf_obj is not None and surf_obj.type == 'MESH' and surf_obj.data is not None and len(surf_obj.data.polygons) > 0:
        s_inv = surf_obj.matrix_world.inverted_safe()
        s_orig = s_inv @ ray_origin
        s_dir = (s_inv.to_3x3() @ ray_dir).normalized()
        s_hit, s_loc, _, _ = surf_obj.ray_cast(s_orig, s_dir)
        if s_hit:
            body_dist = (surf_obj.matrix_world @ s_loc - ray_origin).length
            # Allow a small tolerance (0.04m) for feather roots slightly embedded in the body skin
            max_depth = body_dist + 0.04

    # 1. 3D Raycast against the generated 3D feather cards (`out_obj`)
    if out_obj is not None and out_obj.data is not None and len(out_obj.data.polygons) > 0 and chain_ranges:
        mw_out = out_obj.matrix_world
        mw_inv = mw_out.inverted_safe()
        loc_orig = mw_inv @ ray_origin
        loc_dir = (mw_inv.to_3x3() @ ray_dir).normalized()

        best_dist = max_depth
        best_poly = -1
        best_is_mirrored = False

        hit, loc, _, poly_idx = out_obj.ray_cast(loc_orig, loc_dir)
        if hit and poly_idx >= 0:
            d = (mw_out @ loc - ray_origin).length
            if d <= best_dist:
                best_dist = d
                best_poly = poly_idx
                best_is_mirrored = False

        # Test mirrored half using the exact reflection matrix of `mirror_object`
        if fprops.use_mirror:
            refl_mat = _get_mirror_reflection_matrix(fprops)
            mir_orig_w = refl_mat @ ray_origin
            mir_dir_w = (refl_mat.to_3x3() @ ray_dir).normalized()
            m_loc_orig = mw_inv @ mir_orig_w
            m_loc_dir = (mw_inv.to_3x3() @ mir_dir_w).normalized()
            m_hit, m_loc, _, m_poly_idx = out_obj.ray_cast(m_loc_orig, m_loc_dir)
            if m_hit and m_poly_idx >= 0:
                w_hit_mirrored = refl_mat @ (mw_out @ m_loc)
                d = (w_hit_mirrored - ray_origin).length
                if d < best_dist:
                    best_dist = d
                    best_poly = m_poly_idx
                    best_is_mirrored = True

        if best_poly >= 0:
            for c_idx, _, _, p_start, p_end, _, _ in chain_ranges:
                if p_start <= best_poly < p_end:
                    _HOVERED_MIRRORED = best_is_mirrored
                    return c_idx

    # 2. Fallback: Depth-culled 2D screen-space distance to guide spline segments (within 22 px)
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
    best_mir = False

    for c_idx, (v_chain, _) in enumerate(chains):
        for is_mir in ((False, True) if refl_mat is not None else (False,)):
            pts_2d = []
            for vi in v_chain:
                w_pt = mw @ Vector(verts_co[vi])
                if is_mir:
                    w_pt = refl_mat @ w_pt
                # Reject spline points occluded behind the bird body!
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
                    best_mir = is_mir

    _HOVERED_MIRRORED = best_mir
    return best_c_idx


class ARANTOOLS_OT_feather_toggle_edit(Operator):
    bl_idname = "arantools.feather_toggle_edit"
    bl_label = "Toggle Feather Mode"
    bl_description = "Enter or exit the dedicated Feather Mode (Feathers / Splines / Vertices)"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        global _FEATHER_PICK_MODAL_ACTIVE, _HOVERED_CHAIN_INDEX
        guide_obj = get_feather_guide_from_context(context)
        if guide_obj is None:
            self.report({'WARNING'}, "Select a Feather Object first.")
            return {'CANCELLED'}

        fprops = guide_obj.arantools_feather
        out_obj = fprops.output_object

        if fprops.in_feather_mode:
            # Exit Feather Mode -> return to standard Object Mode
            fprops.in_feather_mode = False
            _FEATHER_PICK_MODAL_ACTIVE = False
            _HOVERED_CHAIN_INDEX = -1
            if guide_obj.mode == 'EDIT':
                _unhide_all_feathers(guide_obj, select_all=False)
                bpy.ops.object.mode_set(mode='OBJECT')
            if out_obj is not None:
                out_obj.hide_select = False
            for area in context.screen.areas if context.screen else []:
                area.tag_redraw()
        else:
            # Enter Feather Mode -> default to Feathers (`FEATHER_SELECT`) submode if not already set
            fprops.in_feather_mode = True
            target_sub = fprops.feather_submode or 'FEATHER_SELECT'
            bpy.ops.arantools.feather_set_submode('INVOKE_DEFAULT', submode=target_sub)

        return {'FINISHED'}


class ARANTOOLS_OT_feather_show_all(Operator):
    bl_idname = "arantools.feather_show_all"
    bl_label = "Show All Feathers"
    bl_description = "Unhide all feathers so every feather spline is visible (Shortcut: Alt+H)"
    bl_options = {'REGISTER', 'UNDO'}

    def invoke(self, context, event):
        guide_obj = get_feather_guide_from_context(context)
        if guide_obj is None or not guide_obj.arantools_feather.in_feather_mode:
            return {'PASS_THROUGH'}
        return self.execute(context)

    def execute(self, context):
        guide_obj = get_feather_guide_from_context(context)
        if guide_obj is None:
            return {'CANCELLED'}
        _unhide_all_feathers(guide_obj, select_all=False)
        for area in context.screen.areas if context.screen else []:
            area.tag_redraw()
        self.report({'INFO'}, "All feathers shown.")
        return {'FINISHED'}


class ARANTOOLS_OT_feather_select_modal(Operator):
    bl_idname = "arantools.feather_select_modal"
    bl_label = "Feather Mode — Feathers Picker"
    bl_description = (
        "Hover over any feather in the 3D Viewport to highlight it, Left-Click to select "
        "(Shift+Click to select multiple feathers, 1 = Edit Vertices, 2 = Edit Splines)"
    )
    bl_options = {'REGISTER'}

    def execute(self, context):
        return self.invoke(context, None)

    def invoke(self, context, event):
        global _FEATHER_PICK_MODAL_ACTIVE, _FEATHER_PICK_MODAL_GEN, _HOVERED_CHAIN_INDEX, _HOVERED_GUIDE_NAME
        guide_obj = get_feather_guide_from_context(context)
        if guide_obj is None:
            return {'CANCELLED'}

        fprops = guide_obj.arantools_feather
        fprops.in_feather_mode = True
        fprops.feather_submode = 'FEATHER_SELECT'

        # Preserve selection from Edit Mode before exiting Edit Mode to eliminate Edit Mode cage flicker!
        cur_sel = _get_selected_chain_indices(guide_obj)
        if guide_obj.mode == 'EDIT':
            _unhide_all_feathers(guide_obj, select_all=False)
            bpy.ops.object.mode_set(mode='OBJECT')

        if context.mode != 'OBJECT':
            bpy.ops.object.mode_set(mode='OBJECT')

        # Lock selection on output mesh while in Feather Mode so clicking feathers never selects `out_obj`
        out_obj = fprops.output_object
        if out_obj is not None:
            out_obj.hide_select = True
            out_obj.select_set(False)

        guide_obj.hide_set(False)
        guide_obj.select_set(True)
        context.view_layer.objects.active = guide_obj

        if cur_sel:
            _set_selected_chain_indices(guide_obj, cur_sel, isolate_in_edit=False, context=context)

        _HOVERED_GUIDE_NAME = guide_obj.name
        _HOVERED_CHAIN_INDEX = -1

        if bpy.app.background or event is None or context.window is None:
            return {'FINISHED'}

        # Always start a fresh picker: a stale `_FEATHER_PICK_MODAL_ACTIVE` (e.g. after a file load killed
        # the old handler) must never block it, and any older instance retires itself via the generation.
        _FEATHER_PICK_MODAL_GEN += 1
        self._gen = _FEATHER_PICK_MODAL_GEN
        _FEATHER_PICK_MODAL_ACTIVE = True
        context.window_manager.modal_handler_add(self)
        for area in context.screen.areas if context.screen else []:
            area.tag_redraw()
        return {'RUNNING_MODAL'}

    def _finish(self, context):
        global _FEATHER_PICK_MODAL_ACTIVE, _HOVERED_CHAIN_INDEX
        _FEATHER_PICK_MODAL_ACTIVE = False
        _HOVERED_CHAIN_INDEX = -1
        for area in context.screen.areas if context.screen else []:
            area.tag_redraw()

    def modal(self, context, event):
        global _FEATHER_PICK_MODAL_ACTIVE, _HOVERED_CHAIN_INDEX, _HOVERED_GUIDE_NAME
        # A newer picker owns the globals now -> exit quietly. (PASS_THROUGH alone would keep this
        # handler alive as a zombie, so always combine it with CANCELLED/FINISHED.)
        if getattr(self, "_gen", -1) != _FEATHER_PICK_MODAL_GEN:
            return {'CANCELLED', 'PASS_THROUGH'}

        guide_obj = get_feather_guide_from_context(context)
        if (
            not _FEATHER_PICK_MODAL_ACTIVE
            or guide_obj is None
            or not guide_obj.arantools_feather.in_feather_mode
            or guide_obj.arantools_feather.feather_submode != 'FEATHER_SELECT'
        ):
            self._finish(context)
            return {'CANCELLED', 'PASS_THROUGH'}

        # Keyboard shortcuts inside Feathers Mode (only when cursor is in 3D Viewport)
        v_area, v_reg, _, _ = _find_view3d_region_at_mouse(context, event)
        if v_reg is None:
            if _HOVERED_CHAIN_INDEX != -1:
                _HOVERED_CHAIN_INDEX = -1
                for area in context.screen.areas if context.screen else []:
                    if area.type == 'VIEW_3D':
                        area.tag_redraw()
            return {'PASS_THROUGH'}

        if event.type == 'ESC' and event.value == 'PRESS':
            bpy.ops.arantools.feather_toggle_edit()
            self._finish(context)
            return {'FINISHED'}

        if event.type == 'ONE' and event.value == 'PRESS' and not event.ctrl and not event.alt:
            self._finish(context)
            bpy.ops.arantools.feather_set_submode('INVOKE_DEFAULT', submode='VERTEX')
            return {'FINISHED'}

        if event.type == 'TWO' and event.value == 'PRESS' and not event.ctrl and not event.alt:
            self._finish(context)
            bpy.ops.arantools.feather_set_submode('INVOKE_DEFAULT', submode='EDGE')
            return {'FINISHED'}

        if event.type in {'X', 'DEL'} and event.value == 'PRESS' and not event.ctrl:
            bpy.ops.arantools.feather_delete_selected()
            return {'RUNNING_MODAL'}

        if event.type == 'A' and event.value == 'PRESS' and not event.ctrl:
            act = 'DESELECT' if event.alt else 'SELECT'
            bpy.ops.arantools.feather_select_all(action=act)
            return {'RUNNING_MODAL'}

        if event.type == 'MOUSEMOVE':
            c_idx = _pick_feather_chain_at_mouse(context, event, guide_obj)
            if c_idx != _HOVERED_CHAIN_INDEX:
                _HOVERED_CHAIN_INDEX = c_idx
                _HOVERED_GUIDE_NAME = guide_obj.name
                for area in context.screen.areas if context.screen else []:
                    if area.type == 'VIEW_3D':
                        area.tag_redraw()
            return {'PASS_THROUGH'}

        # DOUBLE_CLICK counts as a press so fast consecutive (Shift+)clicks aren't dropped.
        if event.type == 'LEFTMOUSE' and event.value in {'PRESS', 'DOUBLE_CLICK'} and not event.alt and not event.ctrl:
            c_idx = _pick_feather_chain_at_mouse(context, event, guide_obj)
            cur_sel = set(_get_selected_chain_indices(guide_obj))
            if c_idx >= 0:
                if event.shift:
                    if c_idx in cur_sel:
                        cur_sel.remove(c_idx)
                    else:
                        cur_sel.add(c_idx)
                else:
                    cur_sel = {c_idx}
                _set_selected_chain_indices(guide_obj, sorted(cur_sel), isolate_in_edit=False, context=context)
            else:
                if not event.shift and cur_sel:
                    _set_selected_chain_indices(guide_obj, [], isolate_in_edit=False, context=context)
            for area in context.screen.areas if context.screen else []:
                area.tag_redraw()
            return {'RUNNING_MODAL'}

        # Swallow the rest of the left-click (RELEASE / CLICK / CLICK_DRAG) so Blender's own
        # view3d.select / box-select never runs after our pick and steals the active object.
        if event.type == 'LEFTMOUSE' and not event.alt and not event.ctrl:
            return {'RUNNING_MODAL'}

        return {'PASS_THROUGH'}


class ARANTOOLS_OT_feather_set_submode(Operator):
    bl_idname = "arantools.feather_set_submode"
    bl_label = "Set Feather Mode"
    bl_description = "Switch Feather Mode between Feathers (3), Splines (2), and Vertices (1)"
    bl_options = {'REGISTER', 'UNDO'}

    submode: EnumProperty(
        items=[
            ('FEATHER_SELECT', "Feathers", "Select whole feathers in the viewport and edit feather-level settings"),
            ('EDGE', "Splines", "Edit feather guide splines"),
            ('VERTEX', "Vertices", "Edit individual feather vertices, radius, tilt & smoothing"),
        ],
        default='FEATHER_SELECT',
    )

    def invoke(self, context, event):
        guide_obj = get_feather_guide_from_context(context)
        if guide_obj is None:
            return {'PASS_THROUGH'}
        return self.execute(context)

    def execute(self, context):
        global _FEATHER_PICK_MODAL_ACTIVE, _HOVERED_CHAIN_INDEX
        guide_obj = get_feather_guide_from_context(context)
        if guide_obj is None:
            return {'CANCELLED'}

        fprops = guide_obj.arantools_feather
        fprops.in_feather_mode = True
        out_obj = fprops.output_object
        if out_obj is not None:
            out_obj.hide_select = True

        # Capture current selected feather chains before switching mode
        cur_sel_chains = _get_selected_chain_indices(guide_obj)
        fprops.feather_submode = self.submode

        if self.submode == 'FEATHER_SELECT':
            # Feathers mode runs in Object Mode with our custom 3D raycast picker -> ZERO Edit Mode flicker!
            bpy.ops.arantools.feather_select_modal('INVOKE_DEFAULT')
        else:
            # Vertices or Splines mode -> enter Edit Mode and isolate the selected feather(s)!
            _FEATHER_PICK_MODAL_ACTIVE = False
            _HOVERED_CHAIN_INDEX = -1

            if guide_obj.mode != 'EDIT':
                if context.mode != 'OBJECT':
                    bpy.ops.object.mode_set(mode='OBJECT')
                bpy.ops.object.select_all(action='DESELECT')
                guide_obj.hide_set(False)
                guide_obj.select_set(True)
                context.view_layer.objects.active = guide_obj
                bpy.ops.object.mode_set(mode='EDIT')

            if self.submode == 'VERTEX':
                context.tool_settings.mesh_select_mode = (True, False, False)
            else:
                context.tool_settings.mesh_select_mode = (False, True, False)

            if cur_sel_chains:
                _set_selected_chain_indices(guide_obj, cur_sel_chains, isolate_in_edit=True, context=context)
            else:
                sync_ui_from_selection(context)

        for area in context.screen.areas if context.screen else []:
            area.tag_redraw()
        return {'FINISHED'}


class ARANTOOLS_OT_feather_toggle_select_mode(Operator):
    bl_idname = "arantools.feather_toggle_select_mode"
    bl_label = "Switch Feathers / Vertices"
    bl_description = "Quickly toggle between Feathers selection mode and Vertices editing mode (Shortcut: 4 or Alt+F)"
    bl_options = {'REGISTER', 'UNDO'}

    def invoke(self, context, event):
        guide_obj = get_feather_guide_from_context(context)
        if guide_obj is None:
            return {'PASS_THROUGH'}
        return self.execute(context)

    def execute(self, context):
        guide_obj = get_feather_guide_from_context(context)
        if guide_obj is None:
            return {'CANCELLED'}
        fprops = guide_obj.arantools_feather
        target = 'VERTEX' if (fprops.in_feather_mode and fprops.feather_submode == 'FEATHER_SELECT') else 'FEATHER_SELECT'
        return bpy.ops.arantools.feather_set_submode('INVOKE_DEFAULT', submode=target)


class ARANTOOLS_OT_feather_select_all(Operator):
    bl_idname = "arantools.feather_select_all"
    bl_label = "Select / Deselect All Feathers"
    bl_description = "Select all feathers or clear selection in Feather Mode"
    bl_options = {'REGISTER', 'UNDO'}

    action: EnumProperty(
        items=[
            ('SELECT', "Select All", "Select all feathers"),
            ('DESELECT', "Deselect All", "Clear feather selection"),
        ],
        default='SELECT',
    )

    def execute(self, context):
        guide_obj = get_feather_guide_from_context(context)
        if guide_obj is None:
            return {'CANCELLED'}
        chains = _get_guide_chains_fast(guide_obj)
        if self.action == 'SELECT':
            _unhide_all_feathers(guide_obj, select_all=True)
            _set_selected_chain_indices(guide_obj, list(range(len(chains))), isolate_in_edit=False, context=context)
        else:
            _set_selected_chain_indices(guide_obj, [], isolate_in_edit=False, context=context)
        for area in context.screen.areas if context.screen else []:
            area.tag_redraw()
        return {'FINISHED'}


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
        _set_selected_chain_indices(guide_obj, new_indices, isolate_in_edit=was_edit, context=context)
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
        _set_selected_chain_indices(guide_obj, [], isolate_in_edit=False, context=context)
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
    bl_label = "Set Admitted Feathers (All / None / Re-Roll)"
    bl_description = "Select All admitted meshes (random between all), None, or Re-Roll random variation on selected feather(s)"
    bl_options = {'REGISTER', 'UNDO'}

    mode: EnumProperty(
        items=[
            ('ALL', "All", "Enable all admitted feather meshes (randomly pick among all)"),
            ('NONE', "None", "Clear all admitted feather meshes on selected feather(s)"),
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
    bl_label = "Sync Admitted Meshes from Collection"
    bl_description = "Sync admitted feather card meshes from the assigned Collection"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        guide_obj = get_feather_guide_from_context(context)
        if guide_obj is None:
            return {'CANCELLED'}
        ensure_default_admitted_collection(guide_obj)
        admitted = sync_admitted_collection_to_slots(guide_obj)
        regenerate_feather_object(guide_obj, force_full_rebuild=True)
        self.report({'INFO'}, f"Synced {len(admitted)} admitted feather mesh(es) from collection.")
        return {'FINISHED'}


class ARANTOOLS_UL_FeatherAdmittedList(UIList):
    """Native Blender UIList for Admitted Feather Meshes.
    - In Object Mode: displays the admitted feather card meshes in the collection with polygon counts.
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
        in_feather_mode = bool(getattr(data, "in_feather_mode", False))

        row = layout.row(align=True)
        if in_feather_mode and guide_obj is not None:
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
            e[up_layer] = (0.0, 1.0, 0.0)
            e.select = True

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
            _set_selected_chain_indices(guide_obj, [len(chains) - 1], isolate_in_edit=was_edit, context=context)
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

        ensure_default_admitted_collection(guide_obj)

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
def _feather_depsgraph_update_post(scene, depsgraph):
    global _LAST_SELECTION_SIGNATURE
    if _IS_GENERATING or _IS_SYNCING_UI:
        return

    context = bpy.context
    guide_obj = get_feather_guide_from_context(context)
    if guide_obj is None:
        return

    fprops = guide_obj.arantools_feather

    # If the artist entered Edit Mode directly via Tab, seamlessly sync into Feather Mode (Vertices)
    if guide_obj.mode == 'EDIT' and not fprops.in_feather_mode:
        fprops.in_feather_mode = True
        if fprops.feather_submode == 'FEATHER_SELECT':
            fprops.feather_submode = 'VERTEX'

    if guide_obj.mode == 'EDIT':
        mesh_updated = False
        for update in depsgraph.updates:
            orig_id = update.id.original
            if orig_id == guide_obj or orig_id == guide_obj.data:
                if update.is_updated_geometry:
                    mesh_updated = True
                    break

        if not mesh_updated:
            return

        try:
            bm = bmesh.from_edit_mesh(guide_obj.data)
            cur_verts_co = [(v.co.x, v.co.y, v.co.z) for v in bm.verts]
            sel_v = tuple(v.index for v in bm.verts if v.select)
            sel_e = tuple(e.index for e in bm.edges if e.select)
            act_id = getattr(bm.select_history.active, "index", -1)
            sig = (guide_obj.name, sel_v, sel_e, act_id)
            if sig != _LAST_SELECTION_SIGNATURE:
                _LAST_SELECTION_SIGNATURE = sig
                sync_ui_from_selection(context)

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
    """Draw Feather Mode viewport overlays:
    1. Selected Feather(s) 3D Gold highlight (in Feathers mode)
    2. Hovered Feather 3D Cyan highlight (in Feathers mode, on normal or mirrored side)
    3. Root-to-Tip direction chevrons along visible feather chains
    """
    if bpy.app.background:
        return
    context = bpy.context
    guide_obj = get_feather_guide_from_context(context)
    if guide_obj is None:
        return
    fprops = guide_obj.arantools_feather
    if not fprops.in_feather_mode and guide_obj.mode != 'EDIT' and context.active_object != guide_obj:
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
    chain_ranges = obj_cache.get("chain_ranges", []) if obj_cache else []
    range_map = {r[0]: r for r in chain_ranges}

    shader = gpu.shader.from_builtin('UNIFORM_COLOR')
    gpu.state.blend_set('ALPHA')

    sel_chains = set(_get_selected_chain_indices(guide_obj)) if fprops.in_feather_mode else set()

    # ── 1. Selected Feather(s) Highlight in Feathers Mode ────────────────────
    if fprops.in_feather_mode and fprops.feather_submode == 'FEATHER_SELECT' and sel_chains and out_obj is not None:
        sel_tris = []
        sel_wires = []
        sel_spline_lines = []
        sel_spline_pts = []
        out_mw = out_obj.matrix_world

        for c_idx in sel_chains:
            if not (0 <= c_idx < len(chains)):
                continue
            tr = range_map.get(c_idx)
            if tr is not None:
                _append_chain_card_geometry(out_obj, tr, out_mw, sel_tris, sel_wires)
            vc = chains[c_idx][0]
            w_pts = [mw @ Vector(verts_co[vi]) for vi in vc]
            sel_spline_pts.extend(w_pts)
            for i in range(len(w_pts) - 1):
                sel_spline_lines.extend((w_pts[i], w_pts[i + 1]))

        if sel_tris:
            b_stris = batch_for_shader(shader, 'TRIS', {"pos": sel_tris})
            shader.bind()
            shader.uniform_float("color", (1.0, 0.72, 0.12, 0.30))
            b_stris.draw(shader)

        if sel_wires:
            gpu.state.line_width_set(2.2)
            b_swires = batch_for_shader(shader, 'LINES', {"pos": sel_wires})
            shader.bind()
            shader.uniform_float("color", (1.0, 0.82, 0.18, 0.92))
            b_swires.draw(shader)

        if sel_spline_lines:
            gpu.state.line_width_set(3.0)
            b_slines = batch_for_shader(shader, 'LINES', {"pos": sel_spline_lines})
            shader.bind()
            shader.uniform_float("color", (1.0, 0.90, 0.25, 1.0))
            b_slines.draw(shader)

        if sel_spline_pts:
            gpu.state.point_size_set(8.0)
            b_spts = batch_for_shader(shader, 'POINTS', {"pos": sel_spline_pts})
            shader.bind()
            shader.uniform_float("color", (1.0, 0.95, 0.35, 1.0))
            b_spts.draw(shader)

    # ── 2. 3D Feather Fill Hover Preview in Feathers Mode ────────────────────
    if (
        _FEATHER_PICK_MODAL_ACTIVE
        and _HOVERED_GUIDE_NAME == guide_obj.name
        and 0 <= _HOVERED_CHAIN_INDEX < len(chains)
    ):
        c_idx = _HOVERED_CHAIN_INDEX
        target_range = range_map.get(c_idx)
        refl_mat = _get_mirror_reflection_matrix(fprops) if (_HOVERED_MIRRORED and fprops.use_mirror) else None

        if out_obj is not None and out_obj.data is not None and target_range is not None:
            out_mw = (refl_mat @ out_obj.matrix_world) if refl_mat is not None else out_obj.matrix_world
            tri_coords = []
            wire_coords = []
            _append_chain_card_geometry(out_obj, target_range, out_mw, tri_coords, wire_coords)

            if tri_coords:
                b_fill = batch_for_shader(shader, 'TRIS', {"pos": tri_coords})
                shader.bind()
                shader.uniform_float("color", (0.15, 0.88, 1.0, 0.42))
                b_fill.draw(shader)

            if wire_coords:
                gpu.state.line_width_set(2.5)
                b_wire = batch_for_shader(shader, 'LINES', {"pos": wire_coords})
                shader.bind()
                shader.uniform_float("color", (0.38, 0.96, 1.0, 0.98))
                b_wire.draw(shader)

        hov_v_chain = chains[c_idx][0]
        guide_mw = (refl_mat @ mw) if refl_mat is not None else mw
        hov_pts = [guide_mw @ Vector(verts_co[vi]) for vi in hov_v_chain]
        if len(hov_pts) >= 2:
            hov_lines = []
            for i in range(len(hov_pts) - 1):
                hov_lines.extend((hov_pts[i], hov_pts[i + 1]))
            gpu.state.line_width_set(3.5)
            b_hlines = batch_for_shader(shader, 'LINES', {"pos": hov_lines})
            shader.bind()
            shader.uniform_float("color", (0.45, 0.98, 1.0, 1.0))
            b_hlines.draw(shader)

            gpu.state.point_size_set(9.0)
            b_hpts = batch_for_shader(shader, 'POINTS', {"pos": hov_pts})
            shader.bind()
            shader.uniform_float("color", (0.85, 1.0, 1.0, 1.0))
            b_hpts.draw(shader)

    # ── 3. Direction Arrows (Root = Blue, Chevrons = Green, Tip = Orange) ────
    if fprops.show_direction_overlay:
        hidden_v = set()
        if guide_obj.mode == 'EDIT':
            try:
                bm = bmesh.from_edit_mesh(guide_obj.data)
                hidden_v = {v.index for v in bm.verts if v.hide}
            except Exception:
                pass

        mw_key = (tuple(round(x, 4) for row in mw for x in row), len(hidden_v))
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
                is_rev = any(rev_list[ei] for ei in e_chain)
                ordered_v = list(reversed(v_chain)) if is_rev else list(v_chain)
                w_pts = [mw @ Vector(verts_co[vi]) for vi in ordered_v]
                root_points.append(w_pts[0])
                tip_points.append(w_pts[-1])

                for i in range(len(w_pts) - 1):
                    p0, p1 = w_pts[i], w_pts[i + 1]
                    seg = p1 - p0
                    length = seg.length
                    if length < 1e-5:
                        continue
                    d = seg / length
                    mid = p0.lerp(p1, 0.6)
                    up = Vector((0.0, 0.0, 1.0)) if abs(d.z) < 0.9 else Vector((1.0, 0.0, 0.0))
                    side = d.cross(up).normalized() * (length * 0.14)
                    wing_back = mid - d * (length * 0.18)
                    line_coords.extend((mid, wing_back + side, mid, wing_back - side))

            b_lines = batch_for_shader(shader, 'LINES', {"pos": line_coords}) if line_coords else None
            b_roots = batch_for_shader(shader, 'POINTS', {"pos": root_points}) if root_points else None
            b_tips = batch_for_shader(shader, 'POINTS', {"pos": tip_points}) if tip_points else None
            gpu_batches = (mw_key, b_lines, b_roots, b_tips)
            if obj_cache is not None:
                obj_cache["gpu_batches"] = gpu_batches

        _, b_lines, b_roots, b_tips = gpu_batches
        if b_lines is not None:
            gpu.state.line_width_set(2.0)
            shader.bind()
            shader.uniform_float("color", (0.15, 0.95, 0.45, 0.88))
            b_lines.draw(shader)

        if b_roots is not None:
            gpu.state.point_size_set(7.0)
            shader.bind()
            shader.uniform_float("color", (0.2, 0.6, 1.0, 0.95))
            b_roots.draw(shader)

        if b_tips is not None:
            gpu.state.point_size_set(7.0)
            shader.bind()
            shader.uniform_float("color", (1.0, 0.35, 0.2, 0.95))
            b_tips.draw(shader)

    gpu.state.point_size_set(1.0)
    gpu.state.line_width_set(1.0)
    gpu.state.blend_set('NONE')


# ============================================================================
# 3D Viewport Header Integration (Top-Left Mode Bar next to Blender Mode)
# ============================================================================

def _draw_feather_mode_view3d_header(self, context):
    """Draw the dedicated Feather Mode bar in the 3D Viewport top-left header when a Feather Object is active."""
    guide_obj = get_feather_guide_from_context(context)
    if guide_obj is None:
        return

    fprops = guide_obj.arantools_feather
    in_fm = bool(fprops.in_feather_mode)
    cur_sub = fprops.feather_submode

    layout = self.layout
    layout.separator_spacer()

    row = layout.row(align=True)
    row.operator(
        "arantools.feather_toggle_edit",
        text="Feather Mode",
        icon='STRANDS',
        depress=in_fm,
    )

    if in_fm:
        sub_row = layout.row(align=True)
        op_f = sub_row.operator(
            "arantools.feather_set_submode",
            text="Feathers",
            icon='STRANDS',
            depress=(cur_sub == 'FEATHER_SELECT'),
        )
        op_f.submode = 'FEATHER_SELECT'

        op_s = sub_row.operator(
            "arantools.feather_set_submode",
            text="Splines",
            icon='CURVE_PATH',
            depress=(cur_sub == 'EDGE'),
        )
        op_s.submode = 'EDGE'

        op_v = sub_row.operator(
            "arantools.feather_set_submode",
            text="Vertices",
            icon='VERTEXSEL',
            depress=(cur_sub == 'VERTEX'),
        )
        op_v.submode = 'VERTEX'

        tools_row = layout.row(align=True)
        tools_row.operator("arantools.feather_show_all", text="Show All", icon='HIDE_OFF')
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
        row = scol.row(align=True)
        row.scale_y = 1.3
        row.operator(
            "arantools.feather_convert_scene",
            text="Convert Feather Collection",
            icon='MOD_PARTICLES',
        )
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
    in_fm = bool(fprops.in_feather_mode)
    cur_sub = fprops.feather_submode if in_fm else 'OBJECT'
    in_feather_submode = in_fm and cur_sub in ('FEATHER_SELECT', 'EDGE')
    in_vertex_submode = in_fm and cur_sub == 'VERTEX'

    # ── 1. Top Mode & Viewport Bar ──────────────────────────────────────────
    top_box = layout.box()
    tcol = top_box.column(align=True)

    # Primary Mode Enter / Exit Button
    mode_row = tcol.row(align=True)
    mode_row.scale_y = 1.35
    mode_row.operator(
        "arantools.feather_toggle_edit",
        text="Exit Feather Mode" if in_fm else "Enter Feather Mode",
        icon='OBJECT_DATAMODE' if in_fm else 'STRANDS',
        depress=in_fm,
    )
    mode_row.operator("arantools.feather_regenerate", text="", icon='FILE_REFRESH')

    # Clean Sub-Mode Switcher (Feathers | Splines | Vertices — no numbers in labels!)
    if in_fm:
        tcol.separator(factor=0.4)
        sub_row = tcol.row(align=True)
        sub_row.scale_y = 1.3

        op_f = sub_row.operator(
            "arantools.feather_set_submode",
            text="Feathers",
            icon='STRANDS',
            depress=(cur_sub == 'FEATHER_SELECT'),
        )
        op_f.submode = 'FEATHER_SELECT'

        op_s = sub_row.operator(
            "arantools.feather_set_submode",
            text="Splines",
            icon='CURVE_PATH',
            depress=(cur_sub == 'EDGE'),
        )
        op_s.submode = 'EDGE'

        op_v = sub_row.operator(
            "arantools.feather_set_submode",
            text="Vertices",
            icon='VERTEXSEL',
            depress=(cur_sub == 'VERTEX'),
        )
        op_v.submode = 'VERTEX'

    # Compact Viewport Display Bar (Shading dropdown + Arrows + Live toggles)
    tcol.separator(factor=0.5)
    disp_row = tcol.row(align=True)
    disp_row.prop(fprops, "view_mode", text="")
    disp_row.prop(fprops, "show_direction_overlay", text="Arrows", toggle=True, icon='FORWARD')
    disp_row.prop(fprops, "auto_update", text="Live", toggle=True, icon='AUTO')

    # ── 2. Section Tab Switcher: [ Controls ]  |  [ Advanced ] ──────────────
    tab_row = layout.row(align=True)
    tab_row.scale_y = 1.2
    tab_row.prop(ui, "panel_tab", expand=True)

    # ========================================================================
    # TAB A: CONTROLS (Contextual to Object Mode / Feathers Mode / Vertices Mode)
    # ========================================================================
    if ui.panel_tab == 'CONTROLS':

        # ── CASE 1: OBJECT MODE (Author Admitted Feather Collection) ────────
        if not in_fm:
            adm_box = layout.box()
            acol = adm_box.column(align=True)
            acol.label(text="Admitted Feather Meshes", icon='OUTLINER_COLLECTION')
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

        # ── CASE 2: FEATHERS / SPLINES MODE (Entire-Feather Controls Only) ──
        elif in_feather_submode:
            sel_chains = _get_selected_chain_indices(guide_obj)
            n_sel = len(sel_chains)

            # A. Feather Tools Card
            tool_box = layout.box()
            t_col = tool_box.column(align=True)
            hdr_row = t_col.row(align=True)
            if n_sel == 0:
                hdr_row.label(text="Click a feather in the viewport", icon='RESTRICT_SELECT_OFF')
            elif n_sel == 1:
                hdr_row.label(text=f"1 Feather Selected (#{sel_chains[0] + 1})", icon='STRANDS')
            else:
                hdr_row.label(text=f"{n_sel} Feathers Selected", icon='STRANDS')

            t_col.separator(factor=0.3)
            r1 = t_col.row(align=True)
            r1.scale_y = 1.2
            op_sall = r1.operator("arantools.feather_select_all", text="Select All", icon='SELECT_EXTEND')
            op_sall.action = 'SELECT'
            op_snone = r1.operator("arantools.feather_select_all", text="Clear", icon='X')
            op_snone.action = 'DESELECT'
            r1.operator("arantools.feather_show_all", text="Show All", icon='HIDE_OFF')

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

            # C. Direction Card
            dir_box = layout.box()
            dcol = dir_box.column(align=True)
            dcol.label(text="Direction", icon='FORWARD')
            dcol.separator(factor=0.3)
            drow = dcol.row(align=True)
            drow.scale_y = 1.25
            drow.operator("arantools.feather_flip_direction", text="Flip Direction", icon='FILE_REFRESH')
            drow.operator("arantools.feather_flip_keep_look", text="Flip (Keep Look)", icon='ARROW_LEFTRIGHT')

            # D. Spline Shape Card
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
            vtr1.prop(ui, "apply_whole_chain", text="Whole Feather", toggle=True, icon='LINKED')
            vtr1.operator("arantools.feather_show_all", text="Show All", icon='HIDE_OFF')

            vtcol.separator(factor=0.25)
            vtr2 = vtcol.row(align=True)
            vtr2.scale_y = 1.2
            vtr2.operator("arantools.feather_taper_radius", text="Taper Root -> Tip", icon='IPO_LINEAR')
            vtr2.operator("arantools.feather_select_chain", text="Select Linked", icon='LINKED')

            vert_box = layout.box()
            vcol = vert_box.column(align=True)
            vcol.label(text="Vertex Shape", icon='SURFACE_NCURVE')
            vcol.separator(factor=0.3)
            vcol.prop(ui, "vert_radius", text="Radius (Width)", slider=True)
            vcol.prop(ui, "vert_tilt", text="Tilt (Twist)", slider=True)
            vcol.prop(ui, "vert_smooth", text="Smooth", slider=True)

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
        ocol.separator(factor=0.3)
        ocol.prop(ui, "edge_reverse", text="Reversed Direction Flag")

        # 4. Scene Setup & Conversion
        conv_box = layout.box()
        ccol = conv_box.column(align=True)
        ccol.label(text="Scene Setup", icon='COLLECTION_NEW')
        ccol.separator(factor=0.3)
        ccol.operator(
            "arantools.feather_convert_scene",
            text="Convert Feather Collection",
            icon='MOD_PARTICLES',
        )
        ccol.operator(
            "arantools.feather_create_object",
            text="New Custom Feather Object",
            icon='ADD',
        )


# ============================================================================
# Registration
# ============================================================================

classes = (
    ARANTOOLS_PG_FeatherMeshSlot,
    ARANTOOLS_PG_FeatherObject,
    ARANTOOLS_PG_FeatherSceneUI,
    ARANTOOLS_UL_FeatherAdmittedList,
    ARANTOOLS_OT_feather_convert_scene,
    ARANTOOLS_OT_feather_bake_parameters,
    ARANTOOLS_OT_feather_toggle_edit,
    ARANTOOLS_OT_feather_show_all,
    ARANTOOLS_OT_feather_select_modal,
    ARANTOOLS_OT_feather_set_submode,
    ARANTOOLS_OT_feather_toggle_select_mode,
    ARANTOOLS_OT_feather_select_all,
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
    ARANTOOLS_OT_feather_add_chain,
    ARANTOOLS_OT_feather_create_object,
)


def _register_feather_keymaps():
    """Register Feather Mode shortcuts (1: Vertices, 2: Splines, 3: Feathers, 4 / Alt+F: Toggle, Alt+H: Show All)."""
    _unregister_feather_keymaps()
    wm = getattr(bpy.context, "window_manager", None)
    if wm is None or wm.keyconfigs.addon is None:
        return
    kc = wm.keyconfigs.addon

    for km_name in ("Mesh", "Object Non-modal"):
        km = kc.keymaps.new(name=km_name, space_type='EMPTY')

        kmi1 = km.keymap_items.new("arantools.feather_set_submode", 'ONE', 'PRESS', head=True)
        kmi1.properties.submode = 'VERTEX'
        _ADDON_KEYMAPS.append((km, kmi1))

        kmi2 = km.keymap_items.new("arantools.feather_set_submode", 'TWO', 'PRESS', head=True)
        kmi2.properties.submode = 'EDGE'
        _ADDON_KEYMAPS.append((km, kmi2))

        kmi3 = km.keymap_items.new("arantools.feather_set_submode", 'THREE', 'PRESS', head=True)
        kmi3.properties.submode = 'FEATHER_SELECT'
        _ADDON_KEYMAPS.append((km, kmi3))

        kmi4 = km.keymap_items.new("arantools.feather_toggle_select_mode", 'FOUR', 'PRESS', head=True)
        _ADDON_KEYMAPS.append((km, kmi4))

        kmi_altf = km.keymap_items.new("arantools.feather_toggle_select_mode", 'F', 'PRESS', alt=True, head=True)
        _ADDON_KEYMAPS.append((km, kmi_altf))

        if km_name == "Mesh":
            kmi_alth = km.keymap_items.new("arantools.feather_show_all", 'H', 'PRESS', alt=True, head=True)
            _ADDON_KEYMAPS.append((km, kmi_alth))


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

    if not bpy.app.background and _DRAW_HANDLER is None:
        try:
            _DRAW_HANDLER = bpy.types.SpaceView3D.draw_handler_add(
                _draw_feather_directions_viewport, (), 'WINDOW', 'POST_VIEW'
            )
        except Exception:
            _DRAW_HANDLER = None

    _install_view3d_header()
    _register_feather_keymaps()

    try:
        bpy.app.timers.register(_auto_setup_open_scene_timer, first_interval=0.05)
    except Exception:
        pass


def unregister():
    global _DRAW_HANDLER, _FEATHER_PICK_MODAL_ACTIVE, _HOVERED_CHAIN_INDEX
    _FEATHER_PICK_MODAL_ACTIVE = False
    _HOVERED_CHAIN_INDEX = -1
    _unregister_feather_keymaps()
    _uninstall_view3d_header()
    if _DRAW_HANDLER is not None:
        try:
            bpy.types.SpaceView3D.draw_handler_remove(_DRAW_HANDLER, 'WINDOW')
        except Exception:
            pass
        _DRAW_HANDLER = None

    if _feather_depsgraph_update_post in bpy.app.handlers.depsgraph_update_post:
        bpy.app.handlers.depsgraph_update_post.remove(_feather_depsgraph_update_post)

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
