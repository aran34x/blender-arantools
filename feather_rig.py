"""
AranTools — Feather auto-rig (weights)
======================================
Weights the generated feather cards to a rig. Per Feather Object: the rig (armature) and a body
mesh whose weights can be transferred. Per feather (stored on the guide's edges):

  * Chain mode — the card is weighted along a bone chain (start bone + length; length 0 = follow
    the chain to its end). Points blend between consecutive bones by where they sit along the
    chain. Optionally the base "follows the body": near the root the chain weights blend into the
    body's transferred weights (amount, falloff along the feather, offset from the root).
  * Body mode — weights transferred from the body mesh at each point's nearest surface point
    (data-transfer style). "Base Weight" blends toward the weights at the feather's base, so the
    whole feather can move with its base instead of stretching over the body.

Everything is computed from REST data (bone rest positions, the body's undeformed mesh, the
generated cards' basis), so it works while the character is posed. Weights are always rebuilt from
these settings, so regenerating the feathers never loses them. Mirror copies use the mirrored bone
(.L <-> .R) when the rig has one.
"""

import bpy
import bmesh
import numpy as np
from bpy.props import (
    BoolProperty, CollectionProperty, EnumProperty, FloatProperty, IntProperty, PointerProperty,
    StringProperty,
)
from bpy.types import Operator, PropertyGroup
from mathutils import Vector
from mathutils.bvhtree import BVHTree

from . import feather_cards as fc


MODE_BODY = 0
MODE_CHAIN = 1

# Per-feather settings on the guide's EDGE domain (uniform along each feather)
RIG_EDGE_INT = (
    ("feather_rig_mode", MODE_BODY),
    ("feather_rig_bone", -1),       # index into the Feather Object's rig bone-name list
    ("feather_rig_length", 0),      # chain length in bones; 0 = to the end of the chain
    ("feather_rig_end", -1),        # exact end bone (from Guess / Choose); -1 = use the length
)
RIG_EDGE_FLOAT = (
    ("feather_rig_factor", 0.0),    # Body mode: blend toward the weights at the feather's base
    ("feather_base_follow", 0.0),   # Chain mode: how much the base takes the body's weights
    ("feather_base_falloff", 0.3),  # ...fading out over this fraction of the feather
    ("feather_base_offset", 0.0),   # ...starting this far from the root
    ("feather_rig_smooth", 0.35),   # Chain mode: weight blending across bones (in bones; 0 = hard)
)

_SYNCING = False
# Chain each feather's mirror copy ended up using at the last bake: {guide: {chain: (names, how)}}
_MIRROR_USED = {}
# Cached body data: {key: (tri_verts, W (n_body_verts, n_cols), col_names, bvh, verts)}
_BODY_CACHE = {}
# Cached projection of the generated cards onto the body: {key: (tri_index, bary)}
_PROJ_CACHE = {}


# ============================================================================
# Per-feather attributes
# ============================================================================

def _ensure_rig_attrs(guide_obj):
    me = guide_obj.data
    if guide_obj.mode == 'EDIT':
        bm = bmesh.from_edit_mesh(me)
        changed = False
        for name, default in RIG_EDGE_INT:
            if bm.edges.layers.int.get(name) is None:
                lyr = bm.edges.layers.int.new(name)
                for e in bm.edges:
                    e[lyr] = default
                changed = True
        for name, default in RIG_EDGE_FLOAT:
            if bm.edges.layers.float.get(name) is None:
                lyr = bm.edges.layers.float.new(name)
                for e in bm.edges:
                    e[lyr] = default
                changed = True
        if changed:
            bmesh.update_edit_mesh(me, loop_triangles=False, destructive=False)
        return
    n = len(me.edges)
    for name, default in RIG_EDGE_INT:
        if me.attributes.get(name) is None:
            me.attributes.new(name=name, type='INT', domain='EDGE').data.foreach_set("value", [default] * n)
    for name, default in RIG_EDGE_FLOAT:
        if me.attributes.get(name) is None:
            me.attributes.new(name=name, type='FLOAT', domain='EDGE').data.foreach_set("value", [float(default)] * n)


def _read_edge(guide_obj, name, is_int, default):
    """Per-edge values of a rig attribute (defaults when missing)."""
    me = guide_obj.data
    if guide_obj.mode == 'EDIT':
        bm = bmesh.from_edit_mesh(me)
        layers = bm.edges.layers.int if is_int else bm.edges.layers.float
        lyr = layers.get(name)
        if lyr is None:
            return np.full(len(bm.edges), default, dtype=np.int64 if is_int else np.float64)
        return np.array([e[lyr] for e in bm.edges], dtype=np.int64 if is_int else np.float64)
    attr = me.attributes.get(name)
    n = len(me.edges)
    if attr is None:
        return np.full(n, default, dtype=np.int64 if is_int else np.float64)
    out = np.empty(n, dtype=np.int32 if is_int else np.float32)
    attr.data.foreach_get("value", out)
    return out.astype(np.int64 if is_int else np.float64)


def _write_feathers(guide_obj, chain_ids, name, value):
    """Set rig attribute `name` to `value` on every edge of the given feathers."""
    if not chain_ids:
        return
    _ensure_rig_attrs(guide_obj)
    chains = fc._get_guide_chains_fast(guide_obj)
    edge_ids = [ei for ci in chain_ids if 0 <= ci < len(chains) for ei in chains[ci][1]]
    is_int = any(name == n for n, _ in RIG_EDGE_INT)
    me = guide_obj.data
    if guide_obj.mode == 'EDIT':
        bm = bmesh.from_edit_mesh(me)
        bm.edges.ensure_lookup_table()
        lyr = (bm.edges.layers.int if is_int else bm.edges.layers.float).get(name)
        for ei in edge_ids:
            bm.edges[ei][lyr] = value
        bmesh.update_edit_mesh(me, loop_triangles=False, destructive=False)
    else:
        data = me.attributes[name].data
        for ei in edge_ids:
            data[ei].value = value


def feather_settings(guide_obj):
    """Per-feather rig settings indexed by chain: dict of arrays."""
    chains = fc._get_guide_chains_fast(guide_obj)
    first = np.array([ec[0] if ec else 0 for _, ec in chains], dtype=np.int64)
    out = {}
    for name, default in RIG_EDGE_INT:
        out[name] = _read_edge(guide_obj, name, True, default)[first] if len(first) else np.zeros(0, np.int64)
    for name, default in RIG_EDGE_FLOAT:
        out[name] = _read_edge(guide_obj, name, False, default)[first] if len(first) else np.zeros(0)
    return out


def target_feathers(context, guide_obj):
    """Feathers the Rig controls act on: picks in Feather Select, the selection while editing,
    every feather in Object Mode."""
    if fc.is_in_feather_mode(guide_obj):
        if fc.get_feather_level(context) == 'FEATHER':
            return fc.get_picked_chains(guide_obj)
        return fc._get_selected_chain_indices(guide_obj)
    return list(range(len(fc._get_guide_chains_fast(guide_obj))))


def _bone_slot(rig_settings, bone_name):
    """Index of `bone_name` in the Feather Object's bone-name list (added if new)."""
    for i, item in enumerate(rig_settings.bones):
        if item.name == bone_name:
            return i
    item = rig_settings.bones.add()
    item.name = bone_name
    return len(rig_settings.bones) - 1


# ============================================================================
# Rig / body helpers
# ============================================================================

def _flip_name(name):
    try:
        return bpy.utils.flip_name(name)
    except Exception:
        pass
    for a, b in ((".L", ".R"), ("_L", "_R"), (".l", ".r"), ("_l", "_r"), ("Left", "Right"), ("left", "right")):
        if name.endswith(a):
            return name[: -len(a)] + b
        if name.endswith(b):
            return name[: -len(b)] + a
    return name


def resolve_chain(arm_obj, start_name, length):
    """Bones from `start_name` down the hierarchy: `length` bones, or to the end when 0
    (at a branch the connected child wins, otherwise the first child by name)."""
    bones = arm_obj.data.bones
    b = bones.get(start_name)
    if b is None:
        return []
    out = [b]
    while (length <= 0 or len(out) < length) and len(out) < 256:
        kids = list(b.children)
        if not kids:
            break
        connected = [k for k in kids if k.use_connect]
        b = (connected or sorted(kids, key=lambda k: k.name))[0]
        out.append(b)
    return out


def _bone_segments(arm_obj, bones):
    mw = arm_obj.matrix_world
    heads = np.array([(mw @ b.head_local)[:] for b in bones], dtype=np.float64)
    tails = np.array([(mw @ b.tail_local)[:] for b in bones], dtype=np.float64)
    return heads, tails


def _chain_weights(points, heads, tails, smooth):
    """(m, K) weights of points along a chain of K bone segments.

    Each point gets a position along the chain (bone index + fraction); `smooth` (in bones) spreads
    its weight over neighbouring bones with a gaussian falloff. 0 = hard segments.
    """
    m, k = len(points), len(heads)
    if k == 1:
        return np.ones((m, 1))
    ab = tails - heads
    l2 = np.maximum((ab * ab).sum(1), 1e-18)
    rel = points[:, None, :] - heads[None, :, :]
    u = np.clip((rel * ab[None]).sum(2) / l2[None], 0.0, 1.0)
    closest = heads[None] + ab[None] * u[:, :, None]
    dist = np.linalg.norm(points[:, None, :] - closest, axis=2)
    seg = dist.argmin(1)
    if smooth <= 1e-4:
        w = np.zeros((m, k))
        w[np.arange(m), seg] = 1.0
        return w
    pos = seg + u[np.arange(m), seg]                      # 0 .. k along the chain
    centres = np.arange(k) + 0.5
    w = np.exp(-0.5 * ((pos[:, None] - centres[None]) / smooth) ** 2)
    return w / np.maximum(w.sum(1, keepdims=True), 1e-12)


def _body_data(body_obj, deform_names):
    """Rest-pose triangles, BVH and per-vertex weight matrix of the body (cached)."""
    me = body_obj.data
    key = (body_obj.name, me.name, len(me.vertices), tuple(round(x, 5) for row in body_obj.matrix_world for x in row),
           tuple(vg.name for vg in body_obj.vertex_groups))
    hit = _BODY_CACHE.get(key)
    if hit is not None:
        return hit
    n = len(me.vertices)
    co = np.empty(n * 3, dtype=np.float64)
    me.vertices.foreach_get("co", co)  # undeformed (rest) coordinates
    mw = np.array(body_obj.matrix_world)
    verts = co.reshape(-1, 3) @ mw[:3, :3].T + mw[:3, 3]
    me.calc_loop_triangles()
    tv = np.empty(len(me.loop_triangles) * 3, dtype=np.int64)
    me.loop_triangles.foreach_get("vertices", tv)
    tv = tv.reshape(-1, 3)
    cols = [vg.name for vg in body_obj.vertex_groups if vg.name in deform_names]
    col_of_group = {vg.index: cols.index(vg.name) for vg in body_obj.vertex_groups if vg.name in deform_names}
    w = np.zeros((n, max(1, len(cols))), dtype=np.float32)
    if col_of_group:
        for v in me.vertices:
            for g in v.groups:
                c = col_of_group.get(g.group)
                if c is not None:
                    w[v.index, c] = g.weight
    bvh = BVHTree.FromPolygons(verts.tolist(), tv.tolist())
    data = (tv, w, cols, bvh, verts)
    _BODY_CACHE.clear()
    _BODY_CACHE[key] = data
    return data


def _project_on_body(body, points, cache_key):
    """Nearest body triangle + barycentric coordinates for each point (cached per generation)."""
    tv, _, _, bvh, verts = body
    hit = _PROJ_CACHE.get(cache_key)
    if hit is not None and len(hit[0]) == len(points):
        return hit
    tri = np.empty(len(points), dtype=np.int64)
    loc = np.empty((len(points), 3))
    for i, p in enumerate(points.tolist()):
        l, _, ti, _ = bvh.find_nearest(p)
        tri[i] = ti if ti is not None else 0
        loc[i] = l if l is not None else p
    a, b, c = verts[tv[tri, 0]], verts[tv[tri, 1]], verts[tv[tri, 2]]
    e1, e2, q = b - a, c - a, loc - a
    d00, d01, d11 = (e1 * e1).sum(1), (e1 * e2).sum(1), (e2 * e2).sum(1)
    d20, d21 = (q * e1).sum(1), (q * e2).sum(1)
    den = np.where(np.abs(d00 * d11 - d01 * d01) > 1e-24, d00 * d11 - d01 * d01, 1.0)
    bv = (d11 * d20 - d01 * d21) / den
    bw = (d00 * d21 - d01 * d20) / den
    bary = np.clip(np.stack([1.0 - bv - bw, bv, bw], axis=1), 0.0, 1.0)
    bary /= np.maximum(bary.sum(1, keepdims=True), 1e-12)
    _PROJ_CACHE.clear()
    _PROJ_CACHE[cache_key] = (tri, bary)
    return tri, bary


def _generated_layout(guide_obj, out_obj):
    """Rest positions (world) of the generated cards and their card layout, or None."""
    obj_cache = fc._RUNTIME_CACHE.get(guide_obj.name)
    if obj_cache is None or obj_cache.get("vert_t") is None or obj_cache.get("chain_ranges") is None:
        return None
    me = out_obj.data
    n = len(me.vertices)
    if len(obj_cache["vert_t"]) != n:
        return None
    co = np.empty(n * 3, dtype=np.float64)
    if me.shape_keys is not None:
        me.shape_keys.reference_key.data.foreach_get("co", co)
    else:
        me.vertices.foreach_get("co", co)
    mw = np.array(out_obj.matrix_world)
    pts = co.reshape(-1, 3) @ mw[:3, :3].T + mw[:3, 3]
    cards = [(r, False) for r in obj_cache["chain_ranges"]] + [(r, True) for r in (obj_cache.get("mirror_ranges") or [])]
    return pts, np.clip(np.asarray(obj_cache["vert_t"], dtype=np.float64), 0.0, 1.0), cards, obj_cache.get("gen_id")


def _ends(points, t):
    """Root point, tip point and centre of one card (from its root->tip parameter)."""
    root = points[t <= t.min() + 0.05].mean(0)
    tip = points[t >= t.max() - 0.05].mean(0)
    return root, tip, points.mean(0)


# ============================================================================
# Weights
# ============================================================================

def compute_weights(guide_obj, refresh_body=False):
    """Rebuild the generated mesh's vertex groups from the rig settings. Returns a status string."""
    rs = guide_obj.arantools_feather_rig
    out_obj = guide_obj.arantools_feather.output_object
    arm = rs.armature
    if arm is None or arm.type != 'ARMATURE':
        return "Pick a rig (armature) first."
    if out_obj is None or out_obj.data is None or len(out_obj.data.vertices) == 0:
        return "This Feather Object has no generated feathers yet."
    layout = _generated_layout(guide_obj, out_obj)
    if layout is None:
        fc.regenerate_feather_object(guide_obj)
        layout = _generated_layout(guide_obj, out_obj)
        if layout is None:
            return "Generated mesh is out of date — regenerate the feathers."
    pts, vt, cards, gen_id = layout
    vt = fc.base_oriented_t(guide_obj, pts, vt, cards)  # 0 = base (where the arrows start)
    settings = feather_settings(guide_obj)
    n_chains = len(settings["feather_rig_mode"])
    bone_names = [item.name for item in rs.bones]
    deform = {b.name for b in arm.data.bones if b.use_deform} or {b.name for b in arm.data.bones}

    # Body transfer (only when something needs it)
    needs_body = any(
        (settings["feather_rig_mode"][c] == MODE_BODY) or
        (settings["feather_base_follow"][c] > 0.0)
        for c in range(n_chains)
    )
    body = None
    transfer = None
    body_cols = []
    if needs_body and rs.body is not None and rs.body.type == 'MESH':
        if refresh_body:
            _BODY_CACHE.clear()
            _PROJ_CACHE.clear()
        body = _body_data(rs.body, deform)
        body_cols = body[2]
        proj_key = (guide_obj.name, gen_id, rs.body.name, len(pts), tuple(round(x, 5) for x in pts[:3].ravel()))
        tri, bary = _project_on_body(body, pts, proj_key)
        tv, wb = body[0], body[1]
        transfer = (wb[tv[tri, 0]] * bary[:, 0:1] + wb[tv[tri, 1]] * bary[:, 1:2] + wb[tv[tri, 2]] * bary[:, 2:3])

    # Columns: body bones first, chain bones appended as needed
    col_index = {name: i for i, name in enumerate(body_cols)}
    col_names = list(body_cols)

    def col(name):
        if name not in col_index:
            col_index[name] = len(col_names)
            col_names.append(name)
        return col_index[name]

    triples_v, triples_c, triples_w = [], [], []
    k_max = max(1, rs.max_influences)
    refl_world = fc._get_mirror_reflection_matrix(guide_obj.arantools_feather)
    mirror_names = {}
    used_mirror = {}
    unrigged = 0
    for (c_idx, v0, v1, *_), mirrored in cards:
        if not (0 <= c_idx < n_chains):
            continue
        P = pts[v0:v1]
        t = vt[v0:v1]
        mode = settings["feather_rig_mode"][c_idx]
        cols_here = None
        W = None

        if mode == MODE_CHAIN:
            slot = settings["feather_rig_bone"][c_idx]
            start = bone_names[slot] if 0 <= slot < len(bone_names) else ""
            end_slot = settings["feather_rig_end"][c_idx]
            end = bone_names[end_slot] if 0 <= end_slot < len(bone_names) else ""
            if mirrored:
                chain, how = _best_mirrored_chain(arm, deform, start, end,
                                                  int(settings["feather_rig_length"][c_idx]),
                                                  refl_world, mirror_names, P, t)
                if chain:
                    used_mirror[c_idx] = ([b.name for b in chain], how)
            else:
                chain = resolve_feather_chain(arm, start, end, int(settings["feather_rig_length"][c_idx]))
                chain = [b for b in chain if b.name in deform] or chain
            if chain:
                heads, tails = _bone_segments(arm, chain)
                C = _chain_weights(P, heads, tails, float(settings["feather_rig_smooth"][c_idx]))
                cols_here = [col(b.name) for b in chain]
                follow = float(settings["feather_base_follow"][c_idx])
                if follow > 0.0 and transfer is not None and len(body_cols):
                    falloff = max(float(settings["feather_base_falloff"][c_idx]), 1e-4)
                    offset = float(settings["feather_base_offset"][c_idx])
                    x = np.clip((t - offset) / falloff, 0.0, 1.0)
                    b = follow * (1.0 - x * x * (3.0 - 2.0 * x))  # full at the base -> 0 after the falloff
                    W = np.zeros((len(P), len(col_names)))
                    W[:, :len(body_cols)] = transfer[v0:v1] * b[:, None]
                    W[:, cols_here] += C * (1.0 - b)[:, None]
                    cols_here = None
                else:
                    W = C
            else:
                mode = MODE_BODY  # no valid chain: fall back to the body

        if mode == MODE_BODY and W is None:
            if transfer is None or not len(body_cols):
                unrigged += 1
                continue
            T = transfer[v0:v1].astype(np.float64)
            factor = float(settings["feather_rig_factor"][c_idx])
            base = T[t <= t.min() + 0.05].mean(0)
            W = T * (1.0 - factor) + base[None] * factor
            cols_here = list(range(len(body_cols)))

        if cols_here is None:
            cols_here = list(range(W.shape[1]))
        # Top-k influences, normalised
        if W.shape[1] > k_max:
            keep = np.argsort(-W, axis=1)[:, :k_max]
        else:
            keep = np.tile(np.arange(W.shape[1]), (len(W), 1))
        kw = np.take_along_axis(W, keep, axis=1)
        kw[kw < rs.min_weight] = 0.0
        s = kw.sum(1, keepdims=True)
        ok = s[:, 0] > 0
        kw[ok] /= s[ok]
        rows = np.repeat(np.arange(v0, v1), keep.shape[1])
        cols_arr = np.asarray(cols_here)[keep.ravel()]
        wflat = kw.ravel()
        nz = wflat > 0
        triples_v.append(rows[nz])
        triples_c.append(cols_arr[nz])
        triples_w.append(wflat[nz])

    _MIRROR_USED[guide_obj.name] = used_mirror
    _write_vertex_groups(out_obj, col_names, triples_v, triples_c, triples_w)
    _ensure_armature_modifier(out_obj, arm)
    msg = f"Weighted {len(cards) - unrigged} feather cards to '{arm.name}'."
    if unrigged:
        msg += f" {unrigged} Body-mode feathers skipped: set a Body Mesh."
    return msg


def _write_vertex_groups(out_obj, col_names, tv, tc, tw):
    """Replace the generated mesh's vertex groups (the rig owns them) with the given weights."""
    out_obj.vertex_groups.clear()
    if not tv:
        return
    v = np.concatenate(tv)
    c = np.concatenate(tc)
    w = np.round(np.concatenate(tw), 3)
    groups = {}
    order = np.lexsort((w, c))
    v, c, w = v[order], c[order], w[order]
    # One vg.add call per (bone, weight value)
    keys = c.astype(np.int64) * 10000 + np.round(w * 1000).astype(np.int64)
    starts = np.r_[0, np.nonzero(keys[1:] != keys[:-1])[0] + 1, len(keys)]
    for i in range(len(starts) - 1):
        a, b = starts[i], starts[i + 1]
        name = col_names[c[a]]
        vg = groups.get(name)
        if vg is None:
            vg = out_obj.vertex_groups.new(name=name)
            groups[name] = vg
        vg.add(v[a:b].tolist(), float(w[a]), 'REPLACE')


def _ensure_armature_modifier(out_obj, arm):
    if any(m.type == 'ARMATURE' and m.object == arm for m in out_obj.modifiers):
        return
    mod = out_obj.modifiers.new(name="Feather Rig", type='ARMATURE')
    mod.object = arm
    try:
        out_obj.modifiers.move(len(out_obj.modifiers) - 1, 0)  # deform first, the artist's modifiers after
    except Exception:
        pass


def reapply_after_regenerate(guide_obj):
    """Called by the generator after a full rebuild (which wipes vertex weights)."""
    rs = getattr(guide_obj, "arantools_feather_rig", None)
    if rs is None or not rs.live or rs.armature is None:
        return
    try:
        compute_weights(guide_obj)
    except Exception as exc:
        print(f"[AranTools] Feather rig re-weight warning: {exc}")


def _live_update(context):
    guide_obj = fc.get_feather_guide_from_context(context)
    if guide_obj is not None and guide_obj.arantools_feather_rig.live and guide_obj.arantools_feather_rig.armature:
        compute_weights(guide_obj)


# ============================================================================
# Guess chain
# ============================================================================

def _mirror_bone_name(arm_obj, name, refl_world, cache, kind='NAME'):
    """A candidate bone on the other side for a mirror copy.

    kind 'NAME': the flipped name (.L <-> .R, ...) if the rig has that bone, else the same bone.
    kind 'GEOM': the bone whose head and tail match this one's reflected across the feather mirror
                 plane (works with any naming), else the same bone.
    Bones on the mirror plane map to themselves."""
    if not name:
        return name
    key = (kind, name)
    hit = cache.get(key)
    if hit is not None:
        return hit
    bones = arm_obj.data.bones
    result = name
    if kind == 'NAME':
        flipped = _flip_name(name)
        if flipped != name and flipped in bones:
            result = flipped
    elif name in bones:
        mw = arm_obj.matrix_world
        b = bones[name]
        head = refl_world @ (mw @ b.head_local)
        tail = refl_world @ (mw @ b.tail_local)
        best, best_d = None, None
        for other in bones:
            d = ((mw @ other.head_local) - head).length + ((mw @ other.tail_local) - tail).length
            if best_d is None or d < best_d:
                best, best_d = other, d
        tol = 0.25 * ((mw @ b.tail_local) - (mw @ b.head_local)).length + 1e-5
        if best is not None and best_d <= tol:
            result = best.name
    cache[key] = result
    return result


def _best_mirrored_chain(arm, deform, start, end, length, refl_world, cache, P, t):
    """Bone chain for a MIRROR copy of a feather, in this order:

    1. The mirrored NAME (.L <-> .R, _l <-> _r, ...) when the rig has it — authoritative.
    2. Centre-line chains (no side in the name, lying on the mirror plane): the same bones.
    3. Otherwise the mirror copy is treated as a feather of its own: the bones matching its mirrored
       position, or failing that a Guess-style search around the mirror card.
    The weights themselves are always computed from the mirror card's own geometry.
    """
    def clean(chain):
        return [b for b in chain if b.name in deform] or chain

    bones = arm.data.bones
    # 1. Mirrored name
    flipped_start = _flip_name(start)
    if flipped_start != start and flipped_start in bones:
        flipped_end = _flip_name(end) if end else ""
        if flipped_end and flipped_end not in bones:
            flipped_end = ""  # end didn't flip: fall back to the chain length
        chain = clean(resolve_feather_chain(arm, flipped_start, flipped_end, length))
        if chain:
            return chain, "name"

    original = clean(resolve_feather_chain(arm, start, end, length))
    if not original:
        return [], ""

    # 2. Centre-line chain: every bone maps onto itself across the mirror plane
    geom = [_mirror_bone_name(arm, b.name, refl_world, cache, 'GEOM') for b in original]
    if geom == [b.name for b in original] and _flip_name(start) == start and \
            all(_on_mirror_plane(arm, b, refl_world) for b in original):
        return original, "centre"

    # 3a. Bones at the mirrored position
    if all(g in bones for g in geom) and geom != [b.name for b in original]:
        chain = clean(resolve_feather_chain(arm, geom[0], geom[-1], len(geom)))
        if chain:
            return chain, "position"

    # 3b. Search around the mirror card, as for an independent feather
    centre_line = _centerline(P, t)
    feather_len = float(np.linalg.norm(np.diff(centre_line, axis=0), axis=1).sum()) if len(centre_line) > 1 else 1.0
    best, best_score = [], None
    for _score, chain in _search_chains(arm, P, t, methods=('TIP', 'BASE'), limit=24):
        chain = clean(chain)
        score = _chain_fit(arm, chain, centre_line, feather_len) + 0.02 * abs(len(chain) - len(original))
        if best_score is None or score < best_score:
            best, best_score = chain, score
    return best, "search"


def _on_mirror_plane(arm, bone, refl_world):
    mw = arm.matrix_world
    h, tl = mw @ bone.head_local, mw @ bone.tail_local
    tol = 0.05 * (tl - h).length + 1e-4
    return (refl_world @ h - h).length <= tol and (refl_world @ tl - tl).length <= tol


def resolve_feather_chain(arm_obj, start_name, end_name, length):
    """A feather's chain: start bone down to `end_name` when that's a descendant (exact pick from
    Guess / Choose), otherwise `length` bones from the start (0 = to the end of the chain)."""
    bones = arm_obj.data.bones
    end = bones.get(end_name) if end_name else None
    if end is not None and start_name in bones:
        path, b = [], end
        while b is not None and len(path) < 256:
            path.append(b)
            if b.name == start_name:
                return path[::-1]
            b = b.parent
    return resolve_chain(arm_obj, start_name, length)


def _centerline(points, t, bins=10):
    """Feather card centre line: mean point of each root->tip slice."""
    lo, hi = float(t.min()), float(t.max())
    if hi - lo < 1e-9:
        return points.mean(0)[None]
    edges = np.linspace(lo, hi + 1e-9, bins + 1)
    out = [points[(t >= edges[i]) & (t < edges[i + 1])].mean(0) for i in range(bins)
           if ((t >= edges[i]) & (t < edges[i + 1])).any()]
    return np.array(out)


def _polyline_dist(points, poly):
    """Distance from each point to the polyline `poly`."""
    if len(poly) == 1:
        return np.linalg.norm(points - poly[0], axis=1)
    a, b = poly[:-1], poly[1:]
    ab = b - a
    l2 = np.maximum((ab * ab).sum(1), 1e-18)
    u = np.clip(((points[:, None, :] - a[None]) * ab[None]).sum(2) / l2[None], 0.0, 1.0)
    return np.linalg.norm(points[:, None, :] - (a[None] + ab[None] * u[..., None]), axis=2).min(1)


def _chain_fit(arm_obj, chain, centre_line, feather_len):
    """How well a bone chain lines up with a feather (lower = better, relative to its length):
    the feather must be covered by the chain AND the chain must stay on the feather, so chains
    that run past the feather's own bones (into the head, the body, ...) score badly."""
    mw = arm_obj.matrix_world
    joints = np.array([(mw @ b.head_local)[:] for b in chain] + [(mw @ chain[-1].tail_local)[:]])
    covered = _polyline_dist(centre_line, joints).mean()
    on_feather = _polyline_dist(joints, centre_line).mean()
    return float((covered + on_feather) / max(feather_len, 1e-9))


def chain_candidates(guide_obj, c, methods=('TIP', 'BASE', 'CENTER'), limit=12):
    """Ranked candidate chains for feather `c`: [(score, [bone, ...]), ...], best first."""
    rs = guide_obj.arantools_feather_rig
    arm = rs.armature
    layout = _generated_layout(guide_obj, guide_obj.arantools_feather.output_object)
    if arm is None or layout is None:
        return []
    pts, vt, cards, _ = layout
    vt = fc.base_oriented_t(guide_obj, pts, vt, cards)  # 0 = base (where the arrows start)
    rng = next((r for r, mirrored in cards if not mirrored and r[0] == c), None)
    if rng is None:
        return []
    _, v0, v1, *_ = rng
    return _search_chains(arm, pts[v0:v1], vt[v0:v1], methods, limit)


def _search_chains(arm, P, t, methods=('TIP', 'BASE', 'CENTER'), limit=12):
    """Candidate chains for a card (world points P, root->tip t), ranked by how well they line up
    with it: [(score, [bone, ...]), ...], best first."""
    root, tip, centre = _ends(P, t)
    centre_line = _centerline(P, t)
    feather_len = float(np.linalg.norm(np.diff(centre_line, axis=0), axis=1).sum()) if len(centre_line) > 1 else 1.0
    bones = [b for b in arm.data.bones if b.use_deform] or list(arm.data.bones)
    heads, tails = _bone_segments(arm, bones)

    chains = {}

    def add(chain):
        if chain:
            chains[(chain[0].name, chain[-1].name)] = chain

    if 'TIP' in methods:
        for i in np.argsort(np.linalg.norm(tails - tip, axis=1))[:3]:
            path, b = [], bones[int(i)]
            while b is not None and len(path) < 16:
                path.append(b)
                add(path[::-1])  # every ancestor as a possible start
                b = b.parent
    if 'BASE' in methods:
        for i in np.argsort(np.linalg.norm(heads - root, axis=1))[:3]:
            stack = [[bones[int(i)]]]
            found = 0
            while stack and found < 64:
                path = stack.pop()
                add(path)
                found += 1
                if len(path) < 16:
                    stack.extend(path + [k] for k in path[-1].children)
    if 'CENTER' in methods:
        ab = tails - heads
        u = np.clip(((centre - heads) * ab).sum(1) / np.maximum((ab * ab).sum(1), 1e-18), 0.0, 1.0)
        for i in np.argsort(np.linalg.norm(centre - (heads + ab * u[:, None]), axis=1))[:5]:
            add([bones[int(i)]])

    scored = sorted(((_chain_fit(arm, ch, centre_line, feather_len), ch) for ch in chains.values()),
                    key=lambda sc: sc[0])
    return scored[:limit]


def _assign_chain(guide_obj, chain_ids, chain):
    """Store `chain` (list of bones) on the given feathers: start + exact end."""
    rs = guide_obj.arantools_feather_rig
    arm = rs.armature
    to_end = [b.name for b in resolve_chain(arm, chain[0].name, 0)]
    exact = [b.name for b in chain]
    _write_feathers(guide_obj, chain_ids, "feather_rig_mode", MODE_CHAIN)
    _write_feathers(guide_obj, chain_ids, "feather_rig_bone", _bone_slot(rs, chain[0].name))
    if exact == to_end:  # "start, to the end of the chain" — keep the simple form
        _write_feathers(guide_obj, chain_ids, "feather_rig_end", -1)
        _write_feathers(guide_obj, chain_ids, "feather_rig_length", 0)
    else:
        _write_feathers(guide_obj, chain_ids, "feather_rig_end", _bone_slot(rs, chain[-1].name))
        _write_feathers(guide_obj, chain_ids, "feather_rig_length", len(chain))


def guess_chains(guide_obj, chain_ids, method):
    """Give each feather the best-fitting chain found from its tip / base / centre."""
    if guide_obj.arantools_feather_rig.armature is None:
        return 0
    assigned = 0
    for c in chain_ids:
        cands = chain_candidates(guide_obj, c, methods=(method,), limit=1)
        if cands:
            _assign_chain(guide_obj, [c], cands[0][1])
            assigned += 1
    return assigned


# ============================================================================
# Settings + panel proxies
# ============================================================================

def _poll_armature(self, obj):
    return obj is not None and obj.type == 'ARMATURE'


def _poll_mesh(self, obj):
    fprops = getattr(obj, "arantools_feather", None)
    return (
        obj is not None and obj.type == 'MESH'
        and not (fprops and (fprops.is_feather_guide or fprops.is_feather_output))
    )


def _rig_setting_changed(self, context):
    _BODY_CACHE.clear()
    _PROJ_CACHE.clear()
    owner = self.id_data
    if isinstance(owner, bpy.types.Object) and self.live and self.armature is not None:
        compute_weights(owner)


def _rig_tuning_changed(self, context):
    owner = self.id_data
    if isinstance(owner, bpy.types.Object) and self.live and self.armature is not None:
        compute_weights(owner)


class ARANTOOLS_PG_FeatherRigBone(PropertyGroup):
    name: StringProperty()


class ARANTOOLS_PG_FeatherRig(PropertyGroup):
    armature: PointerProperty(
        name="Rig", type=bpy.types.Object, poll=_poll_armature, update=_rig_setting_changed,
        description="Armature the feathers are weighted to",
    )
    body: PointerProperty(
        name="Body Mesh", type=bpy.types.Object, poll=_poll_mesh, update=_rig_setting_changed,
        description="Weighted body mesh whose weights are transferred (Body mode and Base Follows Body)",
    )
    live: BoolProperty(
        name="Live", default=True,
        description="Re-weight immediately when rig settings change (and after the feathers regenerate)",
    )
    max_influences: IntProperty(
        name="Max Influences", default=4, min=1, max=8, update=_rig_tuning_changed,
        description="Most bones that may influence one point (game-engine friendly)",
    )
    min_weight: FloatProperty(
        name="Min Weight", default=0.01, min=0.0, max=0.5, update=_rig_tuning_changed,
        description="Smaller influences are dropped before normalising",
    )
    bones: CollectionProperty(type=ARANTOOLS_PG_FeatherRigBone)


def _targets_and_guide(context):
    guide_obj = fc.get_feather_guide_from_context(context)
    if guide_obj is None:
        return None, []
    return guide_obj, target_feathers(context, guide_obj)


def _write_from_ui(attr, value_fn, clears_end=False):
    def _update(self, context):
        if _SYNCING:
            return
        guide_obj, targets = _targets_and_guide(context)
        if guide_obj is None or not targets:
            return
        _write_feathers(guide_obj, targets, attr, value_fn(self, guide_obj))
        if clears_end:  # a hand-set start / length replaces an exact end from Guess / Choose
            _write_feathers(guide_obj, targets, "feather_rig_end", -1)
        _live_update(context)
    return _update


def _start_bone_value(self, guide_obj):
    return _bone_slot(guide_obj.arantools_feather_rig, self.start_bone) if self.start_bone else -1


class ARANTOOLS_PG_FeatherRigUI(PropertyGroup):
    """Panel proxies showing / editing the selected feathers' rig settings."""
    mode: EnumProperty(
        name="Rig Mode",
        items=[
            ('CHAIN', "Chain", "Weight the feather along a bone chain"),
            ('BODY', "Body", "Transfer weights from the body mesh"),
        ],
        default='BODY',
        update=_write_from_ui("feather_rig_mode", lambda s, g: MODE_CHAIN if s.mode == 'CHAIN' else MODE_BODY),
    )
    start_bone: StringProperty(
        name="Start Bone", update=_write_from_ui("feather_rig_bone", _start_bone_value, clears_end=True),
        description="First bone of the chain",
    )
    chain_length: IntProperty(
        name="Chain Length", default=0, min=0, soft_max=32,
        update=_write_from_ui("feather_rig_length", lambda s, g: s.chain_length, clears_end=True),
        description="Bones in the chain, from the start bone down (0 = to the end of the chain)",
    )
    smoothness: FloatProperty(
        name="Smoothness", default=0.35, min=0.0, soft_max=2.0, max=4.0,
        update=_write_from_ui("feather_rig_smooth", lambda s, g: s.smoothness),
        description="How far each point's weight spreads over neighbouring bones of the chain (in bones). 0 = hard segments",
    )
    factor: FloatProperty(
        name="Base Weight", default=0.0, min=0.0, max=1.0, subtype='FACTOR',
        update=_write_from_ui("feather_rig_factor", lambda s, g: s.factor),
        description="0 = every point takes the body weight under it; 1 = the whole feather takes the weight at its base",
    )
    base_follow: FloatProperty(
        name="Base Follows Body", default=0.0, min=0.0, max=1.0, subtype='FACTOR',
        update=_write_from_ui("feather_base_follow", lambda s, g: s.base_follow),
        description="How much the feather's base takes the body's weights instead of the chain",
    )
    base_falloff: FloatProperty(
        name="Falloff", default=0.3, min=0.0, max=1.0, subtype='FACTOR',
        update=_write_from_ui("feather_base_falloff", lambda s, g: s.base_falloff),
        description="Fraction of the feather over which Base Follows Body fades out",
    )
    base_offset: FloatProperty(
        name="Offset", default=0.0, min=0.0, max=1.0, subtype='FACTOR',
        update=_write_from_ui("feather_base_offset", lambda s, g: s.base_offset),
        description="Base Follows Body is at full strength up to this fraction of the feather",
    )


def sync_rig_ui(context):
    """Show the first targeted feather's rig settings in the panel proxies."""
    global _SYNCING
    guide_obj, targets = _targets_and_guide(context)
    ui = getattr(context.scene, "arantools_feather_rig_ui", None)
    if guide_obj is None or ui is None or not targets:
        return
    st = feather_settings(guide_obj)
    c = targets[0]
    if not (0 <= c < len(st["feather_rig_mode"])):
        return
    rs = guide_obj.arantools_feather_rig
    slot = int(st["feather_rig_bone"][c])
    _SYNCING = True
    try:
        ui.mode = 'CHAIN' if st["feather_rig_mode"][c] == MODE_CHAIN else 'BODY'
        ui.start_bone = rs.bones[slot].name if 0 <= slot < len(rs.bones) else ""
        ui.chain_length = int(st["feather_rig_length"][c])
        ui.smoothness = float(st["feather_rig_smooth"][c])
        ui.factor = float(st["feather_rig_factor"][c])
        ui.base_follow = float(st["feather_base_follow"][c])
        ui.base_falloff = float(st["feather_base_falloff"][c])
        ui.base_offset = float(st["feather_base_offset"][c])
    finally:
        _SYNCING = False


# ============================================================================
# Operators
# ============================================================================

_CHOICES = []  # EnumProperty items must stay referenced while the popup is open


def _choice_items(self, context):
    return _CHOICES or [('NONE', "No candidate chains", "")]


def _chain_label(chain):
    names = [b.name for b in chain]
    if len(names) > 4:
        names = names[:2] + ["…"] + names[-1:]
    return " › ".join(names) + f"   ({len(chain)} bone{'s' if len(chain) != 1 else ''})"


class ARANTOOLS_OT_feather_rig_choose(Operator):
    bl_idname = "arantools.feather_rig_choose"
    bl_label = "Choose Chain"
    bl_description = "Pick the chain from a ranked list of candidates (best fit to the feather first)"
    bl_options = {'REGISTER', 'UNDO'}

    choice: EnumProperty(name="Chain", items=_choice_items)

    @classmethod
    def poll(cls, context):
        g = fc.get_feather_guide_from_context(context)
        return g is not None and g.arantools_feather_rig.armature is not None

    def invoke(self, context, event):
        global _CHOICES
        guide_obj, targets = _targets_and_guide(context)
        if not targets:
            self.report({'WARNING'}, "Select a feather first.")
            return {'CANCELLED'}
        self._cands = chain_candidates(guide_obj, targets[0])
        _CHOICES = [
            (f"{i}", _chain_label(ch), f"Fit {score:.3f} (lower is better)")
            for i, (score, ch) in enumerate(self._cands)
        ]
        if not _CHOICES:
            self.report({'WARNING'}, "No candidate chains near this feather.")
            return {'CANCELLED'}
        self.choice = _CHOICES[0][0]
        return context.window_manager.invoke_props_dialog(self, width=460)

    def draw(self, context):
        guide_obj, targets = _targets_and_guide(context)
        col = self.layout.column()
        note = "Best fit first"
        if len(targets) > 1:
            note += f" — found for the first of {len(targets)} selected; the pick applies to all of them"
        col.label(text=note, icon='INFO')
        col.prop(self, "choice", expand=True)

    def execute(self, context):
        guide_obj, targets = _targets_and_guide(context)
        cands = getattr(self, "_cands", None) or chain_candidates(guide_obj, targets[0]) if targets else []
        try:
            chain = cands[int(self.choice)][1]
        except (ValueError, IndexError):
            return {'CANCELLED'}
        _assign_chain(guide_obj, targets, chain)
        sync_rig_ui(context)
        _live_update(context)
        self.report({'INFO'}, f"{chain[0].name} → {chain[-1].name} on {len(targets)} feather(s).")
        return {'FINISHED'}


class ARANTOOLS_OT_feather_rig_bake(Operator):
    bl_idname = "arantools.feather_rig_bake"
    bl_label = "Bake Feather Weights"
    bl_description = (
        "Rebuild the generated feathers' vertex groups from the rig settings (re-reads the body "
        "mesh's weights). Adds an Armature modifier for the rig if there is none"
    )
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        g = fc.get_feather_guide_from_context(context)
        return g is not None and g.arantools_feather_rig.armature is not None

    def execute(self, context):
        msg = compute_weights(fc.get_feather_guide_from_context(context), refresh_body=True)
        self.report({'INFO'}, msg)
        return {'FINISHED'}


class ARANTOOLS_OT_feather_rig_guess(Operator):
    bl_idname = "arantools.feather_rig_guess"
    bl_label = "Guess Chain"
    bl_description = "Give the selected feathers the chain that best matches their card mesh"
    bl_options = {'REGISTER', 'UNDO'}

    method: EnumProperty(
        items=[
            ('TIP', "Tip", "Chains ending near the feather's tip; the one that best lines up with the feather wins"),
            ('BASE', "Base", "Chains starting near the feather's base; the one that best lines up with the feather wins"),
            ('CENTER', "Center", "The single bone that best lines up with the feather"),
        ],
        default='TIP',
    )

    @classmethod
    def poll(cls, context):
        g = fc.get_feather_guide_from_context(context)
        return g is not None and g.arantools_feather_rig.armature is not None

    def execute(self, context):
        guide_obj, targets = _targets_and_guide(context)
        if not targets:
            self.report({'WARNING'}, "Select one or more feathers first.")
            return {'CANCELLED'}
        n = guess_chains(guide_obj, targets, self.method)
        sync_rig_ui(context)
        _live_update(context)
        self.report({'INFO'}, f"Matched {n} feather(s) by {self.method.lower()}.")
        return {'FINISHED'}


# ============================================================================
# Panel
# ============================================================================

def draw_rig_ui(layout, context, guide_obj):
    rs = guide_obj.arantools_feather_rig
    ui = context.scene.arantools_feather_rig_ui

    setup = layout.box()
    col = setup.column(align=True)
    col.label(text="Rig Setup", icon='ARMATURE_DATA')
    col.prop(rs, "armature", text="Rig")
    col.prop(rs, "body", text="Body Mesh")
    col.separator(factor=0.4)
    row = col.row(align=True)
    row.scale_y = 1.3
    row.operator("arantools.feather_rig_bake", text="Bake Weights", icon='MOD_VERTEX_WEIGHT')
    row.prop(rs, "live", text="Live", toggle=True, icon='AUTO')
    col.separator(factor=0.3)
    col.prop(rs, "max_influences")
    col.prop(rs, "min_weight")
    if rs.armature is None:
        return

    targets = target_feathers(context, guide_obj)
    box = layout.box()
    fcol = box.column(align=True)
    if fc.is_in_feather_mode(guide_obj):
        title = f"{len(targets)} selected feather{'s' if len(targets) != 1 else ''}"
    else:
        title = f"All feathers ({len(targets)})"
    fcol.label(text=title, icon='STRANDS')
    if not targets:
        hint = fcol.row()
        hint.enabled = False
        hint.label(text="Pick or select feathers to set their rig")
        return

    st = feather_settings(guide_obj)
    modes = {int(st["feather_rig_mode"][c]) for c in targets if 0 <= c < len(st["feather_rig_mode"])}
    if len(modes) > 1:
        mixed = fcol.row()
        mixed.enabled = False
        mixed.label(text="Mixed modes — showing the first feather", icon='INFO')

    fcol.separator(factor=0.3)
    fcol.row(align=True).prop(ui, "mode", expand=True)
    fcol.separator(factor=0.4)

    if ui.mode == 'CHAIN':
        guess = fcol.row(align=True)
        guess.label(text="Guess:")
        for method, text, icon in (('TIP', "Tip", 'TRIA_UP'), ('BASE', "Base", 'TRIA_DOWN'), ('CENTER', "Center", 'PIVOT_MEDIAN')):
            guess.operator("arantools.feather_rig_guess", text=text, icon=icon).method = method
        guess.operator("arantools.feather_rig_choose", text="Choose…", icon='OUTLINER_DATA_ARMATURE')
        fcol.separator(factor=0.3)
        fcol.prop_search(ui, "start_bone", rs.armature.data, "bones", text="Start Bone")
        end_slot = int(st["feather_rig_end"][targets[0]]) if targets[0] < len(st["feather_rig_end"]) else -1
        if 0 <= end_slot < len(rs.bones):
            end_row = fcol.row(align=True)
            end_row.label(text=f"Ends at: {rs.bones[end_slot].name}", icon='BONE_DATA')
            end_row.label(text="(edit length to change)")
        else:
            fcol.prop(ui, "chain_length", text="Chain Length (0 = to end)")
        fcol.prop(ui, "smoothness", slider=True)
        used = _MIRROR_USED.get(guide_obj.name, {}).get(targets[0])
        if used is not None and guide_obj.arantools_feather.use_mirror:
            names, how = used
            shown = names if len(names) <= 3 else [names[0], "\u2026", names[-1]]
            how_text = {"name": "mirrored name", "centre": "centre bones", "position": "mirrored position",
                        "search": "own best fit"}.get(how, how)
            m_row = fcol.row()
            m_row.enabled = False
            m_row.label(text=f"Mirror copy: {' \u203a '.join(shown)}  ({how_text})", icon='MOD_MIRROR')

        follow = box.column(align=True)
        follow.label(text="Base Follows Body", icon='MOD_DATA_TRANSFER')
        if rs.body is None:
            no_body = follow.row()
            no_body.enabled = False
            no_body.label(text="Set a Body Mesh above to use this")
        sub = follow.column(align=True)
        sub.enabled = rs.body is not None
        sub.prop(ui, "base_follow", text="Amount", slider=True)
        sub.prop(ui, "base_falloff", slider=True)
        sub.prop(ui, "base_offset", slider=True)
    else:
        if rs.body is None:
            no_body = fcol.row()
            no_body.alert = True
            no_body.label(text="Set a Body Mesh above", icon='ERROR')
        fcol.prop(ui, "factor", text="Base Weight", slider=True)


# ============================================================================
# Registration
# ============================================================================

classes = (
    ARANTOOLS_PG_FeatherRigBone,
    ARANTOOLS_PG_FeatherRig,
    ARANTOOLS_PG_FeatherRigUI,
    ARANTOOLS_OT_feather_rig_bake,
    ARANTOOLS_OT_feather_rig_guess,
    ARANTOOLS_OT_feather_rig_choose,
)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)
    bpy.types.Object.arantools_feather_rig = PointerProperty(type=ARANTOOLS_PG_FeatherRig)
    bpy.types.Scene.arantools_feather_rig_ui = PointerProperty(type=ARANTOOLS_PG_FeatherRigUI)


def unregister():
    _BODY_CACHE.clear()
    _PROJ_CACHE.clear()
    for attr in ("arantools_feather_rig",):
        if hasattr(bpy.types.Object, attr):
            delattr(bpy.types.Object, attr)
    if hasattr(bpy.types.Scene, "arantools_feather_rig_ui"):
        del bpy.types.Scene.arantools_feather_rig_ui
    for cls in reversed(classes):
        try:
            bpy.utils.unregister_class(cls)
        except Exception:
            pass
