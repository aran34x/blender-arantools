"""
Tree Branch Tubes Geonode — sweep a circular profile along each branch
of a skeleton mesh, producing one closed-tip / open-base tube per branch.

Pipeline:
  1. Read `is_branch_entry` (BOOL, EDGE) and delete those edges. Each
     entry edge bridges a child branch's first vertex to its junction in
     the parent branch; cutting them splits the skeleton into N
     disconnected per-branch chains.
  2. Mesh to Curve → one curve per branch. Point attributes (radius, tilt,
     branch_t, is_underground, branch_base_z, …) carry over to the curve.
  3. Stamp `is_start = True` on each curve's first point via
     Endpoint Selection (Start Size = 1, End Size = 0).
  4. Curve to Mesh, Fill Caps = True, profile = circle of N sides.
     Produces a tube per curve with both ends capped, plus an auto-
     generated UV map (UVMap, Face Corner) you can edit afterward.
  5. Capture is_start on FACE domain (FLOAT) — averages the boolean
     across each face's corners. A base-cap N-gon's corners are ALL on
     the start point so its avg = 1.0; sidewall quads average 0.5
     (2 start corners + 2 non-start); tip-cap N-gons average 0.0.
     Faces with avg > 0.99 are base caps → Delete Geometry.

Result: closed tips, open bases (ready to dock onto the parent branch),
each tube is its own connected component carrying the skeleton's
attributes for the downstream UV geonode to read.
"""

import bpy
from bpy.types import Operator


GEONODE_NAME = "AranTools_TreeBranchTubes"


def _ensure_geonode_group(rebuild=False):
    existing = bpy.data.node_groups.get(GEONODE_NAME)
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
        nt = bpy.data.node_groups.new(GEONODE_NAME, 'GeometryNodeTree')

    # ── Interface ───────────────────────────────────────────────────
    nt.interface.new_socket(
        "Geometry", in_out='INPUT', socket_type='NodeSocketGeometry'
    )
    res = nt.interface.new_socket(
        "Profile Resolution", in_out='INPUT', socket_type='NodeSocketInt'
    )
    res.default_value = 8
    res.min_value = 3
    res.max_value = 64
    res.description = "Number of sides on each branch tube"

    rmul = nt.interface.new_socket(
        "Radius Multiplier", in_out='INPUT', socket_type='NodeSocketFloat'
    )
    rmul.default_value = 1.0
    rmul.min_value = 0.001
    rmul.max_value = 10.0
    rmul.description = ("Scales the per-vertex `radius` attribute from the "
                         "skeleton. 1.0 = use as-is")

    nt.interface.new_socket(
        "Geometry", in_out='OUTPUT', socket_type='NodeSocketGeometry'
    )

    def mk(typ, x, y, **kw):
        n = nt.nodes.new(typ)
        n.location = (x, y)
        for k, v in kw.items():
            setattr(n, k, v)
        return n

    def link(o_node, o_id, i_node, i_id):
        nt.links.new(o_node.outputs[o_id], i_node.inputs[i_id])

    gi = mk('NodeGroupInput',  -2400, 0)
    go = mk('NodeGroupOutput',  2400, 0)

    # ── Step 1: delete the bridge edges flagged by the skeleton tool ──
    # `is_branch_entry` lives on EDGE domain and is True on exactly the
    # edges that connect a child branch's first vertex to its junction in
    # the parent. Cutting them splits the skeleton into per-branch chains.
    entry_attr = mk('GeometryNodeInputNamedAttribute', -2000, 0,
                    data_type='BOOLEAN')
    entry_attr.inputs['Name'].default_value = 'is_branch_entry'

    del_cross = mk('GeometryNodeDeleteGeometry', -1500, 0,
                    domain='EDGE', mode='EDGE_FACE')
    link(gi,         'Geometry',  del_cross, 'Geometry')
    link(entry_attr, 'Attribute', del_cross, 'Selection')

    # ── Step 2: Mesh to Curve (one curve per branch) ─────────────────
    m2c = mk('GeometryNodeMeshToCurve', -1300, 0)
    link(del_cross, 'Geometry', m2c, 'Mesh')

    # ── Step 3: stamp is_start = True on each curve's first point ────
    endpoint = mk('GeometryNodeCurveEndpointSelection', -1100, -200)
    endpoint.inputs['Start Size'].default_value = 1
    endpoint.inputs['End Size'].default_value   = 0

    store_start = mk('GeometryNodeStoreNamedAttribute', -900, 0,
                      data_type='BOOLEAN', domain='POINT')
    store_start.inputs['Name'].default_value = 'is_start'
    link(m2c,      'Curve',     store_start, 'Geometry')
    link(endpoint, 'Selection', store_start, 'Value')

    # ── Apply radius multiplier and set as curve native `radius` ─────
    radius_attr = mk('GeometryNodeInputNamedAttribute', -900, 250,
                     data_type='FLOAT')
    radius_attr.inputs['Name'].default_value = 'radius'

    mul_r = mk('ShaderNodeMath', -700, 250, operation='MULTIPLY')
    link(radius_attr, 'Attribute',        mul_r, 0)
    link(gi,          'Radius Multiplier', mul_r, 1)

    set_radius = mk('GeometryNodeSetCurveRadius', -500, 0)
    link(store_start, 'Geometry', set_radius, 'Curve')
    link(mul_r,       'Value',    set_radius, 'Radius')

    # ── Step 4: Curve to Mesh with capped profile circle ─────────────
    profile = mk('GeometryNodeCurvePrimitiveCircle', -500, -300, mode='RADIUS')
    profile.inputs['Radius'].default_value = 1.0  # final size = curve radius
    link(gi, 'Profile Resolution', profile, 'Resolution')

    c2m = mk('GeometryNodeCurveToMesh', -200, 0)
    c2m.inputs['Fill Caps'].default_value = True
    link(set_radius, 'Geometry', c2m, 'Curve')
    link(profile,    'Curve',    c2m, 'Profile Curve')

    # ── Step 5: detect base caps by per-face average of is_start ─────
    is_start_attr = mk('GeometryNodeInputNamedAttribute', 100, -300,
                       data_type='BOOLEAN')
    is_start_attr.inputs['Name'].default_value = 'is_start'

    # Capture as FLOAT on FACE domain — averages booleans across each
    # face's corners (True = 1, False = 0). Base cap face avg = 1.0.
    capture_face = mk('GeometryNodeCaptureAttribute', 400, 0,
                       data_type='FLOAT', domain='FACE')
    link(c2m,           'Mesh',      capture_face, 'Geometry')
    link(is_start_attr, 'Attribute', capture_face, 'Value')

    is_base_cap = mk('FunctionNodeCompare', 700, -100,
                     data_type='FLOAT', operation='GREATER_THAN')
    is_base_cap.inputs[1].default_value = 0.99
    link(capture_face, 'Attribute', is_base_cap, 0)

    del_base = mk('GeometryNodeDeleteGeometry', 900, 0,
                   domain='FACE', mode='ONLY_FACE')
    link(capture_face, 'Geometry', del_base, 'Geometry')
    link(is_base_cap,  'Result',   del_base, 'Selection')

    # ── Output ───────────────────────────────────────────────────────
    link(del_base, 'Geometry', go, 'Geometry')

    return nt


# ============================================================================
# Operators
# ============================================================================

class ARANTOOLS_OT_TreeAddTubesGeonode(Operator):
    """Build the Tree Branch Tubes node group if missing, then add it as
a Geometry Nodes modifier on the active mesh — should be a vertex+edge
skeleton authored with the Branch Skeleton tool (needs `is_branch_entry`
on EDGE domain and `radius` on POINT domain)."""
    bl_idname  = "arantools.tree_add_tubes_geonode"
    bl_label   = "Add Branch Tubes Geonode"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        obj = context.active_object
        return obj is not None and obj.type == 'MESH'

    def execute(self, context):
        obj = context.active_object
        ng = _ensure_geonode_group()
        for m in obj.modifiers:
            if m.type == 'NODES' and m.node_group == ng:
                self.report({'INFO'},
                            f"'{GEONODE_NAME}' modifier already present on "
                            f"'{obj.name}'.")
                return {'CANCELLED'}
        mod = obj.modifiers.new(name="Tree Branch Tubes", type='NODES')
        mod.node_group = ng
        self.report({'INFO'},
                    f"Added '{GEONODE_NAME}' modifier to '{obj.name}'.")
        return {'FINISHED'}


class ARANTOOLS_OT_TreeRebuildTubesGeonode(Operator):
    """Rebuild the Tree Branch Tubes node group from the addon's current
definition. Existing modifier references stay valid; only the group's
contents are replaced. Manual edits inside the group are lost."""
    bl_idname  = "arantools.tree_rebuild_tubes_geonode"
    bl_label   = "Rebuild Tubes Node Group"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        _ensure_geonode_group(rebuild=True)
        self.report({'INFO'}, f"Rebuilt '{GEONODE_NAME}' from source.")
        return {'FINISHED'}


# ============================================================================
# Registration
# ============================================================================

classes = [
    ARANTOOLS_OT_TreeAddTubesGeonode,
    ARANTOOLS_OT_TreeRebuildTubesGeonode,
]


def register():
    for cls in classes:
        bpy.utils.register_class(cls)


def unregister():
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
