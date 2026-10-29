"""
AranTools — Feather normals
===========================
Per-feather custom normals for the generated cards, transferred from a mesh (usually the body):

  * Normal Mesh (per Feather Object) — whose (smooth, rest-pose) normals are transferred.
  * Normal From Mesh (per feather)   — overall amount of the transfer.
  * Base / Middle / Tip (per feather) — how much of that amount applies along the feather
    (blended smoothly base -> middle -> tip; base = where the direction arrows start).

Computed from REST data (the normal mesh's undeformed surface, the cards' basis) and written as
custom normals, which Blender stores relative to each face — so they follow the rig. That means
the sliders can be tuned while the character is posed. Rebuilt automatically after the feathers
regenerate (a full rebuild wipes custom normals).
"""

import bpy
import bmesh
import numpy as np
from bpy.props import BoolProperty, FloatProperty, PointerProperty
from bpy.types import Operator, PropertyGroup
from mathutils.bvhtree import BVHTree

from . import feather_cards as fc
from . import feather_rig as fr


NORMAL_EDGE_FLOAT = (
    ("feather_nrm_amount", 0.0),  # overall transfer amount (0 = the card's own normals)
    ("feather_nrm_base", 1.0),    # share of it at the base
    ("feather_nrm_mid", 0.6),     # ...at the middle
    ("feather_nrm_tip", 0.2),     # ...at the tip
    ("feather_nrm_range", 1.0),   # transfer reaches this far from the base (fraction of the feather)
    ("feather_nrm_smooth", 0.0),  # smoothing of the result, growing from the base outwards
)
_RANGE_FADE = 0.12  # the transfer fades out over this fraction of the feather past Snap Range

_SYNCING = False
_SOURCE_CACHE = {}   # {key: (verts, vertex_normals, tris, bvh)}
_PROJ_CACHE = {}     # {key: (tri_index, bary)}


# ============================================================================
# Per-feather attributes
# ============================================================================

def _ensure_attrs(guide_obj):
    me = guide_obj.data
    if guide_obj.mode == 'EDIT':
        bm = bmesh.from_edit_mesh(me)
        changed = False
        for name, default in NORMAL_EDGE_FLOAT:
            if bm.edges.layers.float.get(name) is None:
                lyr = bm.edges.layers.float.new(name)
                for e in bm.edges:
                    e[lyr] = default
                changed = True
        if changed:
            bmesh.update_edit_mesh(me, loop_triangles=False, destructive=False)
        return
    for name, default in NORMAL_EDGE_FLOAT:
        if me.attributes.get(name) is None:
            me.attributes.new(name=name, type='FLOAT', domain='EDGE').data.foreach_set(
                "value", [float(default)] * len(me.edges))


def _write_feathers(guide_obj, chain_ids, name, value):
    if not chain_ids:
        return
    _ensure_attrs(guide_obj)
    chains = fc._get_guide_chains_fast(guide_obj)
    edge_ids = [ei for ci in chain_ids if 0 <= ci < len(chains) for ei in chains[ci][1]]
    me = guide_obj.data
    if guide_obj.mode == 'EDIT':
        bm = bmesh.from_edit_mesh(me)
        bm.edges.ensure_lookup_table()
        lyr = bm.edges.layers.float.get(name)
        for ei in edge_ids:
            bm.edges[ei][lyr] = value
        bmesh.update_edit_mesh(me, loop_triangles=False, destructive=False)
    else:
        data = me.attributes[name].data
        for ei in edge_ids:
            data[ei].value = value


def feather_normal_settings(guide_obj):
    """{attr: per-chain values} from each feather's first edge."""
    chains = fc._get_guide_chains_fast(guide_obj)
    first = np.array([ec[0] if ec else 0 for _, ec in chains], dtype=np.int64)
    return {
        name: (fr._read_edge(guide_obj, name, False, default)[first] if len(first) else np.zeros(0))
        for name, default in NORMAL_EDGE_FLOAT
    }


# ============================================================================
# Transfer
# ============================================================================

def _source_object(guide_obj):
    ns = guide_obj.arantools_feather_normals
    src = ns.source or guide_obj.arantools_feather_rig.body or guide_obj.arantools_feather.surface_object
    return src if src is not None and src.type == 'MESH' and len(src.data.polygons) else None


def _source_data(src):
    """Rest-pose (undeformed) surface of the normal mesh in world space, with smooth vertex normals."""
    me = src.data
    key = (src.name, me.name, len(me.vertices), tuple(round(x, 5) for row in src.matrix_world for x in row))
    hit = _SOURCE_CACHE.get(key)
    if hit is not None:
        return hit
    n = len(me.vertices)
    co = np.empty(n * 3, dtype=np.float64)
    me.vertices.foreach_get("co", co)
    vn = np.empty(n * 3, dtype=np.float64)
    me.vertex_normals.foreach_get("vector", vn)
    mw = np.array(src.matrix_world)
    verts = co.reshape(-1, 3) @ mw[:3, :3].T + mw[:3, 3]
    nrm_mat = np.linalg.inv(mw[:3, :3]).T
    vnorm = vn.reshape(-1, 3) @ nrm_mat.T
    vnorm /= np.maximum(np.linalg.norm(vnorm, axis=1, keepdims=True), 1e-12)
    me.calc_loop_triangles()
    tv = np.empty(len(me.loop_triangles) * 3, dtype=np.int64)
    me.loop_triangles.foreach_get("vertices", tv)
    tv = tv.reshape(-1, 3)
    data = (verts, vnorm, tv, BVHTree.FromPolygons(verts.tolist(), tv.tolist()))
    _SOURCE_CACHE.clear()
    _SOURCE_CACHE[key] = data
    return data


def _project(source, points, key):
    """Nearest source triangle + barycentric coordinates per point (cached per generation)."""
    hit = _PROJ_CACHE.get(key)
    if hit is not None and len(hit[0]) == len(points):
        return hit
    verts, _, tv, bvh = source
    tri = np.zeros(len(points), dtype=np.int64)
    loc = np.array(points, dtype=np.float64, copy=True)
    for i, p in enumerate(points.tolist()):
        l, _n, ti, _d = bvh.find_nearest(p)
        if ti is not None:
            tri[i] = ti
            loc[i] = l
    a, b, c = verts[tv[tri, 0]], verts[tv[tri, 1]], verts[tv[tri, 2]]
    e1, e2, q = b - a, c - a, loc - a
    d00, d01, d11 = (e1 * e1).sum(1), (e1 * e2).sum(1), (e2 * e2).sum(1)
    d20, d21 = (q * e1).sum(1), (q * e2).sum(1)
    den = d00 * d11 - d01 * d01
    den = np.where(np.abs(den) > 1e-24, den, 1.0)
    bv = (d11 * d20 - d01 * d21) / den
    bw = (d00 * d21 - d01 * d20) / den
    bary = np.clip(np.stack([1.0 - bv - bw, bv, bw], axis=1), 0.0, 1.0)
    bary /= np.maximum(bary.sum(1, keepdims=True), 1e-12)
    _PROJ_CACHE.clear()
    _PROJ_CACHE[key] = (tri, bary)
    return tri, bary


def _profile(t, base, mid, tip):
    """Base -> middle -> tip share along the feather (smooth piecewise blend)."""
    first = t < 0.5
    u = np.where(first, t / 0.5, (t - 0.5) / 0.5)
    u = u * u * (3.0 - 2.0 * u)
    return np.where(first, base + (mid - base) * u, mid + (tip - mid) * u)


def _geometric_vertex_normals(mesh):
    """Area-weighted vertex normals computed from the positions, (n, 3).

    Blender 5.1 returns the custom normals from `vertex_normals` AND `polygon_normals` once a mesh
    has any, so the card's own shading normal is rebuilt from its triangles here.
    """
    n = len(mesh.vertices)
    co = np.empty(n * 3, dtype=np.float64)
    mesh.vertices.foreach_get("co", co)
    co = co.reshape(-1, 3)
    mesh.calc_loop_triangles()
    tv = np.empty(len(mesh.loop_triangles) * 3, dtype=np.int64)
    mesh.loop_triangles.foreach_get("vertices", tv)
    tv = tv.reshape(-1, 3)
    face = np.cross(co[tv[:, 1]] - co[tv[:, 0]], co[tv[:, 2]] - co[tv[:, 0]])  # length = 2 x area
    out = np.zeros((n, 3))
    for k in range(3):
        for corner in range(3):
            out[:, k] += np.bincount(tv[:, corner], weights=face[:, k], minlength=n)
    return out / np.maximum(np.linalg.norm(out, axis=1, keepdims=True), 1e-12)


def _smooth_normals(mesh, normals, amount, iterations=12):
    """Blend each vertex normal toward its neighbours' average along the card's edges;
    `amount` (per vertex, 0..1) sets how much each vertex smooths per pass."""
    n = len(mesh.vertices)
    ev = np.empty(len(mesh.edges) * 2, dtype=np.int64)
    mesh.edges.foreach_get("vertices", ev)
    e0, e1 = ev[0::2], ev[1::2]
    deg = np.maximum(np.bincount(ev, minlength=n), 1).astype(np.float64)[:, None]
    w = np.clip(amount, 0.0, 1.0)[:, None]
    out = normals.copy()
    for _ in range(iterations):
        acc = np.stack([np.bincount(e0, weights=out[e1, k], minlength=n) +
                        np.bincount(e1, weights=out[e0, k], minlength=n) for k in range(3)], axis=1)
        out = out * (1.0 - w) + (acc / deg) * w
        out /= np.maximum(np.linalg.norm(out, axis=1, keepdims=True), 1e-12)
    return out


def _clear_custom_normals(mesh):
    attr = mesh.attributes.get("custom_normal")
    if attr is not None:
        mesh.attributes.remove(attr)


def apply_normals(guide_obj):
    """Rebuild the generated cards' custom normals from the per-feather settings. Returns a status."""
    out_obj = guide_obj.arantools_feather.output_object
    if out_obj is None or out_obj.data is None or len(out_obj.data.vertices) == 0:
        return "This Feather Object has no generated feathers yet."
    me = out_obj.data
    layout = fr._generated_layout(guide_obj, out_obj)
    if layout is None:
        return "Generated mesh is out of date — regenerate the feathers."
    pts, vt, cards, gen_id = layout
    vt = fc.base_oriented_t(guide_obj, pts, vt, cards)
    st = feather_normal_settings(guide_obj)
    n_chains = len(st["feather_nrm_amount"])

    blend = np.zeros(len(pts))
    smooth = np.zeros(len(pts))
    for (c_idx, v0, v1, *_), _mirrored in cards:
        if 0 <= c_idx < n_chains and st["feather_nrm_amount"][c_idx] > 0.0:
            t = vt[v0:v1]
            prof = _profile(t, st["feather_nrm_base"][c_idx], st["feather_nrm_mid"][c_idx],
                            st["feather_nrm_tip"][c_idx])
            # Snap Range: past it the transfer fades out and the card keeps its own normals
            x = np.clip((t - st["feather_nrm_range"][c_idx]) / _RANGE_FADE, 0.0, 1.0)
            prof = prof * (1.0 - x * x * (3.0 - 2.0 * x))
            blend[v0:v1] = np.clip(st["feather_nrm_amount"][c_idx] * prof, 0.0, 1.0)
            # Smooth After Base: none at the base, full strength from mid-feather on
            u = np.clip(t / 0.5, 0.0, 1.0)
            smooth[v0:v1] = st["feather_nrm_smooth"][c_idx] * u * u * (3.0 - 2.0 * u)

    if not (blend > 0.0).any():
        _clear_custom_normals(me)
        me.update()
        return "Feather normals: all off (cards use their own normals)."
    src = _source_object(guide_obj)
    if src is None:
        return "Pick a Normal Mesh first."

    source = _source_data(src)
    tri, bary = _project(source, pts, (guide_obj.name, gen_id, src.name, len(pts),
                                       tuple(np.round(pts[:3].ravel(), 5))))
    vn, tv = source[1], source[2]
    n_src = (vn[tv[tri, 0]] * bary[:, 0:1] + vn[tv[tri, 1]] * bary[:, 1:2] + vn[tv[tri, 2]] * bary[:, 2:3])
    n_src /= np.maximum(np.linalg.norm(n_src, axis=1, keepdims=True), 1e-12)

    # The cards' own smooth normals at rest, from the face normals: `vertex_normals` would return
    # the previous custom normals once there are any.
    own = _geometric_vertex_normals(me)
    mw = np.array(out_obj.matrix_world)
    nrm_mat = np.linalg.inv(mw[:3, :3]).T
    own = own.reshape(-1, 3) @ nrm_mat.T
    own /= np.maximum(np.linalg.norm(own, axis=1, keepdims=True), 1e-12)

    # Keep each card's facing: transfer the side of the source normal that matches the card's front
    flip = (own * n_src).sum(1) < 0.0
    n_src[flip] *= -1.0
    world = own * (1.0 - blend)[:, None] + n_src * blend[:, None]
    world /= np.maximum(np.linalg.norm(world, axis=1, keepdims=True), 1e-12)
    if (smooth > 0.0).any():
        world = _smooth_normals(me, world, smooth)

    # Back to the generated mesh's object space
    local = world @ np.linalg.inv(nrm_mat).T
    local /= np.maximum(np.linalg.norm(local, axis=1, keepdims=True), 1e-12)
    me.normals_split_custom_set_from_vertices(local.tolist())
    me.update()
    n_on = len({c for (c, v0, v1, *_), _m in cards if 0 <= c < n_chains and st["feather_nrm_amount"][c] > 0.0})
    return f"Feather normals: {n_on} feather(s) take normals from '{src.name}'."


def reapply_after_regenerate(guide_obj):
    """Called by the generator after a full rebuild (which wipes custom normals)."""
    ns = getattr(guide_obj, "arantools_feather_normals", None)
    if ns is None or not ns.live:
        return
    st = feather_normal_settings(guide_obj)
    if not len(st["feather_nrm_amount"]) or not (st["feather_nrm_amount"] > 0.0).any():
        return
    try:
        apply_normals(guide_obj)
    except Exception as exc:
        print(f"[AranTools] Feather normals warning: {exc}")


def _live_update(context):
    guide_obj = fc.get_feather_guide_from_context(context)
    if guide_obj is not None and guide_obj.arantools_feather_normals.live:
        apply_normals(guide_obj)


# ============================================================================
# Settings + panel proxies
# ============================================================================

def _poll_mesh(self, obj):
    fprops = getattr(obj, "arantools_feather", None)
    return obj is not None and obj.type == 'MESH' and not (
        fprops and (fprops.is_feather_guide or fprops.is_feather_output))


def _source_changed(self, context):
    _SOURCE_CACHE.clear()
    _PROJ_CACHE.clear()
    owner = self.id_data
    if isinstance(owner, bpy.types.Object) and self.live:
        apply_normals(owner)


class ARANTOOLS_PG_FeatherNormals(PropertyGroup):
    source: PointerProperty(
        name="Normal Mesh", type=bpy.types.Object, poll=_poll_mesh, update=_source_changed,
        description="Mesh whose normals the feathers take (empty = the Rig tab's Body Mesh)",
    )
    live: BoolProperty(
        name="Live", default=True,
        description="Update the normals immediately when a slider changes (and after the feathers regenerate)",
    )


def _write_from_ui(attr):
    def _update(self, context):
        if _SYNCING:
            return
        guide_obj = fc.get_feather_guide_from_context(context)
        if guide_obj is None:
            return
        targets = fr.target_feathers(context, guide_obj)
        if not targets:
            return
        _write_feathers(guide_obj, targets, attr, float(getattr(self, attr.replace("feather_nrm_", ""))))
        _live_update(context)
    return _update


class ARANTOOLS_PG_FeatherNormalsUI(PropertyGroup):
    amount: FloatProperty(
        name="Normal From Mesh", default=0.0, min=0.0, max=1.0, subtype='FACTOR',
        update=_write_from_ui("feather_nrm_amount"),
        description="How much the feather takes the Normal Mesh's normals (0 = its own card normals)",
    )
    base: FloatProperty(
        name="Base", default=1.0, min=0.0, max=1.0, subtype='FACTOR',
        update=_write_from_ui("feather_nrm_base"),
        description="Share of the transfer at the feather's base",
    )
    mid: FloatProperty(
        name="Middle", default=0.6, min=0.0, max=1.0, subtype='FACTOR',
        update=_write_from_ui("feather_nrm_mid"),
        description="Share of the transfer at the middle of the feather",
    )
    tip: FloatProperty(
        name="Tip", default=0.2, min=0.0, max=1.0, subtype='FACTOR',
        update=_write_from_ui("feather_nrm_tip"),
        description="Share of the transfer at the feather's tip",
    )
    range: FloatProperty(
        name="Snap Range", default=1.0, min=0.0, max=1.0, subtype='FACTOR',
        update=_write_from_ui("feather_nrm_range"),
        description="How far from the base the normal snap reaches (fraction of the feather); "
                    "past it the feather keeps its own normals",
    )
    smooth: FloatProperty(
        name="Smooth After Base", default=0.0, min=0.0, max=1.0, subtype='FACTOR',
        update=_write_from_ui("feather_nrm_smooth"),
        description="Smooth the normals across the card — none at the base, full strength from mid-feather on",
    )


def sync_normals_ui(context):
    """Show the first targeted feather's normal settings in the panel proxies."""
    global _SYNCING
    guide_obj = fc.get_feather_guide_from_context(context)
    ui = getattr(context.scene, "arantools_feather_normals_ui", None)
    if guide_obj is None or ui is None:
        return
    targets = fr.target_feathers(context, guide_obj)
    if not targets:
        return
    st = feather_normal_settings(guide_obj)
    c = targets[0]
    if not (0 <= c < len(st["feather_nrm_amount"])):
        return
    _SYNCING = True
    try:
        ui.amount = float(st["feather_nrm_amount"][c])
        ui.base = float(st["feather_nrm_base"][c])
        ui.mid = float(st["feather_nrm_mid"][c])
        ui.tip = float(st["feather_nrm_tip"][c])
        ui.range = float(st["feather_nrm_range"][c])
        ui.smooth = float(st["feather_nrm_smooth"][c])
    finally:
        _SYNCING = False


class ARANTOOLS_OT_feather_normals_apply(Operator):
    bl_idname = "arantools.feather_normals_apply"
    bl_label = "Update Feather Normals"
    bl_description = "Rebuild the feathers' custom normals from the Normal Mesh (re-reads it)"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return fc.get_feather_guide_from_context(context) is not None

    def execute(self, context):
        _SOURCE_CACHE.clear()
        _PROJ_CACHE.clear()
        self.report({'INFO'}, apply_normals(fc.get_feather_guide_from_context(context)))
        return {'FINISHED'}


def draw_normals_ui(layout, context, guide_obj):
    ns = guide_obj.arantools_feather_normals
    ui = context.scene.arantools_feather_normals_ui

    box = layout.box()
    col = box.column(align=True)
    col.label(text="Normal Source", icon='NORMALS_VERTEX_FACE')
    col.prop(ns, "source", text="Normal Mesh")
    if ns.source is None:
        src = _source_object(guide_obj)
        hint = col.row()
        hint.enabled = False
        hint.label(text=f"Using '{src.name}'" if src else "Pick a mesh (or set the Rig tab's Body Mesh)")
    col.separator(factor=0.4)
    row = col.row(align=True)
    row.scale_y = 1.3
    row.operator("arantools.feather_normals_apply", text="Update Normals", icon='FILE_REFRESH')
    row.prop(ns, "live", text="Live", toggle=True, icon='AUTO')

    targets = fr.target_feathers(context, guide_obj)
    fbox = layout.box()
    fcol = fbox.column(align=True)
    if fc.is_in_feather_mode(guide_obj):
        title = f"{len(targets)} selected feather{'s' if len(targets) != 1 else ''}"
    else:
        title = f"All feathers ({len(targets)})"
    fcol.label(text=title, icon='STRANDS')
    if not targets:
        hint = fcol.row()
        hint.enabled = False
        hint.label(text="Pick or select feathers to set their normals")
        return
    fcol.prop(ui, "amount", slider=True)
    sub = fcol.column(align=True)
    sub.enabled = ui.amount > 0.0
    sub.separator(factor=0.4)
    sub.label(text="Along the feather:")
    sub.prop(ui, "base", slider=True)
    sub.prop(ui, "mid", slider=True)
    sub.prop(ui, "tip", slider=True)
    sub.separator(factor=0.4)
    sub.prop(ui, "range", slider=True)
    sub.prop(ui, "smooth", slider=True)


# ============================================================================
# Registration
# ============================================================================

classes = (
    ARANTOOLS_PG_FeatherNormals,
    ARANTOOLS_PG_FeatherNormalsUI,
    ARANTOOLS_OT_feather_normals_apply,
)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)
    bpy.types.Object.arantools_feather_normals = PointerProperty(type=ARANTOOLS_PG_FeatherNormals)
    bpy.types.Scene.arantools_feather_normals_ui = PointerProperty(type=ARANTOOLS_PG_FeatherNormalsUI)


def unregister():
    _SOURCE_CACHE.clear()
    _PROJ_CACHE.clear()
    if hasattr(bpy.types.Object, "arantools_feather_normals"):
        del bpy.types.Object.arantools_feather_normals
    if hasattr(bpy.types.Scene, "arantools_feather_normals_ui"):
        del bpy.types.Scene.arantools_feather_normals_ui
    for cls in reversed(classes):
        try:
            bpy.utils.unregister_class(cls)
        except Exception:
            pass
