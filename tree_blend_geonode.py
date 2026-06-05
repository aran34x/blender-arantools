"""
Tree Branch Blend Geonode — hides the seam where each child branch
meets its parent by INJECTING a procedural collar (a flared frustum)
at every junction. The disconnected tubes stay disconnected — the
collar just covers the gap visually, and optional Subdivision Surface
rounds the seam.

Pipeline:

  ┌──────────────────────────────────────────────────────────────────┐
  │ 1. Mask: branch_t < threshold AND branch_depth > 0               │
  │    Picks the bottom-ring verts of every child branch tube.       │
  │                                                                  │
  │ 2. Mesh to Points → one point per masked vert.                   │
  │                                                                  │
  │ 3. Set Position to the midpoint (parent_junction + branch_base)  │
  │    / 2.  All N ring points per branch now coincide.              │
  │                                                                  │
  │ 4. Merge by Distance (tiny threshold) → one point per branch.    │
  │                                                                  │
  │ 5. Build a unit collar mesh: MeshCone with                       │
  │      Radius Top    = 1                                           │
  │      Radius Bottom = Collar Flare                                │
  │      Depth         = 1                                           │
  │      Fill Type     = None                                        │
  │                                                                  │
  │ 6. Instance the collar on each junction point.                   │
  │                                                                  │
  │ 7. Rotate instances: align local +Z with                         │
  │      axis = branch_base - parent_junction                        │
  │                                                                  │
  │ 8. Scale instances by (radius, radius, |axis|), so the collar's  │
  │    top radius matches the child branch's radius and its length   │
  │    matches the parent-to-child distance.                         │
  │                                                                  │
  │ 9. Realize Instances → the cone verts inherit branch_id,         │
  │    branch_t, branch_base_*, etc. from the source point, so the   │
  │    UV geonode downstream stamps them correctly.                  │
  │                                                                  │
  │ 10. Join Geometry: original tubes + collars.                     │
  │                                                                  │
  │ 11. Merge by Distance (Merge Distance): welds the collar's top   │
  │     ring to the child's base ring (same N verts at same XYZ).    │
  │                                                                  │
  │ 12. Subdivision Surface (Level = Smoothing Subdivisions): rounds │
  │     the junction profile, hiding any remaining seam.             │
  └──────────────────────────────────────────────────────────────────┘

Requires the skeleton tool to have written `parent_junction_x/y/z`,
`branch_base_x/y/z`, `branch_t`, `branch_depth`, and `radius` on the
skeleton — they propagate through Mesh→Curve→Mesh to the tube mesh.
"""

import bpy
from bpy.types import Operator


GEONODE_BLEND_NAME = "AranTools_TreeBranchBlend"


def _ensure_blend_geonode_group(rebuild=False):
    existing = bpy.data.node_groups.get(GEONODE_BLEND_NAME)
    if existing is not None and not rebuild:
        return existing
    if existing is not None:
        existing.nodes.clear()
        for item in list(existing.interface.items_tree):
            try:
                existing.interface.remove(item)
            except Exception:
                pass
        nt = existing
    else:
        nt = bpy.data.node_groups.new(GEONODE_BLEND_NAME,
                                       'GeometryNodeTree')

    # ── Interface ───────────────────────────────────────────────────
    nt.interface.new_socket(
        "Geometry", in_out='INPUT', socket_type='NodeSocketGeometry'
    )
    thr = nt.interface.new_socket(
        "Base Ring Threshold", in_out='INPUT',
        socket_type='NodeSocketFloat'
    )
    thr.default_value = 0.05
    thr.min_value = 0.0
    thr.max_value = 0.5
    thr.description = ("branch_t below this counts as 'base ring' — the "
                        "set of verts that the collar will weld to")

    flare = nt.interface.new_socket(
        "Collar Flare", in_out='INPUT', socket_type='NodeSocketFloat'
    )
    flare.default_value = 1.8
    flare.min_value = 1.0
    flare.max_value = 5.0
    flare.description = ("Bottom radius of the collar relative to the "
                          "child branch's radius. 1.0 = cylinder, 1.8 "
                          "= a moderate skirt that flares onto the parent")

    sides = nt.interface.new_socket(
        "Collar Sides", in_out='INPUT', socket_type='NodeSocketInt'
    )
    sides.default_value = 8
    sides.min_value = 3
    sides.max_value = 64
    sides.description = ("Vertex count around the collar — match this "
                          "to the tubes geonode's Profile Resolution so "
                          "the collar's top ring welds cleanly to the "
                          "child branch base ring")

    merge_d = nt.interface.new_socket(
        "Merge Distance", in_out='INPUT', socket_type='NodeSocketFloat'
    )
    merge_d.default_value = 0.02
    merge_d.min_value = 0.0
    merge_d.max_value = 1.0
    merge_d.description = ("Welds the collar's top ring to the child "
                            "branch's base ring after joining")

    smooth = nt.interface.new_socket(
        "Smoothing Subdivisions", in_out='INPUT',
        socket_type='NodeSocketInt'
    )
    smooth.default_value = 1
    smooth.min_value = 0
    smooth.max_value = 3
    smooth.description = ("Subdivision Surface levels applied at the "
                          "end. 1 = visible smoothing, 2 = soft, "
                          "3 = expensive")

    nt.interface.new_socket(
        "Geometry", in_out='OUTPUT', socket_type='NodeSocketGeometry'
    )

    # ── Helpers ─────────────────────────────────────────────────────
    def mk(typ, x, y, **kw):
        n = nt.nodes.new(typ)
        n.location = (x, y)
        for k, v in kw.items():
            setattr(n, k, v)
        return n

    def link(o, oid, i, iid):
        nt.links.new(o.outputs[oid], i.inputs[iid])

    def first(seq, predicate):
        return next((x for x in seq if predicate(x)), None)

    # ── Group I/O ───────────────────────────────────────────────────
    gi = mk('NodeGroupInput',  -2800, 0)
    go = mk('NodeGroupOutput',  2800, 0)

    # ── Read skeleton attrs (POINT domain on the tube mesh) ─────────
    def named_float(name, x, y):
        n = mk('GeometryNodeInputNamedAttribute', x, y, data_type='FLOAT')
        n.inputs['Name'].default_value = name
        return n

    def named_int(name, x, y):
        n = mk('GeometryNodeInputNamedAttribute', x, y, data_type='INT')
        n.inputs['Name'].default_value = name
        return n

    bt = named_float('branch_t',          -2500, 700)
    bd = named_int  ('branch_depth',      -2500, 500)
    bbx = named_float('branch_base_x',    -2500, 200)
    bby = named_float('branch_base_y',    -2500, 0)
    bbz = named_float('branch_base_z',    -2500, -200)
    pjx = named_float('parent_junction_x',-2500, -500)
    pjy = named_float('parent_junction_y',-2500, -700)
    pjz = named_float('parent_junction_z',-2500, -900)

    # ── Mask: branch_t < threshold AND branch_depth > 0 ─────────────
    bt_low = mk('FunctionNodeCompare', -2200, 700,
                data_type='FLOAT', operation='LESS_THAN')
    link(bt, 'Attribute',           bt_low, 0)
    link(gi, 'Base Ring Threshold', bt_low, 1)

    bd_pos = mk('FunctionNodeCompare', -2200, 500,
                data_type='INT', operation='GREATER_THAN')
    link(bd, 'Attribute', bd_pos, 0)
    bd_pos.inputs[1].default_value = 0

    mask = mk('FunctionNodeBooleanMath', -1900, 600, operation='AND')
    link(bt_low, 'Result', mask, 0)
    link(bd_pos, 'Result', mask, 1)

    # ── Build branch_base_pos and parent_junction_pos vectors ───────
    base_pos = mk('ShaderNodeCombineXYZ', -2200, 100)
    link(bbx, 'Attribute', base_pos, 'X')
    link(bby, 'Attribute', base_pos, 'Y')
    link(bbz, 'Attribute', base_pos, 'Z')

    parent_pos = mk('ShaderNodeCombineXYZ', -2200, -600)
    link(pjx, 'Attribute', parent_pos, 'X')
    link(pjy, 'Attribute', parent_pos, 'Y')
    link(pjz, 'Attribute', parent_pos, 'Z')

    # axis = base_pos - parent_pos, length = |axis|, midpoint = (a+b)/2
    axis = mk('ShaderNodeVectorMath', -1900, -100, operation='SUBTRACT')
    link(base_pos,   'Vector', axis, 0)
    link(parent_pos, 'Vector', axis, 1)

    axis_len = mk('ShaderNodeVectorMath', -1700, -100, operation='LENGTH')
    link(axis, 'Vector', axis_len, 0)

    midsum = mk('ShaderNodeVectorMath', -1900, -400, operation='ADD')
    link(base_pos,   'Vector', midsum, 0)
    link(parent_pos, 'Vector', midsum, 1)
    mid_pos = mk('ShaderNodeVectorMath', -1700, -400, operation='SCALE')
    link(midsum, 'Vector', mid_pos, 0)
    mid_pos.inputs['Scale'].default_value = 0.5

    # ── Mesh to Points (selected base-ring verts) ───────────────────
    mtp = mk('GeometryNodeMeshToPoints', -1500, 800, mode='VERTICES')
    link(gi,   'Geometry', mtp, 'Mesh')
    link(mask, 'Boolean',  mtp, 'Selection')

    # ── Move points to midpoint so all N per branch coincide ────────
    set_mid = mk('GeometryNodeSetPosition', -1200, 800)
    link(mtp,     'Points', set_mid, 'Geometry')
    link(mid_pos, 'Vector', set_mid, 'Position')

    # ── Merge by Distance — one point per branch ────────────────────
    merge_pts = mk('GeometryNodeMergeByDistance', -900, 800)
    link(set_mid, 'Geometry', merge_pts, 'Geometry')
    merge_pts.inputs['Distance'].default_value = 0.001

    # ── Build the unit collar mesh (cone frustum) ───────────────────
    cone = mk('GeometryNodeMeshCone', -1500, -1000, fill_type='NONE')
    link(gi,    'Collar Sides', cone, 'Vertices')
    cone.inputs['Side Segments'].default_value = 1
    cone.inputs['Fill Segments'].default_value = 1
    cone.inputs['Radius Top'].default_value = 1.0
    link(gi,    'Collar Flare', cone, 'Radius Bottom')
    cone.inputs['Depth'].default_value = 1.0

    # Translate so bottom is at Z=0, top at Z=1.
    cone_shift = mk('GeometryNodeTransform', -1200, -1000)
    link(cone, 'Mesh', cone_shift, 'Geometry')
    cone_shift.inputs['Translation'].default_value = (0.0, 0.0, 0.5)

    # ── Instance the collar on the merged points ────────────────────
    iop = mk('GeometryNodeInstanceOnPoints', -600, 0)
    link(merge_pts,  'Geometry', iop, 'Points')
    link(cone_shift, 'Geometry', iop, 'Instance')

    # ── Rotate instances: align +Z to (base - parent) ───────────────
    # On the merged points the per-instance attrs survived, so we
    # re-read parent_junction_* and branch_base_* on the points stream
    # to compute the axis per instance.
    bt_p   = named_float('branch_t',          -1200, 1100)
    bbx_p  = named_float('branch_base_x',     -1200, 900)
    bby_p  = named_float('branch_base_y',     -1200, 700)
    bbz_p  = named_float('branch_base_z',     -1200, 500)
    pjx_p  = named_float('parent_junction_x', -1200, 300)
    pjy_p  = named_float('parent_junction_y', -1200, 100)
    pjz_p  = named_float('parent_junction_z', -1200, -100)
    r_p    = named_float('radius',            -1200, -300)

    base_pos_p = mk('ShaderNodeCombineXYZ', -1000, 800)
    link(bbx_p, 'Attribute', base_pos_p, 'X')
    link(bby_p, 'Attribute', base_pos_p, 'Y')
    link(bbz_p, 'Attribute', base_pos_p, 'Z')

    parent_pos_p = mk('ShaderNodeCombineXYZ', -1000, 200)
    link(pjx_p, 'Attribute', parent_pos_p, 'X')
    link(pjy_p, 'Attribute', parent_pos_p, 'Y')
    link(pjz_p, 'Attribute', parent_pos_p, 'Z')

    axis_p = mk('ShaderNodeVectorMath', -800, 500, operation='SUBTRACT')
    link(base_pos_p,   'Vector', axis_p, 0)
    link(parent_pos_p, 'Vector', axis_p, 1)

    axis_len_p = mk('ShaderNodeVectorMath', -600, 500, operation='LENGTH')
    link(axis_p, 'Vector', axis_len_p, 0)

    align = mk('FunctionNodeAlignEulerToVector', -400, 300, axis='Z',
               pivot_axis='AUTO')
    link(axis_p, 'Vector', align, 'Vector')

    rotate = mk('GeometryNodeRotateInstances', -100, 0)
    link(iop,   'Instances', rotate, 'Instances')
    link(align, 'Rotation',  rotate, 'Rotation')

    # ── Scale instances: (radius, radius, height) ───────────────────
    scale_vec = mk('ShaderNodeCombineXYZ', -100, -400)
    link(r_p,        'Attribute', scale_vec, 'X')
    link(r_p,        'Attribute', scale_vec, 'Y')
    link(axis_len_p, 'Value',     scale_vec, 'Z')

    scale = mk('GeometryNodeScaleInstances', 200, 0)
    link(rotate,    'Instances', scale, 'Instances')
    link(scale_vec, 'Vector',    scale, 'Scale')

    # ── Move instances to parent_pos so the collar bottom lands at  ─
    #    the junction (top will land at base_pos after scale+rotate)
    set_at_parent = mk('GeometryNodeSetPosition', 500, 0)
    link(scale,        'Instances', set_at_parent, 'Geometry')
    link(parent_pos_p, 'Vector',    set_at_parent, 'Position')

    # ── Realize Instances ──────────────────────────────────────────
    realize = mk('GeometryNodeRealizeInstances', 800, 0)
    link(set_at_parent, 'Geometry', realize, 'Geometry')

    # ── Join collars with the input mesh ────────────────────────────
    join = mk('GeometryNodeJoinGeometry', 1100, 0)
    # NOTE: join input is multi — add input mesh first, then collars
    link(gi,      'Geometry', join, 'Geometry')
    link(realize, 'Geometry', join, 'Geometry')

    # ── Merge by Distance to weld collar tops to child base rings ──
    weld = mk('GeometryNodeMergeByDistance', 1500, 0)
    link(join, 'Geometry',       weld, 'Geometry')
    link(gi,   'Merge Distance', weld, 'Distance')

    # ── Subdivision Surface (optional smoothing) ────────────────────
    subd = mk('GeometryNodeSubdivisionSurface', 1900, 0)
    link(weld, 'Mesh',                   subd, 'Mesh')
    link(gi,   'Smoothing Subdivisions', subd, 'Level')

    link(subd, 'Mesh', go, 'Geometry')

    return nt


# ============================================================================
# Operators
# ============================================================================

class ARANTOOLS_OT_TreeAddBlendGeonode(Operator):
    """Build the Tree Branch Blend node group if missing, then add it as
a Geometry Nodes modifier on the active wood mesh. Place AFTER Branch
Tubes and BEFORE Branch UV in the modifier stack."""
    bl_idname  = "arantools.tree_add_blend_geonode"
    bl_label   = "Add Branch Blend Geonode"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        obj = context.active_object
        return obj is not None and obj.type == 'MESH'

    def execute(self, context):
        obj = context.active_object
        ng = _ensure_blend_geonode_group()
        for m in obj.modifiers:
            if m.type == 'NODES' and m.node_group == ng:
                self.report({'INFO'},
                            f"'{GEONODE_BLEND_NAME}' modifier already on "
                            f"'{obj.name}'.")
                return {'CANCELLED'}
        mod = obj.modifiers.new(name="Tree Branch Blend", type='NODES')
        mod.node_group = ng
        self.report({'INFO'},
                    f"Added '{GEONODE_BLEND_NAME}' to '{obj.name}'.")
        return {'FINISHED'}


class ARANTOOLS_OT_TreeRebuildBlendGeonode(Operator):
    """Rebuild the Tree Branch Blend node group from source."""
    bl_idname  = "arantools.tree_rebuild_blend_geonode"
    bl_label   = "Rebuild Blend Node Group"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        _ensure_blend_geonode_group(rebuild=True)
        self.report({'INFO'}, f"Rebuilt '{GEONODE_BLEND_NAME}' from source.")
        return {'FINISHED'}


classes = [
    ARANTOOLS_OT_TreeAddBlendGeonode,
    ARANTOOLS_OT_TreeRebuildBlendGeonode,
]


def register():
    for cls in classes:
        bpy.utils.register_class(cls)


def unregister():
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
