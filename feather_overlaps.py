"""
AranTools — Feather "Fix Overlaps"
==================================
Mesh-based relaxation of the generated feather cards so feathers stop going through each other
(especially toward the tips) and out of collision meshes, while staying as close as possible to
the generated shape. The guide (vertices, attributes, parameters) is never touched: the result is
written to a shape key on the generated mesh, which `feather_cards` keeps across regeneration.

Solver (position based, per iteration):
  1. Optional momentum ("wiggle") from the previous step.
  2. Shape retention: every vertex is pulled back toward its rest position; strength fades from
     root (stiff) to tip (free) — and the base can be pinned outright.
  3. Displacement smoothing along card edges, so feathers bend as a whole instead of crumpling.
  4. Edge-length constraints (cards don't stretch).
  5. Feather-vs-feather contacts: every vertex near another card is pushed to the side of that card
     where it belongs, keeping a minimum gap. "Where it belongs" comes from one stacking order of
     all feathers, read once from where they overlap near their ROOTS (so tips that went through a
     neighbour are pushed back) — a single order, so no stack of feathers can ask for "A over B
     over C over A", which can never be satisfied and makes the cards fight forever.
  6. Collision meshes (a collection): vertices are pushed out along the surface normal.
  7. Pins and max-offset clamp.
  8. Mirror: a feather and its built-in mirror copy are kept exact mirror images (averaged every
     step) and on their own sides of the mirror plane ("clipping"), so both sides of the bird get
     the same result and a feather never fights its own copy across the centre line.
Contacts are found with a vectorised uniform-grid broad phase and solved with vectorised
point-triangle tests — no per-vertex Python loops — and the work is split into time-boxed chunks
so Blender stays responsive while it runs.
"""

import time

import bpy
import numpy as np
from bpy.props import BoolProperty, FloatProperty, IntProperty, PointerProperty, StringProperty
from bpy.types import Operator, PropertyGroup
from mathutils import Vector
from mathutils.bvhtree import BVHTree

from . import feather_cards as fc


# Live run state per guide object name (read by the panel for the progress bar)
_RUNS = {}


def _tag_redraw_all():
    wm = bpy.context.window_manager
    for win in wm.windows if wm else []:
        for area in win.screen.areas:
            if area.type in {'VIEW_3D', 'PROPERTIES'}:
                area.tag_redraw()


# ============================================================================
# Settings
# ============================================================================

class ARANTOOLS_PG_FixOverlaps(PropertyGroup):
    # ── Shape retention ──
    pin_base: BoolProperty(
        name="Pin Base", default=False,
        description="Feather bases don't move at all. Off by default: neighbours whose bases already cross "
                    "can then be separated too (Root Stiffness still keeps the bases within millimetres)",
    )
    pin_length: FloatProperty(
        name="Pinned Length", default=0.15, min=0.0, max=1.0, subtype='FACTOR',
        description="Fraction of each feather, from the root, that stays pinned",
    )
    root_stiffness: FloatProperty(
        name="Root Stiffness", default=0.35, min=0.0, max=1.0, subtype='FACTOR',
        description="How strongly points near the root are pulled back to the original shape each step",
    )
    tip_stiffness: FloatProperty(
        name="Tip Stiffness", default=0.02, min=0.0, max=1.0, subtype='FACTOR',
        description="How strongly points at the tip are pulled back to the original shape each step",
    )
    stiffness_falloff: FloatProperty(
        name="Falloff", default=1.5, min=0.1, max=8.0,
        description="Root -> tip stiffness curve (higher = stiffness drops off sooner, freer tips)",
    )
    smoothing: FloatProperty(
        name="Bend Smoothing", default=0.5, min=0.0, max=1.0, subtype='FACTOR',
        description="Spread each correction along the card so feathers bend smoothly instead of denting",
    )
    edge_stiffness: FloatProperty(
        name="Edge Stiffness", default=0.8, min=0.0, max=1.0, subtype='FACTOR',
        description="How strictly card edges keep their length (prevents stretching)",
    )
    edge_iterations: IntProperty(
        name="Edge Passes", default=2, min=0, max=10,
        description="Edge-length passes per iteration (more = stiffer cards, slower)",
    )
    max_offset_rel: FloatProperty(
        name="Max Offset", default=0.6, min=0.0, soft_max=2.0,
        description="No point moves further than this from its original position, in feather lengths "
                    "(0 = unlimited)",
    )

    # ── Feather contacts ──
    gap_rel: FloatProperty(
        name="Gap", default=0.01, min=0.0, soft_max=0.1, precision=3,
        description="Minimum distance kept between feathers, in feather lengths",
    )
    search_rel: FloatProperty(
        name="Search Distance", default=0.1, min=0.001, soft_max=1.0, precision=3,
        description="How far to look for nearby feathers each step, in feather lengths. Cards that cross "
                    "each other are always found exactly, however deep — this only covers near-misses "
                    "(larger = slower)",
    )
    collision_strength: FloatProperty(
        name="Push Strength", default=1.0, min=0.0, max=1.0, subtype='FACTOR',
        description="Fraction of each overlap resolved per iteration (lower = softer, more iterations needed)",
    )

    # ── Collision meshes ──
    use_collision: BoolProperty(
        name="Collision Meshes", default=False,
        description="Also keep feathers outside the meshes in the chosen collection",
    )
    collision_collection: PointerProperty(
        name="Collision Collection", type=bpy.types.Collection,
        description="Meshes the feathers must stay outside of (normals should point outward)",
    )
    collision_gap_rel: FloatProperty(
        name="Collision Gap", default=0.005, min=0.0, soft_max=0.1, precision=3,
        description="Minimum distance kept from collision meshes, in feather lengths",
    )

    # ── Solver ──
    iterations: IntProperty(
        name="Iterations", default=120, min=1, soft_max=2000,
        description="Simulation steps",
    )
    momentum: FloatProperty(
        name="Wiggle", default=0.3, min=0.0, max=0.95, subtype='FACTOR',
        description="Carry-over of movement between steps (physics-like wiggle; 0 = pure relaxation)",
    )
    tolerance_rel: FloatProperty(
        name="Stop When Settled", default=0.0002, min=0.0, soft_max=0.01, precision=5,
        description="Stop early once nothing overlaps and no point moves more than this per step, "
                    "in feather lengths (0 = never)",
    )
    cleanup_passes: IntProperty(
        name="Cleanup Passes", default=80, min=0, max=500,
        description="Extra passes at the end that keep pushing until no two cards intersect "
                    "(stops early as soon as none do)",
    )
    full_scan_every: IntProperty(
        name="Contact Scan Every", default=5, min=1, max=50,
        description=(
            "Search for new contacts every N iterations (the slow part); in between, known contacts are "
            "solved at full speed. Higher = faster, but fast-moving tips may slip through"
        ),
    )
    time_budget_ms: IntProperty(
        name="Time Slice (ms)", default=40, min=5, max=500,
        description="Work per UI tick. Lower keeps Blender smoother, higher finishes sooner",
    )
    live_every: IntProperty(
        name="Live Update Every", default=1, min=1, max=100,
        description="Show the result in the viewport every N iterations",
    )
    start_from_current: BoolProperty(
        name="Continue From Current Result", default=False,
        description="Start from the existing Fix Overlaps shape key instead of the original shape",
    )
    shape_key_name: StringProperty(
        name="Shape Key", default="Fix Overlaps",
        description="Shape key on the generated mesh that receives the result",
    )
    show_settings: BoolProperty(name="Settings", default=False)


def get_settings(guide_obj):
    return guide_obj.arantools_fix_overlaps


# ============================================================================
# Simulation
# ============================================================================

def _card_layout(guide_obj, out_obj):
    """Per-vertex card id, per-polygon card id and per-vertex root->tip t, for the generated mesh."""
    obj_cache = fc._RUNTIME_CACHE.get(guide_obj.name)
    if obj_cache is None or obj_cache.get("vert_t") is None or obj_cache.get("chain_ranges") is None:
        fc.regenerate_feather_object(guide_obj)
        obj_cache = fc._RUNTIME_CACHE.get(guide_obj.name)
    ranges = list(obj_cache.get("chain_ranges") or []) + list(obj_cache.get("mirror_ranges") or [])
    n_v = len(out_obj.data.vertices)
    n_p = len(out_obj.data.polygons)
    vert_card = np.full(n_v, -1, dtype=np.int32)
    poly_card = np.full(n_p, -1, dtype=np.int32)
    for card_id, (_, v0, v1, p0, p1, _, _) in enumerate(ranges):
        vert_card[v0:min(v1, n_v)] = card_id
        poly_card[p0:min(p1, n_p)] = card_id
    vert_t = obj_cache["vert_t"]
    if len(vert_t) != n_v or (vert_card < 0).any():
        raise RuntimeError("Generated mesh is out of date — regenerate the feathers and try again.")
    card_chain = np.array([rng[0] for rng in ranges], dtype=np.int64)
    return vert_card, poly_card, np.asarray(vert_t, dtype=np.float64), len(ranges), obj_cache.get("gen_id"), card_chain


def _hidden_points(guide_obj, out_obj, vert_card, card_chain):
    """Generated points that must not move: hidden on the generated mesh, or belonging to a feather
    whose guide spline is hidden (isolated / H in Feather Mode). They still block other feathers."""
    hidden = np.zeros(len(vert_card), dtype=bool)
    out_hide = np.zeros(len(out_obj.data.vertices), dtype=bool)
    out_obj.data.vertices.foreach_get("hide", out_hide)
    hidden |= out_hide

    if guide_obj.mode == 'EDIT':
        import bmesh
        bm = bmesh.from_edit_mesh(guide_obj.data)
        g_hide = np.array([v.hide for v in bm.verts], dtype=bool)
    else:
        g_hide = np.zeros(len(guide_obj.data.vertices), dtype=bool)
        guide_obj.data.vertices.foreach_get("hide", g_hide)
    if g_hide.any():
        chains = fc._get_guide_chains_fast(guide_obj)
        chain_hidden = np.array([bool(g_hide[list(vc)].any()) if vc else False for vc, _ in chains], dtype=bool)
        card_hidden = chain_hidden[np.clip(card_chain, 0, len(chain_hidden) - 1)]
        hidden |= card_hidden[vert_card]
    return hidden


def _collision_geometry(settings, out_obj, depsgraph):
    """Triangles of every mesh in the collision collection, in the generated mesh's local space.

    Returns `(verts (V, 3), tris (T, 3))` or None.
    """
    coll = settings.collision_collection
    if not settings.use_collision or coll is None:
        return None
    to_local = out_obj.matrix_world.inverted_safe()
    verts, tris = [], []
    for obj in coll.all_objects:
        if obj.type != 'MESH' or obj == out_obj:
            continue
        fprops = getattr(obj, "arantools_feather", None)
        if fprops is not None and (fprops.is_feather_guide or fprops.is_feather_output):
            continue
        obj_eval = obj.evaluated_get(depsgraph)
        mesh = obj_eval.to_mesh()
        try:
            mesh.calc_loop_triangles()
            xf = to_local @ obj.matrix_world
            ofs = len(verts)
            verts.extend((xf @ v.co)[:] for v in mesh.vertices)
            tris.extend((ofs + a, ofs + b, ofs + c) for a, b, c in (lt.vertices for lt in mesh.loop_triangles))
        finally:
            obj_eval.to_mesh_clear()
    if not tris:
        return None
    return np.asarray(verts, dtype=np.float64), np.asarray(tris, dtype=np.int64)


def _scatter_add(idx, vals, n):
    """`np.add.at(out, idx, vals)` for (k, 3) vectors, via bincount (many times faster)."""
    return np.stack([np.bincount(idx, weights=vals[:, k], minlength=n) for k in range(3)], axis=1)


def _projects_inside(p, a, b, c, tol=0.02):
    """Whether each point's projection onto its triangle's plane lands inside the triangle."""
    e1, e2, q = b - a, c - a, p - a
    nrm = np.cross(e1, e2)
    ln = np.maximum(np.linalg.norm(nrm, axis=1), 1e-14)
    q = q - nrm * (np.einsum('ij,ij->i', q, nrm) / (ln * ln))[:, None]
    d00, d01, d11 = (e1 * e1).sum(1), (e1 * e2).sum(1), (e2 * e2).sum(1)
    d20, d21 = (q * e1).sum(1), (q * e2).sum(1)
    den = d00 * d11 - d01 * d01
    ok = np.abs(den) > 1e-24
    den = np.where(ok, den, 1.0)
    bv = (d11 * d20 - d01 * d21) / den
    bw = (d00 * d21 - d01 * d20) / den
    return ok & (bv >= -tol) & (bw >= -tol) & (1.0 - bv - bw >= -tol)


def _body_object(guide_obj):
    """The body the feathers grow from: the Rig tab's Body, else the Surface / Projection mesh."""
    rs = getattr(guide_obj, "arantools_feather_rig", None)
    for obj in (getattr(rs, "body", None), guide_obj.arantools_feather.surface_object):
        if obj is not None and obj.type == 'MESH' and len(obj.data.polygons):
            return obj
    return None


def _card_up_sign(guide_obj, out_obj, cards, card_normal, rest, t, pair_a, pair_b):
    """±1 per card: the sign that turns the card's average normal OUTWARD, away from the body.

    Neighbouring cards are first oriented to agree with each other (through the most parallel
    neighbours first, so a fan of tilted wing feathers stays one consistent stack). Only neighbours
    facing the same way are linked: cards facing opposite ways (e.g. upright crest feathers back to
    back on the mirror line) are two sides, not one stack. Then each group of
    connected cards takes the side its cards vote for: the body's rest surface under each feather's
    base (cards lying flat on the body vote strongest — on a wing's arm tube the surface normal
    points every which way), or the feathers' Up attribute when there's no body (Rig tab Body or
    Surface mesh). `pair_a` / `pair_b`: neighbouring cards. `rest` / `t` per generated vertex."""
    n_c = len(cards)
    nu = card_normal / np.maximum(np.linalg.norm(card_normal, axis=1), 1e-14)[:, None]
    vote = _card_up_sign_from_attr(guide_obj, out_obj, cards, card_normal) * 0.02
    body = _body_object(guide_obj)
    if body is not None:
        me = body.data
        co = np.empty(len(me.vertices) * 3, dtype=np.float64)
        me.vertices.foreach_get("co", co)   # undeformed: rest pose
        xf = np.array(out_obj.matrix_world.inverted_safe() @ body.matrix_world)
        verts = co.reshape(-1, 3) @ xf[:3, :3].T + xf[:3, 3]
        bvh = BVHTree.FromPolygons(verts.tolist(), [p.vertices[:] for p in me.polygons])
        for i, ((_, v0, v1, *_), _m) in enumerate(cards):
            tt = t[v0:v1]
            acc = np.zeros(3)
            for p in rest[v0:v1][tt <= tt.min() + 0.35]:
                hit = bvh.find_nearest(p)
                if hit[1] is not None:
                    acc += np.array(hit[1][:])
            ln = np.linalg.norm(acc)
            if ln > 1e-12:
                dot = float(nu[i] @ acc) / ln
                vote[i] += dot * abs(dot)   # flat on the body: strong; standing off it: ~nothing

    # Orient neighbours consistently: maximum spanning forest on n_a . n_b (Prim)
    import heapq
    adj = [dict() for _ in range(n_c)]
    for a, b in zip(np.asarray(pair_a).tolist(), np.asarray(pair_b).tolist()):
        d = float(nu[a] @ nu[b])
        if a != b and d > 0.2:
            adj[a][b] = d
            adj[b][a] = d
    rel = np.zeros(n_c)        # orientation relative to the component's root
    comp = np.full(n_c, -1)
    for root in np.argsort(-np.abs(vote)).tolist():
        if comp[root] >= 0:
            continue
        comp[root] = root
        rel[root] = 1.0
        heap = [(-d, root, b) for b, d in adj[root].items()]
        heapq.heapify(heap)
        while heap:
            _w, a, b = heapq.heappop(heap)
            if comp[b] >= 0:
                continue
            comp[b] = root
            rel[b] = rel[a]
            for c, d in adj[b].items():
                if comp[c] < 0:
                    heapq.heappush(heap, (-d, b, c))
    flip = {}
    for c in range(n_c):
        flip[comp[c]] = flip.get(comp[c], 0.0) + rel[c] * vote[c]
    return np.array([rel[c] * (1.0 if flip[comp[c]] >= 0.0 else -1.0) for c in range(n_c)])


def _card_up_sign_from_attr(guide_obj, out_obj, cards, card_normal):
    """±1 per card: the sign that turns the card's average normal toward the feather's Up attribute.
    Cards without an Up stay +1."""
    sign = np.ones(len(cards))
    me = guide_obj.data
    if guide_obj.mode == 'EDIT':
        import bmesh
        bm = bmesh.from_edit_mesh(me)
        layer = bm.edges.layers.float_vector.get("feather_up")
        if layer is None:
            return sign
        up = np.array([e[layer][:] for e in bm.edges], dtype=np.float64).reshape(-1, 3)
    else:
        attr = me.attributes.get("feather_up")
        if attr is None or attr.domain != 'EDGE':
            return sign
        up = np.empty(len(me.edges) * 3, dtype=np.float64)
        attr.data.foreach_get("vector", up)
        up = up.reshape(-1, 3)
    chains = fc._get_guide_chains_fast(guide_obj)
    fprops = guide_obj.arantools_feather
    to_world = np.array(guide_obj.matrix_world.to_3x3())
    refl = np.array(fc._get_mirror_reflection_matrix(fprops).to_3x3()) if fprops.use_mirror else None
    to_out = np.array(out_obj.matrix_world.inverted_safe().to_3x3())
    for i, ((c_idx, *_), mirrored) in enumerate(cards):
        if not 0 <= c_idx < len(chains):
            continue
        e_chain = [ei for ei in chains[c_idx][1] if ei < len(up)]
        if not e_chain:
            continue
        u = to_world @ up[e_chain].mean(axis=0)
        if mirrored and refl is not None:
            u = refl @ u
        if float(card_normal[i] @ (to_out @ u)) < 0.0:
            sign[i] = -1.0
    return sign


def _layer_order(card_a, card_b, votes, n_cards):
    """One stacking order for all cards from pairwise votes (`votes[k]` > 0: card_a[k] lies over
    card_b[k]). Returns a rank per card, higher = on top.

    Votes are summed per pair, then ordered with the greedy feedback-arc-set heuristic (Eades et al.):
    feathers nothing lies over go on top, feathers lying over nothing go to the bottom, and when only
    cycles are left ("A over B over C over A", common where a dense coat is tangled at rest) the
    feather with the most weight on top breaks the cycle. A consistent order is always solvable."""
    out_w = [dict() for _ in range(n_cards)]   # out_w[a][b]: weight of "a over b"
    in_w = [dict() for _ in range(n_cards)]
    if len(votes):
        lo = np.minimum(card_a, card_b).astype(np.int64)
        hi = np.maximum(card_a, card_b).astype(np.int64)
        signed = np.where(card_a == lo, votes, -votes)   # + = lower index on top
        keys, inv = np.unique(lo * n_cards + hi, return_inverse=True)
        tot = np.bincount(inv, weights=signed)
        for key, w in zip(keys.tolist(), tot.tolist()):
            if w == 0.0:
                continue
            a, b = divmod(key, n_cards)
            if w < 0.0:
                a, b = b, a
            out_w[a][b] = abs(w)
            in_w[b][a] = abs(w)

    alive = set(range(n_cards))
    top, bottom = [], []

    def remove(v):
        alive.discard(v)
        for u in out_w[v]:
            in_w[u].pop(v, None)
        for u in in_w[v]:
            out_w[u].pop(v, None)
        nbrs = list(out_w[v]) + list(in_w[v])
        out_w[v], in_w[v] = {}, {}
        return nbrs

    work = list(range(n_cards))
    while alive:
        while work:
            v = work.pop()
            if v not in alive:
                continue
            if not out_w[v]:
                bottom.append(v)
            elif not in_w[v]:
                top.append(v)
            else:
                continue
            work.extend(remove(v))
        if alive:
            v = max(alive, key=lambda c: sum(out_w[c].values()) - sum(in_w[c].values()))
            top.append(v)
            work.extend(remove(v))
    order = top + bottom[::-1]   # top first
    rank = np.empty(n_cards, dtype=np.int64)
    rank[np.array(order, dtype=np.int64)] = np.arange(n_cards, 0, -1)
    return rank


def _point_triangle_push(p, a, b, c, side, gap, depth, weight, direction=None):
    """Vectorised: for points `p` against triangles (a, b, c), the push that puts each point on
    `side` of its triangle at least `gap` away — zero where the point doesn't project onto the
    triangle, is already fine, or is deeper than `depth` (not a contact). See `_push_along` for
    `direction`."""
    e1 = b - a
    e2 = c - a
    nrm = np.cross(e1, e2)
    ln = np.linalg.norm(nrm, axis=1)
    ok = ln > 1e-14
    nrm[ok] /= ln[ok, None]
    d = p - a
    sd = np.einsum('ij,ij->i', d, nrm)
    q = d - nrm * sd[:, None]
    d00 = np.einsum('ij,ij->i', e1, e1)
    d01 = np.einsum('ij,ij->i', e1, e2)
    d11 = np.einsum('ij,ij->i', e2, e2)
    d20 = np.einsum('ij,ij->i', q, e1)
    d21 = np.einsum('ij,ij->i', q, e2)
    den = d00 * d11 - d01 * d01
    ok &= np.abs(den) > 1e-24
    den[~ok] = 1.0
    bv = (d11 * d20 - d01 * d21) / den
    bw = (d00 * d21 - d01 * d20) / den
    bu = 1.0 - bv - bw
    tol = 0.02  # a little slack so points over triangle seams aren't missed
    inside = ok & (bu >= -tol) & (bv >= -tol) & (bw >= -tol)
    s_sd = side * sd
    hit = inside & (s_sd < gap) & (s_sd > -depth)
    return hit, _push_along(nrm, side * gap - sd, weight, direction)


def _push_along(nrm, need, weight, direction=None):
    """Displacement that changes each point's signed distance along `nrm` by `need` (× weight).

    Straight along the triangle normal by default. With `direction` (unit, per pair) the point moves
    along it instead — the stacking direction, out of / into the coat — by however much that takes:
    on steeply tilted cards the face normal points sideways, and pushing along it shoves the point
    into the next feather over. Falls back to the normal where `direction` is too oblique."""
    if direction is None:
        return nrm * (need * weight)[:, None]
    cos = np.einsum('ij,ij->i', direction, nrm) * np.sign(need)
    use = cos > 0.35
    out = nrm * (need * weight)[:, None]
    out[use] = direction[use] * (np.abs(need[use]) * weight[use] / cos[use])[:, None]
    return out


def _grid_candidates(x, tri_v, tri_card, points, point_card, r, tri_pos=None, keep=3, chunk=6000):
    """Vectorised broad phase (uniform grid, cell = r): (point, triangle) pairs within ~r, other
    feathers only, trimmed to the `keep` nearest triangles per (point, feather). Generator yielding
    progress fractions; the pairs are the StopIteration value.

    `tri_pos` (V, 3) gives triangle vertex positions when they differ from `x` (collision meshes);
    `tri_card` = None means no same-feather filtering.
    """
    tp = x if tri_pos is None else tri_pos
    a, b, c = tp[tri_v[:, 0]], tp[tri_v[:, 1]], tp[tri_v[:, 2]]
    tmin = np.minimum(np.minimum(a, b), c) - r
    tmax = np.maximum(np.maximum(a, b), c) + r
    cent = (a + b + c) / 3.0
    cell = max(r, 1e-6)
    origin = np.minimum(tmin.min(axis=0), x[points].min(axis=0)) if len(points) else tmin.min(axis=0)
    lo = np.floor((tmin - origin) / cell).astype(np.int64)
    span = np.floor((tmax - origin) / cell).astype(np.int64) - lo + 1
    count = span.prod(axis=1)
    tri_rep = np.repeat(np.arange(len(tri_v)), count)
    local = np.arange(len(tri_rep)) - np.repeat(np.cumsum(count) - count, count)
    sx, sy = span[tri_rep, 0], span[tri_rep, 1]
    big = np.int64(1 << 20)
    keys = ((lo[tri_rep, 0] + local % sx) * big + (lo[tri_rep, 1] + (local // sx) % sy)) * big \
        + (lo[tri_rep, 2] + local // (sx * sy))
    order = np.argsort(keys, kind='stable')
    keys, tri_rep = keys[order], tri_rep[order]
    yield 0.2

    pv_all, pt_all = [], []
    for start in range(0, len(points), chunk):
        pts = points[start:start + chunk]
        cidx = np.floor((x[pts] - origin) / cell).astype(np.int64)
        pkey = (cidx[:, 0] * big + cidx[:, 1]) * big + cidx[:, 2]
        lo_i = np.searchsorted(keys, pkey, side='left')
        hi_i = np.searchsorted(keys, pkey, side='right')
        n_each = hi_i - lo_i
        pv = np.repeat(pts, n_each)
        offs = np.arange(n_each.sum()) - np.repeat(np.cumsum(n_each) - n_each, n_each)
        pt = tri_rep[np.repeat(lo_i, n_each) + offs]
        if tri_card is not None:
            other = tri_card[pt] != point_card[pv]
            pv, pt = pv[other], pt[other]
        # The point must be inside the triangle's r-expanded bounding box
        pp = x[pv]
        inb = np.all((pp >= tmin[pt]) & (pp <= tmax[pt]), axis=1)
        pv, pt = pv[inb], pt[inb]
        if len(pv) and keep:
            dist = np.linalg.norm(x[pv] - cent[pt], axis=1)
            if tri_card is not None:
                gkeys = pv * (int(tri_card.max()) + 1) + tri_card[pt]
            else:
                gkeys = pv
            o = np.lexsort((dist, gkeys))
            ks = gkeys[o]
            first = np.r_[0, np.nonzero(ks[1:] != ks[:-1])[0] + 1]
            rank = np.arange(len(ks)) - np.repeat(first, np.diff(np.r_[first, len(ks)]))
            sel = o[rank < keep]
            pv, pt = pv[sel], pt[sel]
        pv_all.append(pv)
        pt_all.append(pt)
        yield 0.2 + 0.8 * min(1.0, (start + chunk) / max(1, len(points)))
    if not pv_all:
        return np.zeros(0, np.int64), np.zeros(0, np.int64)
    return np.concatenate(pv_all), np.concatenate(pt_all)


def _write_result(out_obj, key_name, x):
    """Write positions `x` (n, 3) into the result shape key (creating Basis + key if needed)."""
    mesh = out_obj.data
    if mesh.shape_keys is None:
        out_obj.shape_key_add(name="Basis", from_mix=False)
    kb = mesh.shape_keys.key_blocks.get(key_name)
    if kb is None:
        kb = out_obj.shape_key_add(name=key_name, from_mix=False)
        kb.value = 1.0
    kb.data.foreach_set("co", x.astype(np.float32).ravel())
    out_obj.active_shape_key_index = list(mesh.shape_keys.key_blocks).index(kb)
    mesh.update()


def simulate(guide_obj, state):
    """Generator: runs Fix Overlaps on `guide_obj`'s generated mesh, yielding between small chunks.

    `state` (dict) is updated with progress / stats and checked for "stop".
    """
    s = get_settings(guide_obj)
    out_obj = guide_obj.arantools_feather.output_object
    if out_obj is None or out_obj.data is None or len(out_obj.data.vertices) == 0:
        raise RuntimeError("This Feather Object has no generated feathers yet.")
    mesh = out_obj.data
    key_name = s.shape_key_name.strip() or "Fix Overlaps"
    state.update(phase="Preparing", progress=0.0, iteration=0, contacts=0, moved=0.0)
    yield

    vert_card, poly_card, vert_t, n_cards, gen_id, card_chain = _card_layout(guide_obj, out_obj)
    state["gen_id"] = gen_id
    n = len(mesh.vertices)

    # Rest = generated shape (the basis); never the current shape-key mix
    rest = np.empty(n * 3, dtype=np.float64)
    if mesh.shape_keys is not None:
        mesh.shape_keys.reference_key.data.foreach_get("co", rest)
    else:
        mesh.vertices.foreach_get("co", rest)
    rest = rest.reshape(n, 3)
    x = rest.copy()
    if s.start_from_current and mesh.shape_keys is not None and mesh.shape_keys.key_blocks.get(key_name):
        cur = np.empty(n * 3, dtype=np.float64)
        mesh.shape_keys.key_blocks[key_name].data.foreach_get("co", cur)
        x = cur.reshape(n, 3)

    mesh.calc_loop_triangles()
    n_tris = len(mesh.loop_triangles)
    tri_v = np.empty(n_tris * 3, dtype=np.int64)
    mesh.loop_triangles.foreach_get("vertices", tri_v)
    tri_v = tri_v.reshape(-1, 3)
    tri_poly = np.empty(n_tris, dtype=np.int64)
    mesh.loop_triangles.foreach_get("polygon_index", tri_poly)
    tri_card = poly_card[tri_poly]

    edges = np.empty(len(mesh.edges) * 2, dtype=np.int64)
    mesh.edges.foreach_get("vertices", edges)
    edges = edges.reshape(-1, 2)
    e0, e1 = edges[:, 0], edges[:, 1]
    rest_len = np.linalg.norm(rest[e1] - rest[e0], axis=1)
    degree = np.maximum(np.bincount(edges.ravel(), minlength=n), 1).astype(np.float64)[:, None]

    # 0 = base (where the arrows start): pins and stiffness start at the attached end
    obj_cache = fc._RUNTIME_CACHE.get(guide_obj.name) or {}
    cards = [(r, False) for r in obj_cache.get("chain_ranges") or []] + \
        [(r, True) for r in obj_cache.get("mirror_ranges") or []]
    mw = np.array(out_obj.matrix_world)
    vert_t = fc.base_oriented_t(guide_obj, rest @ mw[:3, :3].T + mw[:3, 3], vert_t, cards)
    t = np.clip(vert_t, 0.0, 1.0)
    pinned = (t < s.pin_length) if s.pin_base else np.zeros(n, dtype=bool)
    # Only the chosen feathers are simulated (the picks in Feather Select, else the visible ones);
    # the rest stay put but still act as obstacles
    only = state.get("only_chains")
    if only:
        hidden = ~np.isin(card_chain[vert_card], np.array(sorted(only), dtype=np.int64))
    else:
        hidden = _hidden_points(guide_obj, out_obj, vert_card, card_chain)
    pinned |= hidden
    state["simulated_points"] = int((~pinned).sum())
    if not (~hidden).any():
        raise RuntimeError("All feathers are hidden — nothing to simulate.")
    stiff = (s.tip_stiffness + (s.root_stiffness - s.tip_stiffness) * (1.0 - t) ** s.stiffness_falloff)[:, None]
    # Mobility: how much a point gives way in a contact (pinned = 0, root stiff, tip free).
    # A contact is split between the two feathers by mobility, so free tips move and the
    # feather they went into barely does.
    mobility = np.where(pinned, 0.0, 0.05 + 0.95 * t)
    tri_mobility = mobility[tri_v].mean(axis=1)
    query_verts = np.nonzero(~pinned)[0]

    depsgraph = bpy.context.evaluated_depsgraph_get()
    static = _collision_geometry(s, out_obj, depsgraph)
    yield

    # All distances are relative to the typical feather length, so defaults work at any scale
    lengths = []
    for (_c, v0, v1, *_), _m in cards:
        tt, pp = t[v0:v1], rest[v0:v1]
        if v1 - v0 >= 2:
            lengths.append(float(np.linalg.norm(pp[tt >= tt.max() - 0.05].mean(0) - pp[tt <= tt.min() + 0.05].mean(0))))
    feather_len = float(np.median(lengths)) if lengths else 1.0
    feather_len = max(feather_len, 1e-6)
    state["feather_length"] = feather_len
    gap = s.gap_rel * feather_len
    r = max(s.search_rel * feather_len, gap * 1.5)
    cgap = s.collision_gap_rel * feather_len
    max_offset = s.max_offset_rel * feather_len
    tolerance = s.tolerance_rel * feather_len
    # A point only counts as touching a card when it's within this of the card's surface. Points
    # further "behind" a card are beside it (an upright card's plane passes far through its
    # neighbours) or properly through it — those are the crossing resolver's job, which only acts
    # on triangles that really intersect.
    contact_depth = 3.0 * gap
    # Largest push per point per step: big single pushes shove cards through their other neighbours
    step_max = 0.05 * feather_len
    tri_list = tri_v.tolist()

    # ── Mirror symmetry: original cards and their mirror copies, vertex for vertex ──
    # Cards whose copy is an exact reflection (same card mesh, no random flip) are solved as one
    # feather: decisions are made per feather, and the two are averaged into mirror images each step.
    card_group = np.arange(n_cards, dtype=np.int64)
    sym_o = sym_m = np.zeros(0, dtype=np.int64)
    clip_idx = np.zeros(0, dtype=np.int64)
    clip_side = np.zeros(0)
    refl3 = refl_t = plane_n = None
    plane_c = 0.0
    fprops = guide_obj.arantools_feather
    mirror_list = obj_cache.get("mirror_ranges") or []
    if fprops.use_mirror and mirror_list:
        out_mw = out_obj.matrix_world
        rm = np.array(out_mw.inverted_safe() @ fc._get_mirror_reflection_matrix(fprops) @ out_mw)
        refl3, refl_t = rm[:3, :3], rm[:3, 3]
        k = np.argmax(np.linalg.norm(np.eye(3) - refl3, axis=0))
        plane_n = (np.eye(3) - refl3)[:, k]
        plane_n = plane_n / max(np.linalg.norm(plane_n), 1e-12)
        plane_c = float(plane_n @ refl_t) * 0.5
        n_orig = len(obj_cache.get("chain_ranges") or [])
        orig_of_chain = {rng[0]: i for i, (rng, _m) in enumerate(cards[:n_orig])}
        o_list, m_list, c_list, c_side = [], [], [], []
        for j, (rng, _m) in enumerate(cards[n_orig:]):
            mi = n_orig + j
            oi = orig_of_chain.get(rng[0])
            if oi is None:
                continue
            (_, a0, a1, *_), (_, b0, b1, *_) = cards[oi][0], rng
            if a1 - a0 != b1 - b0:
                continue
            if np.abs(rest[a0:a1] @ refl3.T + refl_t - rest[b0:b1]).max() > 1e-3 * feather_len:
                continue
            card_group[mi] = oi
            o_list.append(np.arange(a0, a1))
            m_list.append(np.arange(b0, b1))
            # Its own side of the plane: where its base is (or most of it, for a base on the plane)
            sd_o = rest[a0:a1] @ plane_n - plane_c
            tt = t[a0:a1]
            base_sd = float(sd_o[tt <= tt.min() + 0.05].mean())
            side = np.sign(base_sd) if abs(base_sd) > 0.01 * feather_len else np.sign(sd_o.sum())
            side = side if side != 0.0 else 1.0
            c_list += [np.arange(a0, a1), np.arange(b0, b1)]
            c_side += [np.full(a1 - a0, side), np.full(b1 - b0, -side)]
        if o_list:
            sym_o, sym_m = np.concatenate(o_list), np.concatenate(m_list)
            clip_idx, clip_side = np.concatenate(c_list), np.concatenate(c_side)
            keep = ~pinned[clip_idx]
            clip_idx, clip_side = clip_idx[keep], clip_side[keep]
    vert_group = card_group[vert_card]
    tri_group = card_group[tri_card]
    state["symmetric_feathers"] = int(len(np.unique(card_group[card_group != np.arange(n_cards)])))

    def symmetrize(x_):
        """Mirror pairs -> exact mirror images (their average), each on its own side of the plane."""
        if not len(sym_o):
            return
        avg = (x_[sym_o] + x_[sym_m] @ refl3.T + refl_t) * 0.5
        x_[sym_o] = avg
        x_[sym_m] = avg @ refl3.T + refl_t
        sd_ = x_[clip_idx] @ plane_n - plane_c
        need = clip_side * gap * 0.15 - sd_   # a hair off the plane: copies meet without crossing
        over = need * clip_side > 0.0
        x_[clip_idx[over]] += plane_n * need[over, None]

    def apply_push(x_, idx, vec):
        """Average the pushes per point, cap each point's step, apply in place."""
        push = _scatter_add(idx, vec, n)
        cnt = np.bincount(idx, minlength=n)
        m = cnt > 0
        step = push[m] / cnt[m, None]
        ln = np.linalg.norm(step, axis=1)
        big = ln > step_max
        step[big] *= (step_max / ln[big])[:, None]
        x_[m] += step

    # Facing of each card (its average normal; flipped outward once the neighbours are known)
    tri_n = np.cross(rest[tri_v[:, 1]] - rest[tri_v[:, 0]], rest[tri_v[:, 2]] - rest[tri_v[:, 0]])
    card_normal = np.stack([np.bincount(tri_card, weights=tri_n[:, k], minlength=n_cards) for k in range(3)], axis=1)

    # ── Stacking order of all feathers, read from where they overlap near their roots ──
    state["phase"] = "Reading feather layering"
    # Read once, over a wide radius, so deep crossings get a side from where the feathers actually
    # overlap (the per-step search can then stay small and fast)
    gather = _grid_candidates(rest, tri_v, tri_group, np.arange(n), vert_group, max(r, 0.5 * feather_len))
    while True:
        try:
            frac = next(gather)
        except StopIteration as done:
            pv, pt = done.value
            break
        state["progress"] = 0.05 * frac
        yield
        if state.get("stop"):
            return
    near = np.unique(vert_card[pv].astype(np.int64) * n_cards + tri_card[pt]) if len(pv) else np.zeros(0, np.int64)
    card_up = _card_up_sign(guide_obj, out_obj, cards, card_normal, rest, t, near // n_cards, near % n_cards)
    # A mirror copy faces out exactly like its original, reflected
    for mi in np.nonzero(card_group != np.arange(n_cards))[0].tolist():
        oi = card_group[mi]
        card_up[mi] = 1.0 if float(card_normal[mi] @ (refl3 @ (card_normal[oi] * card_up[oi]))) >= 0.0 else -1.0
    votes = np.zeros(0)
    vote_a = vote_b = np.zeros(0, dtype=np.int64)
    if len(pv):
        a, b, c = rest[tri_v[pt, 0]], rest[tri_v[pt, 1]], rest[tri_v[pt, 2]]
        nrm = np.cross(b - a, c - a)
        nrm /= np.maximum(np.linalg.norm(nrm, axis=1), 1e-14)[:, None]
        sd = np.einsum('ij,ij->i', rest[pv] - a, nrm)
        # Only points lying over / under the other card count: on a curved body a neighbour's plane,
        # extended sideways, passes above points beside it and would invert the layering
        decisive = (np.abs(sd) > max(gap * 0.05, 1e-7)) & _projects_inside(rest[pv], a, b, c)
        pv, pt, sd = pv[decisive], pt[decisive], sd[decisive]
        vote_a, vote_b = vert_card[pv], tri_card[pt]
        # +1 = this point's card lies over the other card (on its outward side). A feather grows into
        # its neighbour from the root toward the tip, so points near the ROOT decide; tips that went
        # through a neighbour barely count.
        votes = np.where(sd >= 0.0, 1.0, -1.0) * card_up[vote_b] * np.exp(-6.0 * t[pv])
        # Per feather: a mirror copy votes with its original, so both sides get the same order
        vote_a, vote_b = card_group[vote_a], card_group[vote_b]
    rank = _layer_order(vote_a, vote_b, votes, n_cards)[card_group]

    def side_of(pv_, pt_, x_=None):
        """Side (+1 / -1) of its candidate triangle each point belongs on: outward when the point's
        feather is higher in the stacking order, inward when lower."""
        ca, cb = vert_card[pv_], tri_card[pt_]
        return np.where(rank[ca] > rank[cb], 1.0, -1.0) * card_up[cb]

    # Stacking direction of a feather pair: the mean of their outward facings (≈ out of the body,
    # even where both cards are tilted like blinds)
    card_out = card_normal / np.maximum(np.linalg.norm(card_normal, axis=1), 1e-14)[:, None] * card_up[:, None]

    def stack_dir(pv_, pt_):
        """Unit direction each point should move to get to its side of its candidate triangle."""
        ca, cb = vert_card[pv_], tri_card[pt_]
        u = card_out[ca] + card_out[cb]
        ln = np.linalg.norm(u, axis=1)
        # Cards facing (nearly) opposite ways have no shared "out": use the other card's own
        opposed = ln < 0.5
        u[opposed] = card_out[cb[opposed]]
        ln[opposed] = 1.0
        u /= np.maximum(ln, 1e-14)[:, None]
        return u * np.where(rank[ca] > rank[cb], 1.0, -1.0)[:, None]

    def solve_contacts(x_, pv_, pt_, side_, w_, spv_, spt_, full_strength=False):
        """Apply contact corrections in place: feather vs feather (averaged per point), then
        collision meshes last so they always win. Returns the number of contacts found."""
        contacts_ = 0
        if len(pv_):
            weight = np.ones(len(pv_)) if full_strength else w_
            hit, p = _point_triangle_push(
                x_[pv_], x_[tri_v[pt_, 0]], x_[tri_v[pt_, 1]], x_[tri_v[pt_, 2]], side_, gap, contact_depth,
                weight, stack_dir(pv_, pt_))
            if hit.any():
                apply_push(x_, pv_[hit], p[hit])
                contacts_ += int(hit.sum())
        if static is not None and len(spv_):
            sv, st = static
            hit, p = _point_triangle_push(
                x_[spv_], sv[st[spt_, 0]], sv[st[spt_, 1]], sv[st[spt_, 2]],
                np.ones(len(spv_)), cgap, r, np.ones(len(spv_)))
            if hit.any():
                push = _scatter_add(spv_[hit], p[hit], n)
                cnt = np.bincount(spv_[hit], minlength=n)
                m = cnt > 0
                x_[m] += push[m] / cnt[m, None]
                contacts_ += int(hit.sum())
        return contacts_

    def crossing_pairs(x_, groups=tri_group):
        """Every pair of triangles from different feathers that intersect (exact BVH overlap).
        `groups=tri_card` also counts a feather against its own mirror copy."""
        tree = BVHTree.FromPolygons(x_.tolist(), tri_list)
        pairs = np.array(tree.overlap(tree), dtype=np.int64).reshape(-1, 2)
        return pairs[groups[pairs[:, 0]] != groups[pairs[:, 1]]] if len(pairs) else pairs

    def resolve_crossings(x_, pairs=None):
        """Push each crossing triangle's wrong-side corners to its side of the other triangle (plus
        the gap), split between the two cards by mobility. Catches crossings where no vertex of
        either card is inside the other — common with low-poly cards. Returns the number of crossings."""
        if pairs is None:
            pairs = crossing_pairs(x_)
        if not len(pairs):
            return 0
        ta = np.concatenate([pairs[:, 0], pairs[:, 1]])   # each crossing, from both sides
        tb = np.concatenate([pairs[:, 1], pairs[:, 0]])
        pv_ = tri_v[ta].ravel()
        pt_ = np.repeat(tb, 3)
        side_ = side_of(pv_, pt_, x_)
        a_, b_, c_ = (x_[tri_v[pt_, j]] for j in range(3))
        nrm = np.cross(b_ - a_, c_ - a_)
        nrm /= np.maximum(np.linalg.norm(nrm, axis=1), 1e-14)[:, None]
        sd_ = np.einsum('ij,ij->i', x_[pv_] - a_, nrm)
        wrong = side_ * sd_ < gap
        # A crossing that needs a PINNED corner to move can't be undone by the free corners: leave
        # it (it's reported) instead of pushing the rest of the feather further every iteration
        stuck = (wrong & pinned[pv_]).reshape(-1, 3).any(axis=1)
        wrong &= ~np.repeat(stuck, 3)
        if wrong.any():
            mv = mobility[pv_]
            w_ = mv / np.maximum(mv + tri_mobility[pt_], 1e-9)
            p_ = _push_along(nrm, side_ * gap - sd_, w_, stack_dir(pv_, pt_))
            apply_push(x_, pv_[wrong], p_[wrong])
        return len(pairs)

    def full_scan(x_):
        """Broad phase over every free point (numpy grid), as a generator returning candidate pairs."""
        g = _grid_candidates(x_, tri_v, tri_group, query_verts, vert_group, r)
        while True:
            try:
                yield 0.8 * next(g)
            except StopIteration as done:
                pv_, pt_ = done.value
                break
        spv_, spt_ = np.zeros(0, np.int64), np.zeros(0, np.int64)
        if static is not None:
            g = _grid_candidates(x_, static[1], None, query_verts, None, r, tri_pos=static[0], keep=4)
            while True:
                try:
                    yield 0.8 + 0.2 * next(g)
                except StopIteration as done:
                    spv_, spt_ = done.value
                    break
        side_ = side_of(pv_, pt_, x_)
        mv = mobility[pv_]
        w_ = mv / np.maximum(mv + tri_mobility[pt_], 1e-9) * s.collision_strength
        return pv_, pt_, side_, w_, spv_, spt_

    def run_scan(x_, base_progress, span):
        g = full_scan(x_)
        while True:
            try:
                frac = next(g)
            except StopIteration as done:
                return done.value
            state["progress"] = base_progress + span * frac
            yield

    # ── Iterate ──
    vel = np.zeros_like(x)
    cand = None
    settled_streak = 0
    iters = max(1, s.iterations)
    for it in range(iters):
        if state.get("stop"):
            break
        state.update(phase="Simulating", iteration=it + 1)
        x_prev = x.copy()

        # 1. Momentum
        if s.momentum > 0.0:
            x += vel * s.momentum

        # 2. Shape retention (root stiff -> tip free)
        x += (rest - x) * stiff

        # 3. Bend smoothing of the displacement field along card edges
        if s.smoothing > 0.0:
            disp = x - rest
            avg = _scatter_add(e0, disp[e1], n) + _scatter_add(e1, disp[e0], n)
            avg /= degree
            x = rest + disp + (avg - disp) * s.smoothing

        # 4. Edge lengths
        for _ in range(s.edge_iterations):
            d = x[e1] - x[e0]
            length = np.linalg.norm(d, axis=1)
            length[length < 1e-12] = 1e-12
            corr = d * (((length - rest_len) / length) * 0.5 * s.edge_stiffness)[:, None]
            delta = _scatter_add(e0, corr, n) - _scatter_add(e1, corr, n)
            x += delta / degree

        # 5. Contacts: BVH broad phase on scan iterations, vectorised narrow phase every step
        scanned = cand is None or it % s.full_scan_every == 0
        if scanned:
            cand = yield from run_scan(x, 0.05 + 0.95 * it / iters, 0.95 / iters)
        contacts = solve_contacts(x, *cand)
        crossings = resolve_crossings(x)
        contacts += crossings
        state["intersections"] = crossings

        # 6. Mirror symmetry, pins and clamp
        symmetrize(x)
        x[pinned] = rest[pinned]
        if max_offset > 0.0:
            disp = x - rest
            dl = np.linalg.norm(disp, axis=1)
            over = dl > max_offset
            x[over] = rest[over] + disp[over] * (max_offset / dl[over])[:, None]

        vel = x - x_prev
        moved = float(np.abs(vel).max()) if n else 0.0
        state.update(contacts=contacts, moved=moved, progress=0.05 + 0.95 * (it + 1) / iters)

        if (it + 1) % s.live_every == 0:
            _write_result(out_obj, key_name, x)

        # Only fresh scans can prove nothing overlaps any more
        if tolerance > 0.0 and scanned:
            if contacts == 0 and moved < tolerance:
                settled_streak += 1
                if settled_streak >= 2:
                    state["settled"] = True
                    break
            else:
                settled_streak = 0
        yield

    # ── Cleanup: resolve whatever is left at full strength (no shape pull-back) ──
    best_x, best_n = None, None
    if s.cleanup_passes > 0 and not state.get("stop"):
        state["phase"] = "Cleaning up"
        cand = yield from run_scan(x, 0.97, 0.02)
        for k in range(s.cleanup_passes):
            if k and k % 10 == 0:
                cand = yield from run_scan(x, 0.97, 0.02)
            pairs = crossing_pairs(x)
            # Pushes in a tight stack can make things worse for a while: keep the cleanest state
            if best_n is None or len(pairs) < best_n:
                best_x, best_n = x.copy(), len(pairs)
            found = solve_contacts(x, *cand) + resolve_crossings(x, pairs)
            symmetrize(x)
            x[pinned] = rest[pinned]
            if max_offset > 0.0:
                disp = x - rest
                dl = np.linalg.norm(disp, axis=1)
                over = dl > max_offset
                x[over] = rest[over] + disp[over] * (max_offset / dl[over])[:, None]
            if not found:
                break
            if k % 4 == 3:
                yield
        state["contacts"] = int(solve_contacts(x.copy(), *cand))
    left = len(crossing_pairs(x))
    if best_n is not None and best_n < left:
        x, left = best_x, best_n
    # Reported: every crossing, a feather against its own mirror copy included
    state["intersections"] = len(crossing_pairs(x, tri_card))

    _write_result(out_obj, key_name, x)
    state.update(phase="Done", progress=1.0)


# ============================================================================
# Operators
# ============================================================================

class ARANTOOLS_OT_feather_fix_overlaps(Operator):
    bl_idname = "arantools.feather_fix_overlaps"
    bl_label = "Fix Overlaps"
    bl_description = (
        "Wiggle the generated feathers apart so they stop going through each other and through "
        "collision meshes, keeping the original shape as much as possible. Only visible feathers move; "
        "hidden ones stay put as obstacles. Result goes to a shape key; the guide is not changed. "
        "Runs in the background — Blender stays usable"
    )
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        guide_obj = fc.get_feather_guide_from_context(context)
        return guide_obj is not None and guide_obj.name not in _RUNS

    def execute(self, context):
        # Blocking run (scripts / background); the panel uses invoke -> modal
        guide_obj = fc.get_feather_guide_from_context(context)
        state = {"started": time.monotonic(), "only_chains": _feather_select_targets(context, guide_obj),
                 "guide": guide_obj.name}
        try:
            for _ in simulate(guide_obj, state):
                pass
        except RuntimeError as exc:
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}
        self.report({'INFO'}, _summary(state))
        return {'FINISHED'}

    def invoke(self, context, event):
        if context.window is None or bpy.app.background:
            return self.execute(context)
        guide_obj = fc.get_feather_guide_from_context(context)
        self._guide_name = guide_obj.name
        self._state = {"started": time.monotonic(), "only_chains": _feather_select_targets(context, guide_obj),
                       "guide": guide_obj.name}
        _RUNS[self._guide_name] = self._state
        self._gen = simulate(guide_obj, self._state)
        wm = context.window_manager
        self._timer = wm.event_timer_add(0.01, window=context.window)
        wm.modal_handler_add(self)
        return {'RUNNING_MODAL'}

    def _end(self, context):
        context.window_manager.event_timer_remove(self._timer)
        _RUNS.pop(self._guide_name, None)
        if context.workspace is not None:
            context.workspace.status_text_set(None)
        _tag_redraw_all()

    def modal(self, context, event):
        if event.type != 'TIMER':
            return {'PASS_THROUGH'}  # everything else keeps working while it runs

        guide_obj = bpy.data.objects.get(self._guide_name)
        cache = fc._RUNTIME_CACHE.get(self._guide_name)
        if guide_obj is None or (
            "gen_id" in self._state and cache is not None and cache.get("gen_id") != self._state["gen_id"]
        ):
            self._end(context)
            self.report({'WARNING'}, "Fix Overlaps stopped: the feathers were regenerated meanwhile.")
            return {'CANCELLED'}

        budget = get_settings(guide_obj).time_budget_ms / 1000.0
        t0 = time.monotonic()
        try:
            while time.monotonic() - t0 < budget:
                next(self._gen)
        except StopIteration:
            self._end(context)
            self.report({'INFO'}, _summary(self._state))
            return {'FINISHED'}
        except RuntimeError as exc:
            self._end(context)
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}

        st = self._state
        if context.workspace is not None:
            context.workspace.status_text_set(
                f"Fix Overlaps: {st.get('progress', 0.0) * 100:.0f}%   {st.get('phase', '')}   "
                f"iteration {st.get('iteration', 0)}   contacts {st.get('contacts', 0)}"
            )
        _tag_redraw_all()
        return {'PASS_THROUGH'}


def _feather_select_targets(context, guide_obj):
    """In Feather Select: the picked feathers (all when none are picked). Otherwise None (= visible)."""
    if fc.is_in_feather_mode(guide_obj) and fc.get_feather_level(context) == 'FEATHER':
        picks = fc.get_picked_chains(guide_obj)
        return set(picks) if picks else set(range(len(fc._get_guide_chains_fast(guide_obj))))
    return None


_LAST_RESULT = {}  # guide name -> crossing triangle pairs left after the last run


def _summary(state):
    elapsed = time.monotonic() - state.get("started", time.monotonic())
    if "guide" in state and "intersections" in state:
        _LAST_RESULT[state["guide"]] = int(state["intersections"])
    how = "stopped" if state.get("stop") else ("settled" if state.get("settled") else "finished")
    return (
        f"Fix Overlaps {how} after {state.get('iteration', 0)} iterations: "
        f"{state.get('intersections', 0)} crossing triangle pairs left ({elapsed:.1f}s)."
    )


class ARANTOOLS_OT_feather_fix_overlaps_stop(Operator):
    bl_idname = "arantools.feather_fix_overlaps_stop"
    bl_label = "Stop Fix Overlaps"
    bl_description = "Stop the running simulation and keep the result so far"
    bl_options = {'INTERNAL'}

    def execute(self, context):
        guide_obj = fc.get_feather_guide_from_context(context)
        run = _RUNS.get(guide_obj.name) if guide_obj else None
        if run is not None:
            run["stop"] = True
        return {'FINISHED'}


class ARANTOOLS_OT_feather_fix_overlaps_clear(Operator):
    bl_idname = "arantools.feather_fix_overlaps_clear"
    bl_label = "Remove Fix Overlaps"
    bl_description = "Delete the Fix Overlaps shape key (the feathers go back to the generated shape)"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        guide_obj = fc.get_feather_guide_from_context(context)
        out_obj = guide_obj.arantools_feather.output_object if guide_obj else None
        sk = out_obj.data.shape_keys if out_obj is not None else None
        kb = sk.key_blocks.get(get_settings(guide_obj).shape_key_name) if sk is not None else None
        if kb is None:
            return {'CANCELLED'}
        out_obj.shape_key_remove(kb)
        if len(out_obj.data.shape_keys.key_blocks) == 1:  # only Basis left
            out_obj.shape_key_clear()
        return {'FINISHED'}


# ============================================================================
# Panel section
# ============================================================================

def draw_fix_overlaps_ui(layout, context, guide_obj):
    s = get_settings(guide_obj)
    out_obj = guide_obj.arantools_feather.output_object
    box = layout.box()
    col = box.column(align=True)
    col.label(text="Fix Overlaps", icon='PHYSICS')
    only = _feather_select_targets(context, guide_obj)
    note = col.row()
    note.enabled = False
    if only is not None:
        picked = fc.get_picked_chains(guide_obj)
        note.label(text=f"Runs on the {len(picked)} picked feathers" if picked else "Runs on all feathers (none picked)")
    else:
        note.label(text="Runs on the visible feathers")
    if guide_obj.arantools_feather.use_mirror:
        fc.draw_centre_feathers_hint(col, guide_obj)
    last = _LAST_RESULT.get(guide_obj.name)
    if last is not None and guide_obj.name not in _RUNS:
        res = col.row()
        res.alert = last > 0
        res.label(text="Last run: no crossing cards" if last == 0 else f"Last run: {last} crossing triangle pairs left",
                  icon='CHECKMARK' if last == 0 else 'ERROR')

    run = _RUNS.get(guide_obj.name)
    if run is not None:
        col.progress(
            factor=run.get("progress", 0.0), type='BAR',
            text=f"{run.get('phase', '')}  {run.get('progress', 0.0) * 100:.0f}%",
        )
        stat = col.row(align=True)
        stat.enabled = False
        stat.label(text=f"Iteration {run.get('iteration', 0)} / {s.iterations}   •   {run.get('contacts', 0)} contacts")
        if "simulated_points" in run:
            stat2 = col.row(align=True)
            stat2.enabled = False
            stat2.label(text=f"{run['simulated_points']} visible points simulated")
        col.separator(factor=0.3)
        col.operator("arantools.feather_fix_overlaps_stop", text="Stop (keep result)", icon='CANCEL')
        return

    row = col.row(align=True)
    row.scale_y = 1.3
    row.operator("arantools.feather_fix_overlaps", text="Fix Overlaps", icon='PLAY')
    sk = out_obj.data.shape_keys if out_obj is not None and out_obj.data is not None else None
    kb = sk.key_blocks.get(s.shape_key_name) if sk is not None else None
    if kb is not None:
        row.operator("arantools.feather_fix_overlaps_clear", text="", icon='TRASH')
        col.prop(kb, "value", text="Result Blend", slider=True)

    col.separator(factor=0.4)
    col.prop(s, "show_settings", icon='TRIA_DOWN' if s.show_settings else 'TRIA_RIGHT', emboss=False)
    if not s.show_settings:
        return

    shape = box.column(align=True)
    shape.label(text="Keep Shape")
    shape.prop(s, "pin_base")
    sub = shape.row(align=True)
    sub.enabled = s.pin_base
    sub.prop(s, "pin_length", slider=True)
    shape.prop(s, "root_stiffness", slider=True)
    shape.prop(s, "tip_stiffness", slider=True)
    shape.prop(s, "stiffness_falloff")
    shape.prop(s, "smoothing", slider=True)
    shape.prop(s, "edge_stiffness", slider=True)
    shape.prop(s, "edge_iterations")
    shape.prop(s, "max_offset_rel", text="Max Offset (× length)")

    cont = box.column(align=True)
    cont.label(text="Feather Contacts")
    cont.prop(s, "gap_rel", text="Gap (× length)")
    cont.prop(s, "search_rel", text="Search (× length)")
    cont.prop(s, "collision_strength", slider=True)

    coll = box.column(align=True)
    coll.prop(s, "use_collision")
    sub = coll.column(align=True)
    sub.enabled = s.use_collision
    sub.prop(s, "collision_collection", text="")
    sub.prop(s, "collision_gap_rel", text="Collision Gap (× length)")

    solver = box.column(align=True)
    solver.label(text="Solver")
    solver.prop(s, "iterations")
    solver.prop(s, "momentum", slider=True)
    solver.prop(s, "tolerance_rel", text="Settled (× length)")
    solver.prop(s, "full_scan_every")
    solver.prop(s, "cleanup_passes")
    solver.prop(s, "time_budget_ms")
    solver.prop(s, "live_every")
    solver.prop(s, "start_from_current")
    solver.prop(s, "shape_key_name")


# ============================================================================
# Registration
# ============================================================================

classes = (
    ARANTOOLS_PG_FixOverlaps,
    ARANTOOLS_OT_feather_fix_overlaps,
    ARANTOOLS_OT_feather_fix_overlaps_stop,
    ARANTOOLS_OT_feather_fix_overlaps_clear,
)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)
    bpy.types.Object.arantools_fix_overlaps = PointerProperty(type=ARANTOOLS_PG_FixOverlaps)


def unregister():
    _RUNS.clear()
    if hasattr(bpy.types.Object, "arantools_fix_overlaps"):
        del bpy.types.Object.arantools_fix_overlaps
    for cls in reversed(classes):
        try:
            bpy.utils.unregister_class(cls)
        except Exception:
            pass
