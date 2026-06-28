"""
Branch Retopo (Sweep) — Extras tool.

Standalone cross-section sweep retopology for organic tree/branch sculpts.

Two phases (plus a one-click Full Auto that chains them):

  DETECT   march cross-sections along each branch, find branch splits, and emit
           an EDITABLE vertex+edge "stick figure" skeleton (one node per
           cross-section centroid) with a per-vertex `radius` attribute and a
           stored root vertex. The artist can nudge / delete / re-root it.

  GENERATE walk the (possibly edited) skeleton from its root, and for every
           skeleton vertex place a ring of points ON the sculpt surface
           (ray-cast), bridge consecutive rings into quads, write clean
           cylinder UVs (U around, V along arc length), and stitch branch
           junctions (stop-short + merge-by-distance + optional Subdivision
           Surface). Output a quad-cylinder retopo that hugs the silhouette.

Everything runs in the SOURCE object's local space; the produced objects share
the source's `matrix_world`. Fine bark detail is intentionally left to the
addon's Normal Map Baker — this is the clean low cage.

Reuses: baking._loose_part_ids / coords helpers; tree_branch._build_branches +
root storage. Heavy math is NumPy; the surface query is mathutils BVHTree.
"""

import bpy
import bmesh
import math

import numpy as np
from mathutils import Vector, Matrix
from mathutils.bvhtree import BVHTree

from . import baking as _baking
from . import tree_branch as _tb


# Custom prop on the skeleton mesh remembering which sculpt it came from, so
# Generate can find the source without the user re-picking it.
_SOURCE_KEY = "arantools_retopo_source"
# Custom prop holding the list of root vertex indices (manual mode supports
# several roots in one skeleton mesh).
_ROOTS_KEY = "arantools_retopo_roots"


def _get_roots(mesh):
    """Root vertex indices for a skeleton mesh: the multi-root list if set,
    else the single sticky root, else empty."""
    r = mesh.get(_ROOTS_KEY)
    if r is not None:
        try:
            out = [int(x) for x in r]
            if out:
                return out
        except TypeError:
            pass
    single = _tb.get_root_vert_index(mesh)
    return [single] if single is not None else []


def _set_roots(mesh, indices):
    mesh[_ROOTS_KEY] = [int(i) for i in indices]


# ============================================================================
# Mesh / geometry helpers (all in the source object's LOCAL space)
# ============================================================================

def _eval_arrays(obj, depsgraph):
    """Return (verts (N,3) float64, tris (M,3) int, normals (N,3) float64) for
    the evaluated object in LOCAL space — modifiers/remesh baked in."""
    eval_obj = obj.evaluated_get(depsgraph)
    me = eval_obj.to_mesh()
    try:
        me.calc_loop_triangles()
        n = len(me.vertices)
        co = np.empty(n * 3, dtype=np.float64)
        me.vertices.foreach_get('co', co)
        verts = co.reshape(-1, 3)
        nrm = np.empty(n * 3, dtype=np.float64)
        me.vertex_normals.foreach_get('vector', nrm)
        normals = nrm.reshape(-1, 3)
        nt = len(me.loop_triangles)
        ti = np.empty(nt * 3, dtype=np.int64)
        me.loop_triangles.foreach_get('vertices', ti)
        tris = ti.reshape(-1, 3)
        return verts, tris, normals
    finally:
        eval_obj.to_mesh_clear()


def _build_bvh(verts, tris):
    """World-agnostic BVH (local space) from triangle soup."""
    return BVHTree.FromPolygons(verts.tolist(), tris.tolist(),
                                all_triangles=True)


def _edges_from_tris(tris):
    """Unique undirected edges (E,2) of a triangle soup."""
    e = np.concatenate([tris[:, [0, 1]], tris[:, [1, 2]], tris[:, [2, 0]]],
                       axis=0)
    e = np.sort(e, axis=1)
    return np.unique(e, axis=0)


def _component_ids(n, ev):
    """Connected-component id per vertex (union-find over edge list ev (E,2))."""
    parent = list(range(n))

    def find(x):
        r = x
        while parent[r] != r:
            r = parent[r]
        while parent[x] != r:
            parent[x], x = r, parent[x]
        return r

    for a, b in ev.tolist():
        ra, rb = find(int(a)), find(int(b))
        if ra != rb:
            parent[ra] = rb
    return [find(i) for i in range(n)]


def _initial_perp(t):
    """A unit vector perpendicular to tangent t (np3)."""
    a = np.array([1.0, 0.0, 0.0]) if abs(t[0]) < 0.9 else np.array([0.0, 1.0, 0.0])
    u = a - t * (a @ t)
    nrm = np.linalg.norm(u)
    if nrm < 1e-9:
        return np.array([0.0, 1.0, 0.0])
    return u / nrm


def _rmf_next(t_new, u_prev):
    """Rotation-minimising frame update: project previous up onto the plane
    perpendicular to the new tangent (keeps the ring from twisting)."""
    u = u_prev - t_new * (u_prev @ t_new)
    nrm = np.linalg.norm(u)
    if nrm < 1e-9:
        return _initial_perp(t_new)
    return u / nrm


# ============================================================================
# DETECT — contract the whole mesh to its medial axis, then extract the graph
# ============================================================================
#
# Umbrella (Laplacian) contraction collapses every cross-section ring toward
# its centre, leaving a thin medial line for the WHOLE connected shape at once
# (all feet, the trunk, the loop, every twig). We then bin the contracted
# points on a 3D grid to get evenly spaced skeleton nodes and connect cells via
# the original edges — so branches and loops are preserved by spatial
# separation (no fragile marching, no single-path traversal).

def _medial_points(verts, normals, bvh, diag):
    """Collapse each surface vertex onto the medial axis: shoot a ray inward
    (−normal) to the opposite wall and take the midpoint. Falls back to the
    vertex itself when no sensible opposite wall is found."""
    n = len(verts)
    M = verts.copy()
    eps = max(diag * 1e-4, 1e-7)
    max_d = diag * 0.6
    for i in range(n):
        ni = normals[i]
        nl = ni @ ni
        if nl < 1e-12:
            continue
        ni = ni / math.sqrt(nl)
        v = verts[i]
        # Try inward (−normal); if that misses (e.g. flipped/odd normals) try
        # the other way. Take the nearer valid opposite-wall hit.
        best = None
        for s in (-1.0, 1.0):
            d = Vector((ni[0] * s, ni[1] * s, ni[2] * s))
            origin = Vector((v[0] + d.x * eps, v[1] + d.y * eps, v[2] + d.z * eps))
            hit = bvh.ray_cast(origin, d)
            if (hit[0] is not None and hit[3] is not None
                    and eps * 4 < hit[3] < max_d):
                if best is None or hit[3] < best[1]:
                    best = (hit[0], hit[3])
        if best is not None:
            hp = best[0]
            M[i] = ((v[0] + hp.x) * 0.5, (v[1] + hp.y) * 0.5, (v[2] + hp.z) * 0.5)
    return M


def _smooth_cloud(P, ev, iterations, strength):
    """A few umbrella passes to denoise the medial cloud along the mesh graph."""
    if iterations <= 0:
        return P
    n = len(P)
    deg = np.zeros(n)
    np.add.at(deg, ev[:, 0], 1.0)
    np.add.at(deg, ev[:, 1], 1.0)
    deg = np.maximum(deg, 1.0)[:, None]
    Q = P.copy()
    for _ in range(iterations):
        nbr = np.zeros((n, 3))
        np.add.at(nbr, ev[:, 0], Q[ev[:, 1]])
        np.add.at(nbr, ev[:, 1], Q[ev[:, 0]])
        nbr /= deg
        Q = Q + strength * (nbr - Q)
    return Q


def _skeletonize(verts, tris, normals, params):
    """Project to the medial axis, denoise, then grid-bin into skeleton nodes.
    Returns (node_pos list[np3], node_radius list[float], edges list[(i,j)]).
    node_pos is the medial point; radius is the mean distance of the surface
    verts in that cell to the node."""
    ev = _edges_from_tris(tris)
    bb_min = verts.min(axis=0)
    bb_max = verts.max(axis=0)
    diag = float(np.linalg.norm(bb_max - bb_min))

    bvh = _build_bvh(verts, tris)
    P = _medial_points(verts, normals, bvh, diag)
    P = _smooth_cloud(P, ev, params['iterations'], params['strength'])

    # Grid cell size from the model scale and the requested detail.
    cell = max(diag / max(params['detail'], 1), 1e-5)

    # Bin contracted points; each occupied cell -> one node.
    keys = np.floor((P - bb_min) / cell).astype(np.int64)
    uniq, node_of = np.unique(keys, axis=0, return_inverse=True)
    K = len(uniq)

    # Node position = mean contracted point in the cell (on the medial line).
    pos = np.zeros((K, 3))
    cnt = np.zeros(K)
    np.add.at(pos, node_of, P)
    np.add.at(cnt, node_of, 1.0)
    cntc = np.maximum(cnt, 1.0)[:, None]
    pos /= cntc

    # Radius = mean distance of the ORIGINAL surface verts in the cell to node.
    dist = np.linalg.norm(verts - pos[node_of], axis=1)
    rad = np.zeros(K)
    np.add.at(rad, node_of, dist)
    rad /= np.maximum(cnt, 1.0)

    # Edges between cells, inherited from original connectivity.
    na = node_of[ev[:, 0]]
    nb = node_of[ev[:, 1]]
    diff = na != nb
    pairs = np.sort(np.stack([na[diff], nb[diff]], axis=1), axis=1)
    if len(pairs):
        epairs = np.unique(pairs, axis=0)
        edges = [(int(a), int(b)) for a, b in epairs.tolist()]
    else:
        edges = []

    node_pos = [pos[i] for i in range(K)]
    node_r = [float(rad[i]) for i in range(K)]
    return node_pos, node_r, edges


def _pick_root_node(node_pos, node_r, mode, cursor_local):
    """Index of the root node: nearest to the 3D cursor (MANUAL) else lowest-Z."""
    P = np.asarray(node_pos)
    if mode == 'MANUAL' and cursor_local is not None:
        return int(np.argmin(np.linalg.norm(P - cursor_local, axis=1)))
    return int(np.argmin(P[:, 2]))


def _params_from_props(props):
    return {
        'iterations': props.contract_iterations,
        'strength': props.contract_strength,
        'detail': props.skeleton_detail,
    }


def _write_skeleton_object(context, src, node_pos, node_r, edges, root_idx):
    """Build the editable vertex+edge skeleton object in src local space."""
    verts = [tuple(p) for p in node_pos]
    mesh = bpy.data.meshes.new(f"{src.name}_skeleton")
    mesh.from_pydata(verts, edges, [])
    mesh.update()

    attr = mesh.attributes.new('radius', 'FLOAT', 'POINT')
    attr.data.foreach_set('value', np.asarray(node_r, dtype=np.float32))

    mesh[_SOURCE_KEY] = src.name
    _tb.set_root_vert_index(mesh, int(root_idx))

    obj = bpy.data.objects.new(f"{src.name}_skeleton", mesh)
    obj.matrix_world = src.matrix_world.copy()
    context.collection.objects.link(obj)
    return obj


def _detect_into_object(context, src, props):
    """Project to the medial axis + extract the full skeleton graph and emit
    one skeleton object."""
    depsgraph = context.evaluated_depsgraph_get()
    verts, tris, normals = _eval_arrays(src, depsgraph)
    if len(verts) == 0 or len(tris) == 0:
        return None, "source mesh is empty"

    # Optionally restrict to the largest loose part.
    if props.process_scope == 'LARGEST':
        part_id, num_parts = _baking._loose_part_ids(src.data)
        if len(part_id) == len(verts) and num_parts > 1:
            counts = np.bincount(part_id, minlength=num_parts)
            keep = int(np.argmax(counts))
            mask = (part_id == keep)
            remap = -np.ones(len(verts), dtype=np.int64)
            remap[mask] = np.arange(int(mask.sum()))
            tri_keep = mask[tris].all(axis=1)
            verts = verts[mask]
            normals = normals[mask]
            tris = remap[tris[tri_keep]]

    if len(verts) < 4 or len(tris) == 0:
        return None, "not enough geometry to skeletonize"

    params = _params_from_props(props)
    node_pos, node_r, edges = _skeletonize(verts, tris, normals, params)
    if not node_pos:
        return None, "skeletonization produced no nodes (raise Skeleton Detail)"

    cursor_local = None
    if props.root_mode == 'MANUAL':
        cl = src.matrix_world.inverted() @ context.scene.cursor.location
        cursor_local = np.array([cl.x, cl.y, cl.z])
    root_idx = _pick_root_node(node_pos, node_r, props.root_mode, cursor_local)

    obj = _write_skeleton_object(context, src, node_pos, node_r, edges, root_idx)
    return obj, None


# ============================================================================
# GENERATE — surface-fitted ring retopo from a skeleton
# ============================================================================

def _read_skeleton(skel):
    """Return (positions np(N,3) in skeleton-local space, radius np(N,),
    has_radius bool). A hand-authored skeleton has no radius attribute, so
    Generate estimates it from the surface instead."""
    me = skel.data
    n = len(me.vertices)
    co = np.empty(n * 3, dtype=np.float64)
    me.vertices.foreach_get('co', co)
    pos = co.reshape(-1, 3)
    radius = np.ones(n, dtype=np.float64) * 0.1
    has_radius = False
    attr = me.attributes.get('radius')
    if attr is not None and attr.domain == 'POINT':
        rb = np.empty(n, dtype=np.float32)
        attr.data.foreach_get('value', rb)
        radius = rb.astype(np.float64)
        has_radius = True
    return pos, radius, has_radius


def _ring_on_surface(bvh, center, u, v, n_seg, radius, props):
    """Place n_seg points on the surface around `center` in the (u,v) plane.
    Outward ray-cast with inward fallback; clamp wild hits to the skeleton
    radius so concavities don't spike."""
    pts = []
    rmax = radius * props.ray_clamp
    off = props.surface_offset
    for k in range(n_seg):
        ang = 2.0 * math.pi * k / n_seg
        dirv = u * math.cos(ang) + v * math.sin(ang)
        dv = Vector((dirv[0], dirv[1], dirv[2]))
        origin = Vector((center[0], center[1], center[2]))
        hit = bvh.ray_cast(origin, dv)
        loc = None
        if hit[0] is not None and hit[3] is not None and hit[3] <= rmax:
            loc = hit[0]
        else:
            far = origin + dv * (rmax * 1.5)
            hit2 = bvh.ray_cast(far, -dv)
            if hit2[0] is not None and (origin - hit2[0]).length <= rmax:
                loc = hit2[0]
        if loc is None:
            loc = origin + dv * radius     # fallback: skeleton radius
        if off != 0.0:
            loc = loc + dv * off
        pts.append((loc.x, loc.y, loc.z))
    return pts


def _generate_retopo(context, skel, src, props):
    """Build the quad-ring retopo object from skel onto src. Supports several
    marked roots in one skeleton (one connected component per root)."""
    depsgraph = context.evaluated_depsgraph_get()
    sv, st, _sn = _eval_arrays(src, depsgraph)
    bvh = _build_bvh(sv, st)

    pos, radius, has_radius = _read_skeleton(skel)
    nverts = len(pos)
    if nverts == 0:
        return None, "skeleton mesh has no vertices"

    # Map skeleton-local positions into the SOURCE's local space (in case the
    # skeleton object was transformed independently from the sculpt).
    if skel.matrix_world != src.matrix_world:
        M = (src.matrix_world.inverted() @ skel.matrix_world)
        Mn = np.array(M)
        pos = pos @ Mn[:3, :3].T + Mn[:3, 3]

    # Hand-authored skeletons carry no radius — estimate each node's local tube
    # radius as its distance to the nearest surface point.
    if not has_radius:
        for i in range(nverts):
            res = bvh.find_nearest(Vector((pos[i][0], pos[i][1], pos[i][2])))
            if res[0] is not None and res[3] is not None:
                radius[i] = max(res[3], 1e-4)

    # Resolve root vertices (multi-root). Keep the first marked root in each
    # connected component so two roots in one component don't double-build.
    roots = [r for r in _get_roots(skel.data) if 0 <= r < nverts]
    if not roots:
        return None, ("no root marked — select root vertices and use "
                      "'Mark Roots from Selection'")

    me = skel.data
    ne = len(me.edges)
    ev = np.empty(ne * 2, dtype=np.int64)
    me.edges.foreach_get('vertices', ev)
    comp = _component_ids(nverts, ev.reshape(-1, 2))
    chosen = []
    seen_comp = set()
    for r in roots:
        c = comp[r]
        if c in seen_comp:
            continue
        seen_comp.add(c)
        chosen.append(r)

    bm = bmesh.new()
    bm.from_mesh(skel.data)
    bm.verts.ensure_lookup_table()
    branches = []
    for r in chosen:
        branches.extend(_tb._build_branches(bm, bm.verts[r]))
    bm.free()
    if not branches:
        return None, "skeleton has roots but no edges to walk"

    n_seg = props.radial_segments
    out = bmesh.new()
    uv_layer = out.loops.layers.uv.new("UVMap")

    # vertex_of[(branch_id, ring_index, k)] -> BMVert
    def make_ring(center, u, v, radius_val, vlist):
        pts = _ring_on_surface(bvh, center, u, v, n_seg, radius_val, props)
        ring = [out.verts.new(p) for p in pts]
        vlist.append(ring)

    for br in branches:
        chain = list(br['vert_indices'])
        # For child branches, start the tube at the parent junction vertex so
        # rings overlap there (merge-by-distance stitches it).
        if br['entry_vert_idx'] >= 0:
            chain = [br['entry_vert_idx']] + chain
        if len(chain) < 2:
            continue

        cpos = pos[chain]
        crad = radius[chain]
        rings = []
        arclen = [0.0]
        u = None
        for i in range(len(chain)):
            if i == 0:
                tan = cpos[1] - cpos[0]
            elif i == len(chain) - 1:
                tan = cpos[i] - cpos[i - 1]
            else:
                tan = cpos[i + 1] - cpos[i - 1]
            nrm = np.linalg.norm(tan)
            tan = tan / nrm if nrm > 1e-9 else np.array([0.0, 0.0, 1.0])
            if u is None:
                u = _initial_perp(tan)
            else:
                u = _rmf_next(tan, u)
            vvec = np.cross(tan, u)
            make_ring(cpos[i], u, vvec, max(crad[i], 1e-4), rings)
            if i > 0:
                arclen.append(arclen[-1] + float(np.linalg.norm(cpos[i] - cpos[i - 1])))

        total = arclen[-1] if arclen[-1] > 1e-9 else 1.0

        # Bridge consecutive rings into quads with cylinder UVs.
        for i in range(len(rings) - 1):
            r0 = rings[i]
            r1 = rings[i + 1]
            v0 = (arclen[i] / total) if props.uv_v_mode == 'NORMALIZED' else arclen[i]
            v1 = (arclen[i + 1] / total) if props.uv_v_mode == 'NORMALIZED' else arclen[i + 1]
            for k in range(n_seg):
                k2 = (k + 1) % n_seg
                try:
                    f = out.faces.new((r0[k], r0[k2], r1[k2], r1[k]))
                except ValueError:
                    continue
                u0 = k / n_seg
                u1 = (k + 1) / n_seg
                f.loops[0][uv_layer].uv = (u0, v0)
                f.loops[1][uv_layer].uv = (u1, v0)
                f.loops[2][uv_layer].uv = (u1, v1)
                f.loops[3][uv_layer].uv = (u0, v1)

        # Cap the tip (last ring) with a fan to its centroid.
        if props.cap_tips and len(rings) >= 1:
            tip = rings[-1]
            c = np.mean([np.array(v.co) for v in tip], axis=0)
            apex = out.verts.new((c[0], c[1], c[2]))
            for k in range(n_seg):
                k2 = (k + 1) % n_seg
                try:
                    out.faces.new((tip[k], tip[k2], apex))
                except ValueError:
                    pass

    # Tier-1 junction stitch: weld coincident ring verts where child tubes meet
    # the parent, then (optionally) Subdivision Surface smooths the seam.
    merge_dist = props.junction_merge * float(np.mean(radius) if len(radius) else 0.1)
    if merge_dist > 0.0:
        bmesh.ops.remove_doubles(out, verts=out.verts, dist=merge_dist)
    out.normal_update()

    mesh = bpy.data.meshes.new(f"{src.name}_retopo")
    out.to_mesh(mesh)
    out.free()
    mesh.update()

    obj = bpy.data.objects.new(f"{src.name}_retopo", mesh)
    obj.matrix_world = src.matrix_world.copy()
    context.collection.objects.link(obj)

    if props.add_subsurf:
        sub = obj.modifiers.new("Smooth Junctions", 'SUBSURF')
        sub.levels = props.subsurf_levels
        sub.render_levels = props.subsurf_levels

    return obj, None


def _find_source(skel, props):
    """Resolve the source sculpt for a skeleton: explicit prop, then stored
    name, else None."""
    if props.source is not None:
        return props.source
    name = skel.data.get(_SOURCE_KEY)
    if name:
        return bpy.data.objects.get(name)
    return None


# ============================================================================
# Property group
# ============================================================================

class ARANTOOLS_PG_TreeRetopo(bpy.types.PropertyGroup):
    source: bpy.props.PointerProperty(
        name="Source Sculpt",
        type=bpy.types.Object,
        description="High-detail sculpt to retopologise. Leave empty to use "
                    "the active object (Detect) or the skeleton's stored source "
                    "(Generate)",
        poll=lambda self, o: o.type == 'MESH',
    )

    # ── Detect ──
    root_mode: bpy.props.EnumProperty(
        name="Root",
        description="Where each branch march starts",
        items=[
            ('LOWEST_Z', "Lowest Point", "Trunk base = lowest vertex, march up"),
            ('MANUAL', "3D Cursor", "Nearest point to the 3D cursor, march up"),
        ],
        default='LOWEST_Z',
    )
    process_scope: bpy.props.EnumProperty(
        name="Loose Parts",
        description="Which disconnected pieces of the mesh to process",
        items=[
            ('ALL', "All Parts", "Process every loose part into one skeleton"),
            ('LARGEST', "Largest Only", "Only the biggest loose part"),
        ],
        default='ALL',
    )
    contract_iterations: bpy.props.IntProperty(
        name="Medial Smoothing",
        description="Denoising passes over the medial-axis cloud before "
                    "binning. Higher = smoother centerline, fewer kinks",
        default=5, min=0, max=100)
    contract_strength: bpy.props.FloatProperty(
        name="Smoothing Strength",
        description="Strength of each medial-cloud smoothing pass",
        default=0.5, min=0.05, max=1.0)
    skeleton_detail: bpy.props.IntProperty(
        name="Skeleton Detail",
        description="Nodes along the longest dimension. Higher = denser "
                    "skeleton (and denser retopo rings). The grid cell that "
                    "merges contracted points = bbox diagonal / this",
        default=60, min=8, max=600)

    # ── Generate ──
    radial_segments: bpy.props.IntProperty(
        name="Sides", description="Points around each ring", default=8, min=3, max=64)
    ray_clamp: bpy.props.FloatProperty(
        name="Ray Clamp", description="Max surface hit distance as × the "
        "skeleton radius (tames concavities / wrong-wall hits)",
        default=2.0, min=1.0, soft_max=10.0)
    surface_offset: bpy.props.FloatProperty(
        name="Surface Offset", description="Push rings out (+) or in (-) along "
        "their ray", default=0.0, soft_min=-0.2, soft_max=0.2, subtype='DISTANCE')
    uv_v_mode: bpy.props.EnumProperty(
        name="UV V",
        items=[
            ('NORMALIZED', "0–1", "V spans 0..1 over each branch"),
            ('ARC_LENGTH', "Arc Length", "V = real length (tiles)"),
        ],
        default='NORMALIZED')
    cap_tips: bpy.props.BoolProperty(name="Cap Tips", default=True)
    junction_merge: bpy.props.FloatProperty(
        name="Junction Weld", description="Merge-by-distance radius at branch "
        "junctions, as × the mean radius. 0 = no weld",
        default=0.6, min=0.0, soft_max=3.0)
    add_subsurf: bpy.props.BoolProperty(
        name="Smooth Junctions (Subsurf)", default=True)
    subsurf_levels: bpy.props.IntProperty(name="Levels", default=1, min=0, max=3)

    show_advanced: bpy.props.BoolProperty(name="Advanced", default=False)


# ============================================================================
# Operators
# ============================================================================

def _active_source(context, props):
    """Pick the source sculpt for Detect: explicit prop, else active mesh."""
    if props.source is not None:
        return props.source
    obj = context.active_object
    if obj is not None and obj.type == 'MESH':
        return obj
    return None


class ARANTOOLS_OT_TreeRetopoDetect(bpy.types.Operator):
    """Detect an editable centerline skeleton from the sculpt by marching
cross-sections. Branch splits become skeleton forks. Edit/re-root it, then
run Generate."""
    bl_idname = "arantools.tree_retopo_detect"
    bl_label = "Detect Skeleton"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        p = context.scene.arantools_tree_retopo
        return _active_source(context, p) is not None

    def execute(self, context):
        props = context.scene.arantools_tree_retopo
        src = _active_source(context, props)
        if src is None:
            self.report({'ERROR'}, "Select the sculpt mesh.")
            return {'CANCELLED'}
        if context.object and context.object.mode != 'OBJECT':
            bpy.ops.object.mode_set(mode='OBJECT')
        try:
            obj, err = _detect_into_object(context, src, props)
        except Exception as e:        # noqa: BLE001
            import traceback
            traceback.print_exc()
            self.report({'ERROR'}, f"Detect failed: {e}")
            return {'CANCELLED'}
        if obj is None:
            self.report({'WARNING'}, err or "Detection produced nothing.")
            return {'CANCELLED'}
        bpy.ops.object.select_all(action='DESELECT')
        obj.select_set(True)
        context.view_layer.objects.active = obj
        self.report({'INFO'},
                    f"Skeleton '{obj.name}': {len(obj.data.vertices)} nodes. "
                    f"Edit if needed, then Generate.")
        return {'FINISHED'}


class ARANTOOLS_OT_TreeRetopoNewSkeleton(bpy.types.Operator):
    """Create an empty vertex-only mesh to author a skeleton by hand (extrude
verts to draw branches, then mark roots)."""
    bl_idname = "arantools.tree_retopo_new_skeleton"
    bl_label = "New Manual Skeleton"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        props = context.scene.arantools_tree_retopo
        me = bpy.data.meshes.new("Skeleton")
        # Seed with a single vertex at the 3D cursor so the artist can extrude.
        me.from_pydata([(0.0, 0.0, 0.0)], [], [])
        me.update()
        obj = bpy.data.objects.new("Skeleton", me)
        obj.location = context.scene.cursor.location
        context.collection.objects.link(obj)
        src = props.source or context.active_object
        if src is not None and src.type == 'MESH':
            me[_SOURCE_KEY] = src.name
        bpy.ops.object.select_all(action='DESELECT')
        obj.select_set(True)
        context.view_layer.objects.active = obj
        self.report({'INFO'},
                    "Skeleton created. Enter Edit Mode, extrude verts along "
                    "the branches, select root verts, then Mark Roots.")
        return {'FINISHED'}


class ARANTOOLS_OT_TreeRetopoMarkRoots(bpy.types.Operator):
    """Mark the selected skeleton vertices as roots (added to any existing
roots). One skeleton may have several roots — one per branch system."""
    bl_idname = "arantools.tree_retopo_mark_roots"
    bl_label = "Mark Roots from Selection"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        obj = context.active_object
        return obj is not None and obj.type == 'MESH'

    def execute(self, context):
        obj = context.active_object
        if obj.mode == 'EDIT':
            bm = bmesh.from_edit_mesh(obj.data)
            sel = [v.index for v in bm.verts if v.select]
        else:
            sel = [v.index for v in obj.data.vertices if v.select]
        if not sel:
            self.report({'ERROR'}, "Select one or more vertices first.")
            return {'CANCELLED'}
        roots = set(_get_roots(obj.data))
        roots.update(sel)
        _set_roots(obj.data, sorted(roots))
        self.report({'INFO'}, f"{len(roots)} root(s) marked.")
        return {'FINISHED'}


class ARANTOOLS_OT_TreeRetopoClearRoots(bpy.types.Operator):
    """Remove all marked roots from the active skeleton."""
    bl_idname = "arantools.tree_retopo_clear_roots"
    bl_label = "Clear Roots"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        obj = context.active_object
        return obj is not None and obj.type == 'MESH'

    def execute(self, context):
        me = context.active_object.data
        _set_roots(me, [])
        if _ROOTS_KEY in me.keys():
            del me[_ROOTS_KEY]
        _tb.clear_root_vert_index(me)
        self.report({'INFO'}, "Roots cleared.")
        return {'FINISHED'}


class ARANTOOLS_OT_TreeRetopoSelectRoots(bpy.types.Operator):
    """Select the marked root vertices on the active skeleton (Edit Mode)."""
    bl_idname = "arantools.tree_retopo_select_roots"
    bl_label = "Select Roots"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        obj = context.active_object
        return obj is not None and obj.type == 'MESH'

    def execute(self, context):
        obj = context.active_object
        roots = set(_get_roots(obj.data))
        if not roots:
            self.report({'WARNING'}, "No roots marked.")
            return {'CANCELLED'}
        if obj.mode != 'EDIT':
            bpy.ops.object.mode_set(mode='EDIT')
        bm = bmesh.from_edit_mesh(obj.data)
        for v in bm.verts:
            v.select = (v.index in roots)
        bm.select_flush(True)
        bmesh.update_edit_mesh(obj.data)
        self.report({'INFO'}, f"Selected {len(roots)} root(s).")
        return {'FINISHED'}


class ARANTOOLS_OT_TreeRetopoGenerate(bpy.types.Operator):
    """Generate the surface-fitted quad-cylinder retopo from the ACTIVE
skeleton (rings ray-cast onto the source sculpt, cylinder UVs, junction
welding + optional Subsurf)."""
    bl_idname = "arantools.tree_retopo_generate"
    bl_label = "Generate Retopo"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        obj = context.active_object
        return (obj is not None and obj.type == 'MESH'
                and len(obj.data.polygons) == 0 and len(obj.data.edges) > 0)

    def execute(self, context):
        props = context.scene.arantools_tree_retopo
        skel = context.active_object
        if skel is None or skel.type != 'MESH':
            self.report({'ERROR'}, "Active object must be a skeleton mesh.")
            return {'CANCELLED'}
        src = _find_source(skel, props)
        if src is None:
            self.report({'ERROR'},
                        "No source sculpt — set it in the panel.")
            return {'CANCELLED'}
        if context.object and context.object.mode != 'OBJECT':
            bpy.ops.object.mode_set(mode='OBJECT')
        try:
            obj, err = _generate_retopo(context, skel, src, props)
        except Exception as e:        # noqa: BLE001
            import traceback
            traceback.print_exc()
            self.report({'ERROR'}, f"Generate failed: {e}")
            return {'CANCELLED'}
        if obj is None:
            self.report({'WARNING'}, err or "Generation produced nothing.")
            return {'CANCELLED'}
        bpy.ops.object.select_all(action='DESELECT')
        obj.select_set(True)
        context.view_layer.objects.active = obj
        self.report({'INFO'},
                    f"Retopo '{obj.name}': {len(obj.data.polygons)} faces.")
        return {'FINISHED'}


class ARANTOOLS_OT_TreeRetopoFull(bpy.types.Operator):
    """Full Auto: detect the skeleton from the sculpt and generate the retopo
in one step."""
    bl_idname = "arantools.tree_retopo_full"
    bl_label = "Full Auto Retopo"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        p = context.scene.arantools_tree_retopo
        return _active_source(context, p) is not None

    def execute(self, context):
        props = context.scene.arantools_tree_retopo
        src = _active_source(context, props)
        if src is None:
            self.report({'ERROR'}, "Select the sculpt mesh.")
            return {'CANCELLED'}
        if context.object and context.object.mode != 'OBJECT':
            bpy.ops.object.mode_set(mode='OBJECT')
        try:
            skel, err = _detect_into_object(context, src, props)
            if skel is None:
                self.report({'WARNING'}, err or "Detection produced nothing.")
                return {'CANCELLED'}
            obj, err = _generate_retopo(context, skel, src, props)
        except Exception as e:        # noqa: BLE001
            import traceback
            traceback.print_exc()
            self.report({'ERROR'}, f"Full Auto failed: {e}")
            return {'CANCELLED'}
        if obj is None:
            self.report({'WARNING'}, err or "Generation produced nothing.")
            return {'CANCELLED'}
        bpy.ops.object.select_all(action='DESELECT')
        obj.select_set(True)
        context.view_layer.objects.active = obj
        self.report({'INFO'},
                    f"Retopo '{obj.name}': {len(obj.data.polygons)} faces "
                    f"(skeleton '{skel.name}' kept).")
        return {'FINISHED'}


# ============================================================================
# Registration
# ============================================================================

classes = [
    ARANTOOLS_PG_TreeRetopo,
    ARANTOOLS_OT_TreeRetopoDetect,
    ARANTOOLS_OT_TreeRetopoNewSkeleton,
    ARANTOOLS_OT_TreeRetopoMarkRoots,
    ARANTOOLS_OT_TreeRetopoClearRoots,
    ARANTOOLS_OT_TreeRetopoSelectRoots,
    ARANTOOLS_OT_TreeRetopoGenerate,
    ARANTOOLS_OT_TreeRetopoFull,
]


def register():
    for cls in classes:
        bpy.utils.register_class(cls)
    bpy.types.Scene.arantools_tree_retopo = bpy.props.PointerProperty(
        type=ARANTOOLS_PG_TreeRetopo)


def unregister():
    del bpy.types.Scene.arantools_tree_retopo
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
