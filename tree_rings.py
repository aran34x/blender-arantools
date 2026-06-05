import bpy
import math
import random
from collections import defaultdict
from mathutils import Vector, noise
from bpy.types import Operator


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


# ── Boundary extraction ─────────────────────────────────────────────────────

def _walk_mesh_loop(obj):
    """Return world-space verts of a mesh in connected-edge-loop order, or
    None if it isn't a single clean loop."""
    mesh = obj.data
    if not mesh.edges or not mesh.vertices:
        return None
    adj = defaultdict(list)
    for e in mesh.edges:
        a, b = e.vertices
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
        if len(order) > len(mesh.vertices):
            break
    mw = obj.matrix_world
    return [mw @ mesh.vertices[i].co for i in order]


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
        loop = _walk_mesh_loop(obj)
        if loop and len(loop) >= 3:
            return loop
        mw = obj.matrix_world
        return [mw @ v.co for v in obj.data.vertices]
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
# Property group
# ============================================================================

class ARANTOOLS_PG_TreeRings(bpy.types.PropertyGroup):
    inner_object: bpy.props.PointerProperty(
        name="Inner",
        type=bpy.types.Object,
        description="Inner boundary shape (small loop near the centre)",
        poll=lambda self, o: o.type in {'MESH', 'CURVE'},
    )
    outer_object: bpy.props.PointerProperty(
        name="Outer",
        type=bpy.types.Object,
        description="Outer boundary shape — the spiral never crosses this",
        poll=lambda self, o: o.type in {'MESH', 'CURVE'},
    )
    rings: bpy.props.IntProperty(
        name="Rings",
        description="Number of turns from the inner to the outer shape",
        default=8, min=1, max=200,
    )
    points_per_ring: bpy.props.IntProperty(
        name="Points / Ring",
        description="Spiral resolution per turn. Higher = smoother",
        default=72, min=8, max=512,
    )
    seed: bpy.props.IntProperty(
        name="Seed",
        description="Random seed — change it for a different variant",
        default=0,
    )
    irregularity: bpy.props.FloatProperty(
        name="Irregularity",
        description="Random radial wobble as a fraction of one ring's spacing. "
                    ">1 lets rings overlap (still inside the outer shape)",
        default=0.6, min=0.0, max=4.0, subtype='FACTOR',
    )
    lumpiness: bpy.props.FloatProperty(
        name="Lumpiness",
        description="Angular frequency of the wobble — low = a few broad lobes, "
                    "high = many small bumps",
        default=2.0, min=0.1, max=16.0,
    )
    ring_drift: bpy.props.FloatProperty(
        name="Ring Drift",
        description="How much the wobble changes from one ring to the next. "
                    "0 = perfectly nested rings, higher = rings diverge / merge",
        default=1.0, min=0.0, max=8.0,
    )
    start_angle: bpy.props.FloatProperty(
        name="Start Angle",
        description="Rotates where the spiral begins",
        default=0.0, subtype='ANGLE',
    )
    thickness: bpy.props.FloatProperty(
        name="Line Thickness",
        description="Curve bevel depth, for a visible solid line (0 = wire)",
        default=0.0, min=0.0, soft_max=1.0, subtype='DISTANCE',
    )
    replace_previous: bpy.props.BoolProperty(
        name="Replace Previous",
        description="Replace the last generated spiral instead of adding a new "
                    "one — handy while tuning variants",
        default=True,
    )
    last_spiral: bpy.props.PointerProperty(type=bpy.types.Object)


# ============================================================================
# Core generation
# ============================================================================

def _build_spiral_points(props):
    """Return a list of world-space Vectors for the spiral, or raise ValueError."""
    inner_obj = props.inner_object
    outer_obj = props.outer_object
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

    rings = props.rings
    ppr = props.points_per_ring
    total = rings * ppr
    amp = props.irregularity / rings
    freq = props.lumpiness
    drift = props.ring_drift

    rng = random.Random(props.seed)
    ox = rng.uniform(-1000.0, 1000.0)
    oy = rng.uniform(-1000.0, 1000.0)
    oz = rng.uniform(-1000.0, 1000.0)

    pts = []
    for i in range(total + 1):
        turn = i / ppr
        ang = props.start_angle + 2.0 * math.pi * turn
        p = i / total

        c = math.cos(ang)
        s = math.sin(ang)
        w = noise.noise(Vector((c * freq + ox,
                                s * freq + oy,
                                p * drift + oz)))
        p_eff = min(1.0, max(0.0, p + amp * w))

        r_in = _radius_at(inner_prof, ang)
        r_out = _radius_at(outer_prof, ang)
        r = r_in + (r_out - r_in) * p_eff

        pts.append(origin + u * (c * r) + v * (s * r))
    return pts


def _make_spiral_object(context, props, pts):
    curve = bpy.data.curves.new("AranTools_WoodSpiral", 'CURVE')
    curve.dimensions = '3D'
    curve.bevel_depth = props.thickness
    spline = curve.splines.new('POLY')
    spline.points.add(len(pts) - 1)
    for sp, co in zip(spline.points, pts):
        sp.co = (co.x, co.y, co.z, 1.0)
    obj = bpy.data.objects.new("WoodSpiral", curve)
    context.collection.objects.link(obj)
    return obj


# ============================================================================
# Operators
# ============================================================================

class ARANTOOLS_OT_TreeRingsGenerate(Operator):
    """Generate a randomizable growth-ring spiral that winds from the inner
shape to the outer shape and never crosses the outer boundary."""
    bl_idname = "arantools.tree_rings_generate"
    bl_label = "Generate Ring Spiral"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        props = context.scene.arantools_tree_rings
        try:
            pts = _build_spiral_points(props)
        except ValueError as e:
            self.report({'ERROR'}, str(e))
            return {'CANCELLED'}

        if props.replace_previous and props.last_spiral is not None:
            old = props.last_spiral
            try:
                data = old.data
                bpy.data.objects.remove(old, do_unlink=True)
                if data and data.users == 0:
                    bpy.data.curves.remove(data)
            except (ReferenceError, RuntimeError):
                pass

        obj = _make_spiral_object(context, props, pts)
        props.last_spiral = obj

        for o in context.selected_objects:
            o.select_set(False)
        obj.select_set(True)
        context.view_layer.objects.active = obj
        self.report({'INFO'}, f"Generated spiral ({len(pts)} points).")
        return {'FINISHED'}


class ARANTOOLS_OT_TreeRingsRandomize(Operator):
    """Pick a new random seed and regenerate — a fresh variant each click."""
    bl_idname = "arantools.tree_rings_randomize"
    bl_label = "Randomize Variant"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        props = context.scene.arantools_tree_rings
        props.seed = random.randint(0, 2_000_000_000)
        return bpy.ops.arantools.tree_rings_generate()


class ARANTOOLS_OT_TreeRingsSetShape(Operator):
    """Assign the active object as the Inner or Outer shape."""
    bl_idname = "arantools.tree_rings_set_shape"
    bl_label = "Set Shape"
    bl_options = {'REGISTER', 'UNDO'}

    slot: bpy.props.EnumProperty(
        items=[('INNER', "Inner", ""), ('OUTER', "Outer", "")],
    )

    def execute(self, context):
        props = context.scene.arantools_tree_rings
        obj = context.active_object
        if obj is None or obj.type not in {'MESH', 'CURVE'}:
            self.report({'ERROR'}, "Active object must be a mesh or curve.")
            return {'CANCELLED'}
        if self.slot == 'INNER':
            props.inner_object = obj
        else:
            props.outer_object = obj
        return {'FINISHED'}


# ============================================================================
# Registration
# ============================================================================

classes = [
    ARANTOOLS_PG_TreeRings,
    ARANTOOLS_OT_TreeRingsGenerate,
    ARANTOOLS_OT_TreeRingsRandomize,
    ARANTOOLS_OT_TreeRingsSetShape,
]


def register():
    for cls in classes:
        bpy.utils.register_class(cls)
    bpy.types.Scene.arantools_tree_rings = bpy.props.PointerProperty(
        type=ARANTOOLS_PG_TreeRings
    )


def unregister():
    del bpy.types.Scene.arantools_tree_rings
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
