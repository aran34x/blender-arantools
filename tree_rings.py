import bpy
import bmesh
import math
import random
from collections import defaultdict
from mathutils import Vector, noise
from bpy.types import Operator


# Above this many spiral points (rings × points/ring) live update auto-pauses
# so dragging a slider or editing a boundary never stutters. Press Generate to
# build the heavy version on demand.
LIVE_MAX_POINTS = 20000


# ============================================================================
# Growth-ring / wood-grain spiral generator
# ============================================================================
#
# Given an INNER and an OUTER closed shape (mesh edge-loops or curves), build a
# randomizable spiral that winds from the inner shape out to the outer shape —
# like tree growth rings.
#
# Why it can never leave the outer shape:
#   For every angle θ around a shared center we measure the inner boundary
#   radius r_in(θ) and the outer boundary radius r_out(θ). A spiral point at
#   that angle is r = r_in + p·(r_out - r_in) with p clamped to [0, 1]. That's
#   a convex blend of the two boundaries along one ray, so the point always
#   lies on the segment between them — inside the ring band. Random wobble only
#   nudges p (still clamped), so rings may overlap each other but never escape.
#
# The shapes are treated as star-shaped about the inner centroid (true for the
# blobby/convex rings this is meant for): we sample each boundary as an
# (angle → radius) profile and interpolate, which is robust and needs no
# polygon ray-casting.
#
# Multiple pairs: the scene holds a *collection* of spiral items, each with its
# own Inner/Outer shapes, its own full parameter set, and its own generated
# curve object (`last_spiral`). The panel manages them with a UIList
# (add / remove / duplicate / reorder). Live update is a single global toggle
# that applies to every item.


# ── Boundary extraction ─────────────────────────────────────────────────────

def _mesh_edges_coords(obj):
    """Return (edges, world-space coords) for a mesh. Edit-mode aware: while the
    mesh is being edited we read the live BMesh so the spiral follows the edit
    without needing a flush to mesh data (a flush would re-fire the depsgraph
    handler and loop)."""
    mw = obj.matrix_world
    me = obj.data
    if obj.mode == 'EDIT':
        bm = bmesh.from_edit_mesh(me)
        idx = {v: i for i, v in enumerate(bm.verts)}
        coords = [mw @ v.co for v in bm.verts]
        edges = [(idx[e.verts[0]], idx[e.verts[1]]) for e in bm.edges]
        return edges, coords
    coords = [mw @ v.co for v in me.vertices]
    edges = [(e.vertices[0], e.vertices[1]) for e in me.edges]
    return edges, coords


def _walk_loop(edges, coords):
    """Return coords in connected-edge-loop order, or None if not a clean loop."""
    if not edges or not coords:
        return None
    adj = defaultdict(list)
    for a, b in edges:
        adj[a].append(b)
        adj[b].append(a)
    start = next(iter(adj))
    order = [start]
    prev, cur = None, start
    while True:
        nxts = [v for v in adj[cur] if v != prev]
        if not nxts:
            break
        nxt = nxts[0]
        if nxt == start:
            break
        order.append(nxt)
        prev, cur = cur, nxt
        if len(order) > len(coords):
            break
    return [coords[i] for i in order]


def _curve_points(obj):
    """Return world-space points sampled from a curve object's first spline."""
    mw = obj.matrix_world
    pts = []
    for spline in obj.data.splines:
        if spline.type == 'BEZIER':
            for bp in spline.bezier_points:
                pts.append(mw @ bp.co)
        else:
            for p in spline.points:
                pts.append(mw @ p.co.to_3d())
        if pts:
            break
    return pts


def _boundary_points(obj):
    """Ordered-ish world points for a mesh edge-loop or a curve. Falls back to
    raw mesh verts (order then fixed by angle-sorting in the profile)."""
    if obj.type == 'CURVE':
        return _curve_points(obj)
    if obj.type == 'MESH':
        edges, coords = _mesh_edges_coords(obj)
        loop = _walk_loop(edges, coords)
        if loop and len(loop) >= 3:
            return loop
        return coords
    return []


# ── Plane + projection ──────────────────────────────────────────────────────

def _newell_normal(pts):
    n = Vector((0.0, 0.0, 0.0))
    L = len(pts)
    for i in range(L):
        c = pts[i]
        nx = pts[(i + 1) % L]
        n.x += (c.y - nx.y) * (c.z + nx.z)
        n.y += (c.z - nx.z) * (c.x + nx.x)
        n.z += (c.x - nx.x) * (c.y + nx.y)
    if n.length < 1e-9:
        return Vector((0.0, 0.0, 1.0))
    return n.normalized()


def _plane_basis(normal):
    up = Vector((0.0, 0.0, 1.0))
    if abs(normal.dot(up)) > 0.999:
        up = Vector((1.0, 0.0, 0.0))
    u = normal.cross(up).normalized()
    v = normal.cross(u).normalized()
    return u, v


# ── Angular radius profile ──────────────────────────────────────────────────

def _build_profile(points3d, origin, u, v):
    """Project points to the plane and return a sorted list of (angle, radius)
    measured from the (projected) origin."""
    samples = []
    for p in points3d:
        d = p - origin
        x = d.dot(u)
        y = d.dot(v)
        r = math.hypot(x, y)
        if r < 1e-9:
            continue
        samples.append((math.atan2(y, x), r))
    samples.sort()
    return samples


def _radius_at(profile, angle):
    """Interpolate the boundary radius at `angle` (radians) from a sorted
    (angle, radius) profile, wrapping around ±π."""
    n = len(profile)
    if n == 0:
        return 0.0
    if n == 1:
        return profile[0][1]
    a = (angle + math.pi) % (2.0 * math.pi) - math.pi
    for i in range(n):
        a0, r0 = profile[i]
        a1, r1 = profile[(i + 1) % n]
        if i == n - 1:
            a1 += 2.0 * math.pi
            aa = a if a >= a0 else a + 2.0 * math.pi
        else:
            aa = a
        if a0 <= aa <= a1:
            span = a1 - a0
            t = 0.0 if span < 1e-9 else (aa - a0) / span
            return r0 + (r1 - r0) * t
    return profile[0][1]


# ============================================================================
# Property groups
# ============================================================================

# Per-item parameters that "Duplicate" copies wholesale.
_COPY_PROPS = (
    "inner_object", "outer_object", "rings", "points_per_ring", "seed",
    "irregularity", "lumpiness", "ring_drift", "start_angle", "thickness",
    "connect_ends", "connect_blend", "z_rise",
)


def _live_update_cb(self, context):
    """Fires when a tunable property changes in the panel. `self` is the spiral
    item being edited. Rebuilds that item's spiral in place when live update is
    on (no-op until it has been generated once). Boundary edits in Edit Mode go
    through the depsgraph handler instead — property callbacks never see those."""
    if context is None:
        return
    container = getattr(context.scene, "arantools_tree_rings", None)
    if container is None or not container.live_update:
        return
    _try_live_rebuild(self)


class ARANTOOLS_PG_SpiralItem(bpy.types.PropertyGroup):
    name: bpy.props.StringProperty(name="Name", default="Spiral")
    # Stable id used to name this item's irregularity Float Curve node inside
    # the hidden curves node group. 0 = not assigned yet (legacy / pre-curve).
    uid: bpy.props.IntProperty(default=0)
    inner_object: bpy.props.PointerProperty(
        name="Inner",
        type=bpy.types.Object,
        description="Inner boundary shape (small loop near the centre)",
        poll=lambda self, o: o.type in {'MESH', 'CURVE'},
        update=_live_update_cb,
    )
    outer_object: bpy.props.PointerProperty(
        name="Outer",
        type=bpy.types.Object,
        description="Outer boundary shape — the spiral never crosses this",
        poll=lambda self, o: o.type in {'MESH', 'CURVE'},
        update=_live_update_cb,
    )
    rings: bpy.props.IntProperty(
        name="Rings",
        description="Number of turns from the inner to the outer shape",
        default=8, min=1, max=200,
        update=_live_update_cb,
    )
    points_per_ring: bpy.props.IntProperty(
        name="Points / Ring",
        description="Spiral resolution per turn. Higher = smoother",
        default=72, min=8, max=512,
        update=_live_update_cb,
    )
    seed: bpy.props.IntProperty(
        name="Seed",
        description="Random seed — change it for a different variant",
        default=0,
        update=_live_update_cb,
    )
    irregularity: bpy.props.FloatProperty(
        name="Irregularity",
        description="Random radial wobble as a fraction of one ring's spacing. "
                    ">1 lets rings overlap (still inside the outer shape)",
        default=0.6, min=0.0, max=4.0, subtype='FACTOR',
        update=_live_update_cb,
    )
    lumpiness: bpy.props.FloatProperty(
        name="Lumpiness",
        description="Angular frequency of the wobble — low = a few broad lobes, "
                    "high = many small bumps",
        default=2.0, min=0.1, max=16.0,
        update=_live_update_cb,
    )
    ring_drift: bpy.props.FloatProperty(
        name="Ring Drift",
        description="How much the wobble changes from one ring to the next. "
                    "0 = perfectly nested rings, higher = rings diverge / merge",
        default=1.0, min=0.0, max=8.0,
        update=_live_update_cb,
    )
    start_angle: bpy.props.FloatProperty(
        name="Start Angle",
        description="Rotates where the spiral begins",
        default=0.0, subtype='ANGLE',
        update=_live_update_cb,
    )
    thickness: bpy.props.FloatProperty(
        name="Line Thickness",
        description="Curve bevel depth, for a visible solid line (0 = wire)",
        default=0.0, min=0.0, soft_max=1.0, subtype='DISTANCE',
        update=_live_update_cb,
    )
    connect_ends: bpy.props.BoolProperty(
        name="Connect Ends",
        description="Seamlessly merge the loose inner/outer tips: each end wraps "
                    "an extra fraction of a turn while easing onto the adjacent "
                    "ring, blending into the ring band tangentially (no spike, "
                    "no straight connector)",
        default=False,
        update=_live_update_cb,
    )
    connect_blend: bpy.props.FloatProperty(
        name="Merge Length",
        description="How far (in turns) each end wraps while blending into the "
                    "adjacent ring. Longer = a more gradual, hidden merge",
        default=0.5, min=0.05, soft_max=2.0, max=4.0,
        update=_live_update_cb,
    )
    z_rise: bpy.props.FloatProperty(
        name="Z Rise",
        description="Lift the spiral along its plane normal across its length "
                    "(inner start = 0 → outer end = full), turning it into a "
                    "gentle helix. With a non-zero value no part overlaps "
                    "another in 3D — useful when animating along the length "
                    "(e.g. the UV R channel in Unreal) so revealed ends don't "
                    "intersect other geometry",
        default=0.0, subtype='DISTANCE',
        update=_live_update_cb,
    )
    last_spiral: bpy.props.PointerProperty(type=bpy.types.Object)


class ARANTOOLS_PG_TreeRings(bpy.types.PropertyGroup):
    pairs: bpy.props.CollectionProperty(type=ARANTOOLS_PG_SpiralItem)
    active_index: bpy.props.IntProperty(default=0)
    next_uid: bpy.props.IntProperty(default=1)  # hands out unique item uids
    live_update: bpy.props.BoolProperty(
        name="Live Update",
        description="Rebuild spirals automatically as you tweak sliders or edit "
                    "the Inner/Outer shapes in Edit Mode. Applies to all pairs. "
                    "Auto-pauses a pair above ~%d points (press Generate for "
                    "heavy builds)" % LIVE_MAX_POINTS,
        default=True,
    )


def _active_item(container):
    idx = container.active_index
    if 0 <= idx < len(container.pairs):
        return container.pairs[idx]
    return None


def _unique_pair_name(container, base):
    base = base or "Spiral"
    existing = {p.name for p in container.pairs}
    if base not in existing:
        return base
    i = 1
    while f"{base}.{i:03d}" in existing:
        i += 1
    return f"{base}.{i:03d}"


# ── Per-pair irregularity curves ────────────────────────────────────────────
#
# Each spiral item gets a Float Curve mapping the radial factor (0 = inner
# boundary → 1 = outer) to a wobble multiplier, so e.g. a curve pinned to 0 at
# x=0 locks the innermost points exactly onto the Inner shape. CurveMappings
# can't live on a PropertyGroup, so — like the Branch taper curves — we stash a
# Float Curve node per item inside a hidden ShaderNodeTree, keyed by the item's
# uid. The node group carries a fake user so it survives save/reload.

RINGS_CURVES_NODEGROUP = "AranTools_TreeRingsCurves"


def _curve_node_name(item):
    return f"irreg_{item.uid}"


def _assign_uid(container, item):
    item.uid = container.next_uid
    container.next_uid += 1


def _ensure_rings_curve_group():
    ng = bpy.data.node_groups.get(RINGS_CURVES_NODEGROUP)
    if ng is None:
        ng = bpy.data.node_groups.new(RINGS_CURVES_NODEGROUP, 'ShaderNodeTree')
        ng.use_fake_user = True
    return ng


def _ensure_item_curve(container, item):
    """Create (if missing) and return this item's irregularity Float Curve node.
    Assigns a uid first if the item doesn't have one. Writes bpy.data — only
    call from operators, never from UI draw or live callbacks."""
    if item.uid == 0:
        _assign_uid(container, item)
    ng = _ensure_rings_curve_group()
    node = ng.nodes.get(_curve_node_name(item))
    if node is None:
        node = ng.nodes.new('ShaderNodeFloatCurve')
        node.name = _curve_node_name(item)
        node.label = item.name
        # Default flat at 1.0 → uniform irregularity (matches the old behaviour).
        cm = node.mapping
        curve = cm.curves[0]
        curve.points[0].location = (0.0, 1.0)
        curve.points[1].location = (1.0, 1.0)
        cm.update()
    return node


def _get_item_curve_node(item):
    """Read-only lookup, safe in UI draw / live callbacks. None if not yet
    created."""
    if item.uid == 0:
        return None
    ng = bpy.data.node_groups.get(RINGS_CURVES_NODEGROUP)
    if ng is None:
        return None
    return ng.nodes.get(_curve_node_name(item))


def _delete_item_curve(item):
    ng = bpy.data.node_groups.get(RINGS_CURVES_NODEGROUP)
    if ng is None:
        return
    node = ng.nodes.get(_curve_node_name(item))
    if node is not None:
        try:
            ng.nodes.remove(node)
        except RuntimeError:
            pass


def _copy_item_curve(container, src_item, dst_item):
    """Copy src's irregularity curve points onto dst (creating dst's node)."""
    src = _get_item_curve_node(src_item)
    dst = _ensure_item_curve(container, dst_item)
    if src is None or dst is None:
        return
    s_curve = src.mapping.curves[0]
    d_curve = dst.mapping.curves[0]
    while len(d_curve.points) > len(s_curve.points):
        d_curve.points.remove(d_curve.points[-1])
    while len(d_curve.points) < len(s_curve.points):
        d_curve.points.new(0.5, 0.5)
    for sp, dp in zip(s_curve.points, d_curve.points):
        dp.location = (sp.location[0], sp.location[1])
        dp.handle_type = sp.handle_type
    dst.mapping.update()


# ============================================================================
# Core generation
# ============================================================================

def _build_spiral_points(item):
    """Return a list of world-space Vectors for the spiral, or raise ValueError."""
    inner_obj = item.inner_object
    outer_obj = item.outer_object
    if inner_obj is None or outer_obj is None:
        raise ValueError("Set both an Inner and an Outer shape.")

    inner_pts = _boundary_points(inner_obj)
    outer_pts = _boundary_points(outer_obj)
    if len(inner_pts) < 3 or len(outer_pts) < 3:
        raise ValueError("Inner/Outer shapes need at least 3 boundary points.")

    normal = _newell_normal(outer_pts)
    u, v = _plane_basis(normal)
    origin = sum(inner_pts, Vector((0.0, 0.0, 0.0))) / len(inner_pts)

    inner_prof = _build_profile(inner_pts, origin, u, v)
    outer_prof = _build_profile(outer_pts, origin, u, v)
    if not inner_prof or not outer_prof:
        raise ValueError("Could not build boundary profiles (degenerate shape).")

    rings = item.rings
    ppr = item.points_per_ring
    total = rings * ppr
    amp = item.irregularity / rings
    freq = item.lumpiness
    drift = item.ring_drift

    rng = random.Random(item.seed)
    ox = rng.uniform(-1000.0, 1000.0)
    oy = rng.uniform(-1000.0, 1000.0)
    oz = rng.uniform(-1000.0, 1000.0)

    # Optional per-pair irregularity curve: maps radial factor (0 inner → 1
    # outer) to a wobble multiplier. Read-only lookup so this stays safe in live
    # callbacks; a flat 1.0 is used if the curve hasn't been created.
    irr_node = _get_item_curve_node(item)
    irr_map = irr_curve = None
    if irr_node is not None:
        irr_map = irr_node.mapping
        irr_map.initialize()
        irr_curve = irr_map.curves[0]

    def irr_at(p_factor):
        if irr_map is None:
            return 1.0
        return max(0.0, irr_map.evaluate(irr_curve, p_factor))

    z_rise = item.z_rise

    def point_at(ang, p_factor):
        """World point at this angle / radial factor (0 = inner boundary → 1 =
        outer). Z tracks the radial factor (× Z Rise) so the spiral is a gentle
        helix — and so an end-merge can return to a previous ring's exact
        height when it feeds back in."""
        c = math.cos(ang)
        s = math.sin(ang)
        w = noise.noise(Vector((c * freq + ox,
                                s * freq + oy,
                                p_factor * drift + oz)))
        p_eff = min(1.0, max(0.0, p_factor + amp * irr_at(p_factor) * w))
        r_in = _radius_at(inner_prof, ang)
        r_out = _radius_at(outer_prof, ang)
        r = r_in + (r_out - r_in) * p_eff
        return origin + u * (c * r) + v * (s * r) + normal * (z_rise * p_factor)

    a_start = item.start_angle
    a_end = a_start + 2.0 * math.pi * rings

    main = [point_at(a_start + 2.0 * math.pi * (i / ppr), i / total)
            for i in range(total + 1)]
    pts = main

    # Seamless end-merge that feeds EXACTLY into a previous line. Each end wraps
    # an extra whole number of points at the spiral's own angular cadence
    # (2π/ppr per point) while easing radially (smoothstep) onto a real vertex
    # one run away, then snaps its final point onto that exact vertex — so the
    # tip lands precisely on existing geometry (matching X, Y and Z), with no
    # gap, no offset and no straight chord. Tangent at the landing because the
    # smoothstep kills the radial rate there, leaving circumferential motion.
    if item.connect_ends and total > ppr:
        dpp = 2.0 * math.pi / ppr
        steps = max(1, min(ppr - 1, int(round(ppr * item.connect_blend))))

        # Tail: outer tip → vertex `steps` points into the previous run.
        tgt_end = total - ppr + steps
        p_end = tgt_end / total
        tail = []
        for m in range(1, steps + 1):
            t = m / steps
            wsm = t * t * (3.0 - 2.0 * t)
            tail.append(point_at(a_end + dpp * m, 1.0 + (p_end - 1.0) * wsm))
        tail[-1] = main[tgt_end].copy()          # exact feed-in

        # Lead-in: vertex on the second run → inner tip (prepended).
        tgt_start = ppr - steps
        p_start = tgt_start / total
        lead = []
        for m in range(steps, 0, -1):
            t = m / steps
            wsm = t * t * (3.0 - 2.0 * t)
            lead.append(point_at(a_start - dpp * m, p_start * wsm))
        lead[0] = main[tgt_start].copy()         # exact feed-in

        pts = lead + main + tail

    return pts


def _write_spiral_curve(curve, item, pts):
    """Write the point list into `curve` in place, reusing the existing POLY
    spline when the point count matches (cheap live updates) and rebuilding it
    only when the count changed."""
    curve.dimensions = '3D'
    curve.bevel_depth = item.thickness
    need = len(pts)
    spline = curve.splines[0] if curve.splines else None
    if spline is None or spline.type != 'POLY' or len(spline.points) != need:
        curve.splines.clear()
        spline = curve.splines.new('POLY')
        if need > 1:
            spline.points.add(need - 1)
    flat = [0.0] * (need * 4)
    for i, co in enumerate(pts):
        j = i * 4
        flat[j] = co.x
        flat[j + 1] = co.y
        flat[j + 2] = co.z
        flat[j + 3] = 1.0
    spline.points.foreach_set("co", flat)


def _make_spiral_object(context, item, pts):
    name = item.name or "WoodSpiral"
    curve = bpy.data.curves.new(f"AranTools_{name}", 'CURVE')
    _write_spiral_curve(curve, item, pts)
    obj = bpy.data.objects.new(name, curve)
    context.collection.objects.link(obj)
    return obj


def _generate_item(context, item):
    """Build or refresh this item's spiral, reusing its object when present.
    Returns the object. Raises ValueError if the shapes aren't usable."""
    pts = _build_spiral_points(item)
    obj = _live_target(item)
    if obj is None:
        obj = _make_spiral_object(context, item, pts)
        item.last_spiral = obj
    else:
        _write_spiral_curve(obj.data, item, pts)
    return obj


def _delete_item_object(item):
    """Remove the curve object the tool generated for this item, if any."""
    obj = _live_target(item)
    if obj is None:
        return
    try:
        data = obj.data
        bpy.data.objects.remove(obj, do_unlink=True)
        if data and data.users == 0:
            bpy.data.curves.remove(data)
    except (ReferenceError, RuntimeError):
        pass


# ── Live update (slider tweaks + Edit-Mode boundary follow) ─────────────────

def _live_target(item):
    """Return the item's spiral curve object, or None if there isn't a valid one
    yet (nothing generated, or it was deleted by the user)."""
    obj = item.last_spiral
    if obj is None:
        return None
    try:
        if obj.name not in bpy.data.objects or obj.type != 'CURVE':
            return None
    except ReferenceError:
        return None
    return obj


def _try_live_rebuild(item):
    """Rebuild one item's spiral in place. Silently no-ops when shapes aren't
    both set, there's no generated object yet, or the point count is over the
    live cost cap. (The global live toggle is checked by the callers.)"""
    if item.inner_object is None or item.outer_object is None:
        return
    if item.rings * item.points_per_ring > LIVE_MAX_POINTS:
        return
    obj = _live_target(item)
    if obj is None:
        return
    try:
        pts = _build_spiral_points(item)
    except ValueError:
        return
    _write_spiral_curve(obj.data, item, pts)


# Re-entrancy / scheduling state for the depsgraph handler.
_live_busy = False
_live_pending = False
_live_scene = None
_pending_items = []


def _live_timer():
    """One-shot deferred rebuild — runs outside the depsgraph handler, where
    writing to data is safe."""
    global _live_busy, _live_pending
    _live_pending = False
    scene = _live_scene
    if scene is None:
        return None
    container = getattr(scene, "arantools_tree_rings", None)
    if container is None:
        return None
    _live_busy = True
    try:
        n = len(container.pairs)
        for idx in _pending_items:
            if 0 <= idx < n:
                _try_live_rebuild(container.pairs[idx])
    finally:
        _live_busy = False
        _pending_items.clear()
    return None


def _arantools_tree_rings_depsgraph(scene, depsgraph):
    """Refresh spirals when their Inner/Outer shapes change — including live
    vertex edits in Edit Mode, which property callbacks never see."""
    global _live_pending, _live_scene
    if _live_busy or _live_pending:
        return
    container = getattr(scene, "arantools_tree_rings", None)
    if container is None or not container.live_update or not container.pairs:
        return

    # Map each boundary datablock (object or its data) → the item indices that
    # depend on it. We react only to those — never to our own spiral-curve
    # writes, which would otherwise loop.
    target_items = {}
    for i, it in enumerate(container.pairs):
        if it.last_spiral is None:
            continue
        for o in (it.inner_object, it.outer_object):
            if o is None:
                continue
            target_items.setdefault(o, set()).add(i)
            data = getattr(o, "data", None)
            if data is not None:
                target_items.setdefault(data, set()).add(i)
    if not target_items:
        return

    hit = set()
    for upd in depsgraph.updates:
        if not (upd.is_updated_geometry or upd.is_updated_transform):
            continue
        orig = getattr(upd.id, "original", upd.id)
        s = target_items.get(orig)
        if s:
            hit |= s
    if not hit:
        return

    _live_scene = scene
    _pending_items[:] = sorted(hit)
    _live_pending = True
    bpy.app.timers.register(_live_timer, first_interval=0.0)


def _install_depsgraph_handler():
    _remove_depsgraph_handler()
    bpy.app.handlers.depsgraph_update_post.append(_arantools_tree_rings_depsgraph)


def _remove_depsgraph_handler():
    handlers = bpy.app.handlers.depsgraph_update_post
    for h in list(handlers):
        if getattr(h, "__name__", "") == "_arantools_tree_rings_depsgraph":
            try:
                handlers.remove(h)
            except ValueError:
                pass


# ============================================================================
# UI list
# ============================================================================

class ARANTOOLS_UL_spiral_pairs(bpy.types.UIList):
    def draw_item(self, context, layout, data, item, icon,
                  active_data, active_propname, index):
        if self.layout_type in {'DEFAULT', 'COMPACT'}:
            row = layout.row(align=True)
            row.prop(item, "name", text="", emboss=False, icon='FORCE_VORTEX')
            ready = item.inner_object is not None and item.outer_object is not None
            row.label(text="", icon='CHECKMARK' if ready else 'ERROR')
        elif self.layout_type == 'GRID':
            layout.alignment = 'CENTER'
            layout.label(text="", icon='FORCE_VORTEX')


# ============================================================================
# Operators
# ============================================================================

class ARANTOOLS_OT_TreeRingsAdd(Operator):
    """Add a new Inner/Outer spiral pair to the list."""
    bl_idname = "arantools.tree_rings_add"
    bl_label = "Add Spiral Pair"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        container = context.scene.arantools_tree_rings
        item = container.pairs.add()
        item.name = _unique_pair_name(container, "Spiral")
        _ensure_item_curve(container, item)
        container.active_index = len(container.pairs) - 1
        return {'FINISHED'}


class ARANTOOLS_OT_TreeRingsRemove(Operator):
    """Remove the selected spiral pair and the curve it generated."""
    bl_idname = "arantools.tree_rings_remove"
    bl_label = "Remove Spiral Pair"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return _active_item(context.scene.arantools_tree_rings) is not None

    def execute(self, context):
        container = context.scene.arantools_tree_rings
        idx = container.active_index
        item = _active_item(container)
        if item is None:
            return {'CANCELLED'}
        _delete_item_object(item)
        _delete_item_curve(item)
        container.pairs.remove(idx)
        container.active_index = min(idx, len(container.pairs) - 1)
        return {'FINISHED'}


class ARANTOOLS_OT_TreeRingsDuplicate(Operator):
    """Duplicate the selected pair — copies all of its settings and generates a
    fresh spiral object for the copy."""
    bl_idname = "arantools.tree_rings_duplicate"
    bl_label = "Duplicate Spiral Pair"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return _active_item(context.scene.arantools_tree_rings) is not None

    def execute(self, context):
        container = context.scene.arantools_tree_rings
        src = _active_item(container)
        if src is None:
            return {'CANCELLED'}
        dst = container.pairs.add()
        for prop in _COPY_PROPS:
            setattr(dst, prop, getattr(src, prop))
        dst.name = _unique_pair_name(container, src.name)
        dst.last_spiral = None  # the copy gets its own freshly built object
        dst.uid = 0             # force a fresh curve node (don't share src's)
        _copy_item_curve(container, src, dst)
        container.active_index = len(container.pairs) - 1
        try:
            _generate_item(context, dst)
        except ValueError:
            pass  # shapes not set yet — leave it for an explicit Generate
        return {'FINISHED'}


class ARANTOOLS_OT_TreeRingsMove(Operator):
    """Reorder the selected pair in the list."""
    bl_idname = "arantools.tree_rings_move"
    bl_label = "Move Spiral Pair"
    bl_options = {'REGISTER', 'UNDO'}

    direction: bpy.props.EnumProperty(
        items=[('UP', "Up", ""), ('DOWN', "Down", "")],
    )

    @classmethod
    def poll(cls, context):
        return _active_item(context.scene.arantools_tree_rings) is not None

    def execute(self, context):
        container = context.scene.arantools_tree_rings
        idx = container.active_index
        new_idx = idx - 1 if self.direction == 'UP' else idx + 1
        if not (0 <= new_idx < len(container.pairs)):
            return {'CANCELLED'}
        container.pairs.move(idx, new_idx)
        container.active_index = new_idx
        return {'FINISHED'}


class ARANTOOLS_OT_TreeRingsGenerate(Operator):
    """Generate (or refresh) the selected pair's growth-ring spiral. It winds
from the inner shape to the outer shape and never crosses the outer boundary."""
    bl_idname = "arantools.tree_rings_generate"
    bl_label = "Generate Ring Spiral"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        container = context.scene.arantools_tree_rings
        item = _active_item(container)
        if item is None:
            self.report({'ERROR'}, "No spiral pair selected. Add one first.")
            return {'CANCELLED'}
        try:
            obj = _generate_item(context, item)
        except ValueError as e:
            self.report({'ERROR'}, str(e))
            return {'CANCELLED'}

        for o in context.selected_objects:
            o.select_set(False)
        obj.select_set(True)
        context.view_layer.objects.active = obj
        n = len(obj.data.splines[0].points) if obj.data.splines else 0
        self.report({'INFO'}, f"Generated spiral ({n} points).")
        return {'FINISHED'}


class ARANTOOLS_OT_TreeRingsGenerateAll(Operator):
    """Generate (or refresh) every spiral pair in the list."""
    bl_idname = "arantools.tree_rings_generate_all"
    bl_label = "Generate All Spirals"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        container = context.scene.arantools_tree_rings
        done = 0
        skipped = 0
        for item in container.pairs:
            try:
                _generate_item(context, item)
                done += 1
            except ValueError:
                skipped += 1
        msg = f"Generated {done} spiral(s)."
        if skipped:
            msg += f" Skipped {skipped} with missing shapes."
        self.report({'INFO'}, msg)
        return {'FINISHED'} if done else {'CANCELLED'}


class ARANTOOLS_OT_TreeRingsRandomize(Operator):
    """Pick a new random seed for the selected pair and regenerate it."""
    bl_idname = "arantools.tree_rings_randomize"
    bl_label = "Randomize Variant"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        container = context.scene.arantools_tree_rings
        item = _active_item(container)
        if item is None:
            self.report({'ERROR'}, "No spiral pair selected. Add one first.")
            return {'CANCELLED'}
        item.seed = random.randint(0, 2_000_000_000)
        try:
            _generate_item(context, item)
        except ValueError as e:
            self.report({'ERROR'}, str(e))
            return {'CANCELLED'}
        return {'FINISHED'}


class ARANTOOLS_OT_TreeRingsSetShape(Operator):
    """Assign the active object as the selected pair's Inner or Outer shape."""
    bl_idname = "arantools.tree_rings_set_shape"
    bl_label = "Set Shape"
    bl_options = {'REGISTER', 'UNDO'}

    slot: bpy.props.EnumProperty(
        items=[('INNER', "Inner", ""), ('OUTER', "Outer", "")],
    )

    def execute(self, context):
        container = context.scene.arantools_tree_rings
        item = _active_item(container)
        if item is None:
            self.report({'ERROR'}, "No spiral pair selected. Add one first.")
            return {'CANCELLED'}
        obj = context.active_object
        if obj is None or obj.type not in {'MESH', 'CURVE'}:
            self.report({'ERROR'}, "Active object must be a mesh or curve.")
            return {'CANCELLED'}
        if self.slot == 'INNER':
            item.inner_object = obj
        else:
            item.outer_object = obj
        return {'FINISHED'}


class ARANTOOLS_OT_TreeRingsEnsureCurve(Operator):
    """Create (or reset) the selected pair's irregularity curve — a Float Curve
mapping the radial position (0 = inner → 1 = outer) to a wobble multiplier."""
    bl_idname = "arantools.tree_rings_ensure_curve"
    bl_label = "Create Irregularity Curve"
    bl_options = {'REGISTER', 'UNDO'}

    reset: bpy.props.BoolProperty(default=False)

    @classmethod
    def poll(cls, context):
        return _active_item(context.scene.arantools_tree_rings) is not None

    def execute(self, context):
        container = context.scene.arantools_tree_rings
        item = _active_item(container)
        if item is None:
            return {'CANCELLED'}
        if self.reset:
            _delete_item_curve(item)
        _ensure_item_curve(container, item)
        return {'FINISHED'}


# ============================================================================
# Registration
# ============================================================================

classes = [
    ARANTOOLS_PG_SpiralItem,
    ARANTOOLS_PG_TreeRings,
    ARANTOOLS_UL_spiral_pairs,
    ARANTOOLS_OT_TreeRingsAdd,
    ARANTOOLS_OT_TreeRingsRemove,
    ARANTOOLS_OT_TreeRingsDuplicate,
    ARANTOOLS_OT_TreeRingsMove,
    ARANTOOLS_OT_TreeRingsGenerate,
    ARANTOOLS_OT_TreeRingsGenerateAll,
    ARANTOOLS_OT_TreeRingsRandomize,
    ARANTOOLS_OT_TreeRingsSetShape,
    ARANTOOLS_OT_TreeRingsEnsureCurve,
]


def register():
    for cls in classes:
        bpy.utils.register_class(cls)
    bpy.types.Scene.arantools_tree_rings = bpy.props.PointerProperty(
        type=ARANTOOLS_PG_TreeRings
    )
    _install_depsgraph_handler()


def unregister():
    _remove_depsgraph_handler()
    del bpy.types.Scene.arantools_tree_rings
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
