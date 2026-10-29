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
     where it belongs, keeping a minimum gap. "Where it belongs" is decided once per feather pair
     from where the feathers' ROOTS sit, so tips that went through a neighbour are pushed back.
  6. Collision meshes (a collection): vertices are pushed out along the surface normal.
  7. Pins and max-offset clamp.
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
        name="Pin Base", default=True,
        description="Feather bases don't move at all (only the free part of each feather is simulated)",
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
    max_offset: FloatProperty(
        name="Max Offset", default=0.05, min=0.0, soft_max=0.5, subtype='DISTANCE',
        description="No point moves further than this from its original position (0 = unlimited)",
    )

    # ── Feather contacts ──
    gap: FloatProperty(
        name="Gap", default=0.002, min=0.0, soft_max=0.05, subtype='DISTANCE', precision=4,
        description="Minimum distance kept between feathers",
    )
    search_distance: FloatProperty(
        name="Search Distance", default=0.02, min=0.0001, soft_max=0.2, subtype='DISTANCE', precision=4,
        description="How far to look for contacts — must exceed how deep tips have gone through neighbours",
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
    collision_gap: FloatProperty(
        name="Collision Gap", default=0.001, min=0.0, soft_max=0.05, subtype='DISTANCE', precision=4,
        description="Minimum distance kept from collision meshes",
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
    tolerance: FloatProperty(
        name="Stop When Settled", default=0.00002, min=0.0, soft_max=0.001, subtype='DISTANCE', precision=6,
        description="Stop early once nothing overlaps and no point moves more than this per step (0 = never)",
    )
    cleanup_passes: IntProperty(
        name="Cleanup Passes", default=6, min=0, max=50,
        description="Extra full-strength contact passes at the end so the result has no overlaps left",
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


def _point_triangle_push(p, a, b, c, side, gap, depth, weight):
    """Vectorised: for points `p` against triangles (a, b, c), the push that puts each point on
    `side` of its triangle at least `gap` away — zero where the point doesn't project onto the
    triangle, is already fine, or is deeper than `depth` (not a contact)."""
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
    push = nrm * ((side * gap - sd) * weight)[:, None]
    return hit, push


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

    t = np.clip(vert_t, 0.0, 1.0)
    pinned = (t < s.pin_length) if s.pin_base else np.zeros(n, dtype=bool)
    # Only visible feathers are simulated; hidden ones stay put but still act as obstacles
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

    r = max(s.search_distance, s.gap * 1.5)
    gap, cgap = s.gap, s.collision_gap

    # ── Which side of each neighbour every feather belongs on, decided from the roots ──
    state["phase"] = "Reading feather layering"
    gather = _grid_candidates(rest, tri_v, tri_card, np.arange(n), vert_card, r)
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
    # A feather grows into its neighbour from the root toward the tip, so for every feather pair
    # the contact point nearest the ROOT shows the side that feather belongs on.
    pair_side = {}
    if len(pv):
        a, b, c = rest[tri_v[pt, 0]], rest[tri_v[pt, 1]], rest[tri_v[pt, 2]]
        nrm = np.cross(b - a, c - a)
        nrm /= np.maximum(np.linalg.norm(nrm, axis=1), 1e-14)[:, None]
        sd = np.einsum('ij,ij->i', rest[pv] - a, nrm)
        decisive = np.abs(sd) > max(gap * 0.05, 1e-7)
        keys = vert_card[pv].astype(np.int64) * n_cards + tri_card[pt]
        keys, sd, tt = keys[decisive], sd[decisive], t[pv][decisive]
        order = np.lexsort((tt, keys))
        ks = keys[order]
        first = order[np.r_[0, np.nonzero(ks[1:] != ks[:-1])[0] + 1]] if len(ks) else order
        pair_side = dict(zip(keys[first].tolist(), np.where(sd[first] >= 0.0, 1.0, -1.0).tolist()))

    side_keys = np.array(sorted(pair_side), dtype=np.int64)
    side_vals = np.array([pair_side[k] for k in side_keys.tolist()], dtype=np.float64)

    def side_of(pv_, pt_, x_):
        """Side (+1 / -1) each point belongs on relative to its candidate triangle's feather."""
        nonlocal side_keys, side_vals
        keys_ = vert_card[pv_].astype(np.int64) * n_cards + tri_card[pt_]
        out = np.empty(len(keys_))
        if len(side_keys):
            pos = np.clip(np.searchsorted(side_keys, keys_), 0, len(side_keys) - 1)
            known = side_keys[pos] == keys_
            out[known] = side_vals[pos[known]]
        else:
            known = np.zeros(len(keys_), dtype=bool)
        if (~known).any():
            # Pairs that only met mid-simulation: keep them on the side they are on now
            mi = np.nonzero(~known)[0]
            a_, b_, c_ = (x_[tri_v[pt_[mi], j]] for j in range(3))
            sd_ = np.einsum('ij,ij->i', x_[pv_[mi]] - a_, np.cross(b_ - a_, c_ - a_))
            new_keys, first = np.unique(keys_[mi], return_index=True)
            new_vals = np.where(sd_[first] >= 0.0, 1.0, -1.0)
            side_keys = np.concatenate([side_keys, new_keys])
            side_vals = np.concatenate([side_vals, new_vals])
            o = np.argsort(side_keys)
            side_keys, side_vals = side_keys[o], side_vals[o]
            pos = np.searchsorted(side_keys, keys_[mi])
            out[mi] = side_vals[pos]
        return out

    def solve_contacts(x_, pv_, pt_, side_, w_, spv_, spt_, full_strength=False):
        """Apply contact corrections in place: feather vs feather (averaged per point), then
        collision meshes last so they always win. Returns the number of contacts found."""
        contacts_ = 0
        if len(pv_):
            weight = np.ones(len(pv_)) if full_strength else w_
            hit, p = _point_triangle_push(
                x_[pv_], x_[tri_v[pt_, 0]], x_[tri_v[pt_, 1]], x_[tri_v[pt_, 2]], side_, gap, r, weight)
            if hit.any():
                push = _scatter_add(pv_[hit], p[hit], n)
                cnt = np.bincount(pv_[hit], minlength=n)
                m = cnt > 0
                x_[m] += push[m] / cnt[m, None]
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

    def full_scan(x_):
        """Broad phase over every free point (numpy grid), as a generator returning candidate pairs."""
        g = _grid_candidates(x_, tri_v, tri_card, query_verts, vert_card, r)
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

        # 6. Pins and clamp
        x[pinned] = rest[pinned]
        if s.max_offset > 0.0:
            disp = x - rest
            dl = np.linalg.norm(disp, axis=1)
            over = dl > s.max_offset
            x[over] = rest[over] + disp[over] * (s.max_offset / dl[over])[:, None]

        vel = x - x_prev
        moved = float(np.abs(vel).max()) if n else 0.0
        state.update(contacts=contacts, moved=moved, progress=0.05 + 0.95 * (it + 1) / iters)

        if (it + 1) % s.live_every == 0:
            _write_result(out_obj, key_name, x)

        # Only fresh scans can prove nothing overlaps any more
        if s.tolerance > 0.0 and scanned:
            if contacts == 0 and moved < s.tolerance:
                settled_streak += 1
                if settled_streak >= 2:
                    state["settled"] = True
                    break
            else:
                settled_streak = 0
        yield

    # ── Cleanup: resolve whatever is left at full strength (no shape pull-back) ──
    if s.cleanup_passes > 0 and not state.get("stop"):
        state["phase"] = "Cleaning up"
        cand = yield from run_scan(x, 0.97, 0.02)
        for _ in range(s.cleanup_passes):
            if not solve_contacts(x, *cand, full_strength=True):
                break
            x[pinned] = rest[pinned]
            if s.max_offset > 0.0:
                disp = x - rest
                dl = np.linalg.norm(disp, axis=1)
                over = dl > s.max_offset
                x[over] = rest[over] + disp[over] * (s.max_offset / dl[over])[:, None]
        state["contacts"] = int(solve_contacts(x.copy(), *cand))

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
        state = {"started": time.monotonic()}
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
        self._state = {"started": time.monotonic()}
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


def _summary(state):
    elapsed = time.monotonic() - state.get("started", time.monotonic())
    how = "stopped" if state.get("stop") else ("settled" if state.get("settled") else "finished")
    return (
        f"Fix Overlaps {how} after {state.get('iteration', 0)} iterations "
        f"({state.get('contacts', 0)} contacts left, {elapsed:.1f}s)."
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
    shape.prop(s, "max_offset")

    cont = box.column(align=True)
    cont.label(text="Feather Contacts")
    cont.prop(s, "gap")
    cont.prop(s, "search_distance")
    cont.prop(s, "collision_strength", slider=True)

    coll = box.column(align=True)
    coll.prop(s, "use_collision")
    sub = coll.column(align=True)
    sub.enabled = s.use_collision
    sub.prop(s, "collision_collection", text="")
    sub.prop(s, "collision_gap")

    solver = box.column(align=True)
    solver.label(text="Solver")
    solver.prop(s, "iterations")
    solver.prop(s, "momentum", slider=True)
    solver.prop(s, "tolerance")
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
