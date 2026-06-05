"""
Tree Branch UV Geonode — bakes the SpeedTree-style identification UVs
and depth-tier alpha onto the WOOD tube mesh.

What we learned from inspecting the reference asset + the Unreal master
material functions (MF_VertexColorID + MF_FoliageHeight):
  - The wood shader does NOT sample per-vertex wind amplitude.
  - Wind branch tier comes from VERTEX COLOR R (the shader does
    `MF_VertexColorID.R = round(VertexColor.R * 255)` and feeds that
    int into the "Wind Tier" pin).
  - Tree height (centimeters) comes from `UVMap2.G * (formula in
    MF_FoliageHeight)` plus the per-instance Z-scale; we just need to
    make sure UVMap2.V carries the tree's max Z.
  - The reference's Color.A has 4 discrete levels but the wind tier
    isn't read from there — it's likely AO / stiffness / unused.
    Leaving A = 1 to match the reference's typical case.

So this geonode writes (all FACE_CORNER domain):
  - UVMap2    (Float2): (branch_base_z, tree_max_z)
  - UVMap3    (Float2): (0, 1)  — placeholder constant
  - UVMap1    (Float2): (branch_base_x, branch_base_y + 1)
        The +1 pre-compensates for the shader's Y = (1 - V) * -1 = V - 1
        axis-flip step, so the shader recovers branch_base_y exactly.
  - Attribute (Color):
        Trunk          (depth 0)  : (0.0001, 0, 0, 1)
        Any branch     (depth ≥1) : (0.001,  0, 0, branch_t)
            Alpha ramps 0→1 along each branch's length, so every
            depth transition gets the dark "blend point" at its base.

The leaves use a different encoding (per-leaf wind amp in Color.B);
that's handled by a separate leaf-side geonode, not here.

UVmap_0 (the artist's texture UV) is left untouched.
"""

import bpy
from bpy.types import Operator


GEONODE_NAME = "AranTools_TreeBranchUV"


# ============================================================================
# Node-tree construction
# ============================================================================

def _ensure_geonode_group(rebuild=False):
    """Get or build the node group. Pass rebuild=True to overwrite edits."""
    existing = bpy.data.node_groups.get(GEONODE_NAME)
    if existing is not None and not rebuild:
        return existing
    if existing is not None:
        # Rebuilding: clear modifiers that point at the old group first?
        # Safer to keep modifier references intact and just clear nodes/links.
        existing.nodes.clear()
        # Wipe the interface too — recreated below
        for item in list(existing.interface.items_tree):
            try:
                existing.interface.remove(item)
            except Exception:
                pass
        nt = existing
    else:
        nt = bpy.data.node_groups.new(GEONODE_NAME, 'GeometryNodeTree')

    # ── Interface (inputs / outputs of the group) ─────────────────────
    nt.interface.new_socket(
        "Geometry", in_out='INPUT', socket_type='NodeSocketGeometry'
    )
    # Wind amplitude / height-falloff inputs intentionally removed — the
    # wood shader doesn't read per-vertex wind from color (the reference
    # asset's wood color is all zero on RGB; alpha holds a depth tier).
    nt.interface.new_socket(
        "Geometry", in_out='OUTPUT', socket_type='NodeSocketGeometry'
    )

    # ── Local helpers ────────────────────────────────────────────────
    def mk(typ, x, y, **kw):
        node = nt.nodes.new(typ)
        node.location = (x, y)
        for k, v in kw.items():
            setattr(node, k, v)
        return node

    def link(out_node, out_id, in_node, in_id):
        nt.links.new(out_node.outputs[out_id], in_node.inputs[in_id])

    # ── Group I/O ────────────────────────────────────────────────────
    gi = mk('NodeGroupInput',  -1800, 0)
    go = mk('NodeGroupOutput',  1800, 0)

    # ── Tree max Z: needed for UVMap2.V (the per-tree max-Z pivot) ──
    pos = mk('GeometryNodeInputPosition', -1600, -300)
    sep = mk('ShaderNodeSeparateXYZ',     -1400, -300)
    link(pos, 'Position', sep, 'Vector')

    stat = mk('GeometryNodeAttributeStatistic', -1200, -300,
              data_type='FLOAT', domain='POINT')
    link(gi,  'Geometry', stat, 'Geometry')
    link(sep, 'Z',        stat, 'Attribute')

    # ── Per-branch pivot: read branch_base_z directly ────────────────
    base_attr = mk('GeometryNodeInputNamedAttribute', -1000, 300,
                   data_type='FLOAT')
    base_attr.inputs['Name'].default_value = 'branch_base_z'

    # ── Compose UVMap2 = (branch_base_z, tree_max_z, 0) ─────────────
    uv2_combine = mk('ShaderNodeCombineXYZ', -600, 300)
    link(base_attr, 'Attribute', uv2_combine, 'X')
    link(stat,      'Max',       uv2_combine, 'Y')

    # ── Compose UVMap3 = (0, 1, 0) ──────────────────────────────────
    uv3_combine = mk('ShaderNodeCombineXYZ', -600, 100)
    uv3_combine.inputs['X'].default_value = 0.0
    uv3_combine.inputs['Y'].default_value = 1.0
    uv3_combine.inputs['Z'].default_value = 0.0

    # ── Color = depth-conditional (R, G, B, A) ───────────────────────
    # Spec (revised after detailed inspection of the reference):
    #   Trunk  (depth 0)   : (0.0001, 0, 0, 1)
    #   Branch (depth ≥ 1) : (0.001,  0, 0, branch_t)
    #       Alpha ramps 0→1 along each branch's length, so the "blend
    #       points" at every depth transition (where a child branch
    #       sprouts) get the low-alpha falloff.
    #
    # CombineColor (RGB mode) has no Alpha input, so we author the color
    # via ShaderNodeMix in RGBA mode (A and B sockets accept full RGBA
    # default values). Two Mix nodes:
    #   1. mix_branch = lerp(branch_base=(0.001,0,0,0),
    #                        branch_tip =(0.001,0,0,1), factor=branch_t)
    #   2. mix_final  = pick(mix_branch, trunk=(0.0001,0,0,1),
    #                        factor=is_trunk)
    depth_attr = mk('GeometryNodeInputNamedAttribute', -1400, -200,
                    data_type='INT')
    depth_attr.inputs['Name'].default_value = 'branch_depth'

    bt_attr = mk('GeometryNodeInputNamedAttribute', -1400, -400,
                 data_type='FLOAT')
    bt_attr.inputs['Name'].default_value = 'branch_t'

    is_trunk = mk('FunctionNodeCompare', -1100, -100,
                  data_type='INT', operation='EQUAL')
    link(depth_attr, 'Attribute', is_trunk, 'A')
    is_trunk.inputs['B'].default_value = 0

    def _mix_color_inputs(node):
        return [s for s in node.inputs
                if s.bl_idname == 'NodeSocketColor']

    def _mix_factor_input(node):
        for s in node.inputs:
            if s.name == 'Factor' and s.bl_idname.startswith('NodeSocketFloat'):
                return s
        for s in node.inputs:
            if s.bl_idname.startswith('NodeSocketFloat'):
                return s
        raise RuntimeError(
            f"Could not find Factor socket on Mix node {node.name}")

    def _mix_color_output(node):
        return next(s for s in node.outputs
                    if s.bl_idname == 'NodeSocketColor')

    # 1. Branch color with alpha ramping along branch_t.
    mix_branch = mk('ShaderNodeMix', -800, -400, data_type='RGBA')
    ca, cb = _mix_color_inputs(mix_branch)
    ca.default_value = (0.001, 0.0, 0.0, 0.0)
    cb.default_value = (0.001, 0.0, 0.0, 1.0)
    nt.links.new(bt_attr.outputs['Attribute'], _mix_factor_input(mix_branch))

    # 2. Pick between branch color and trunk constant by depth==0.
    mix_final = mk('ShaderNodeMix', -400, -200, data_type='RGBA')
    ca, cb = _mix_color_inputs(mix_final)
    nt.links.new(_mix_color_output(mix_branch), ca)   # any branch
    cb.default_value = (0.0001, 0.0, 0.0, 1.0)        # trunk
    nt.links.new(is_trunk.outputs['Result'],
                 _mix_factor_input(mix_final))

    final_color_socket = _mix_color_output(mix_final)

    # ── Per-branch pivot X / Y ──────────────────────────────────────
    bx_attr = mk('GeometryNodeInputNamedAttribute', -1000, 600,
                 data_type='FLOAT')
    bx_attr.inputs['Name'].default_value = 'branch_base_x'

    by_attr = mk('GeometryNodeInputNamedAttribute', -1000, 450,
                 data_type='FLOAT')
    by_attr.inputs['Name'].default_value = 'branch_base_y'

    # Compose UVMap1 = (branch_base_x, branch_base_y + 1)
    # The +1 pre-compensates for the shader's "Fix Axis" step which does
    # Y = (1 - V) * -1 = V - 1. We bake V = Y + 1 so the shader recovers
    # exactly branch_base_y. Without this, every branch's pivot Y is off
    # by 1m and the tree visibly spirals around a displaced Y axis.
    by_plus = mk('ShaderNodeMath', -800, 500, operation='ADD')
    link(by_attr, 'Attribute', by_plus, 0)
    by_plus.inputs[1].default_value = 1.0

    lmap_combine = mk('ShaderNodeCombineXYZ', -600, 500)
    link(bx_attr, 'Attribute', lmap_combine, 'X')
    link(by_plus, 'Value',     lmap_combine, 'Y')

    # ── Store UVs in TexCoord-INDEX order ───────────────────────────
    # Blender exports UV layers in their order in `mesh.uv_layers`,
    # which is the CREATION ORDER. That becomes Unreal's TexCoord index.
    # The artist UV ('UVMap', auto-created by Curve to Mesh) is already
    # at index 0. We need:
    #   index 1 → UVMap1 (pivot X / Y)
    #   index 2 → UVMap2 (branch_base_z, tree_max_z)
    #   index 3 → UVMap3 ((0, 1) placeholder)
    # so we MUST store them in this order on the geometry stream.
    s_lmap = mk('GeometryNodeStoreNamedAttribute', 0, 500,
                data_type='FLOAT2', domain='CORNER')
    s_lmap.inputs['Name'].default_value = 'UVMap1'
    link(gi,           'Geometry', s_lmap, 'Geometry')
    link(lmap_combine, 'Vector',   s_lmap, 'Value')

    s_uv2 = mk('GeometryNodeStoreNamedAttribute', 400, 300,
               data_type='FLOAT2', domain='CORNER')
    s_uv2.inputs['Name'].default_value = 'UVMap2'
    link(s_lmap,      'Geometry', s_uv2, 'Geometry')
    link(uv2_combine, 'Vector',   s_uv2, 'Value')

    s_uv3 = mk('GeometryNodeStoreNamedAttribute', 800, 100,
               data_type='FLOAT2', domain='CORNER')
    s_uv3.inputs['Name'].default_value = 'UVMap3'
    link(s_uv2,       'Geometry', s_uv3, 'Geometry')
    link(uv3_combine, 'Vector',   s_uv3, 'Value')

    # ── Store color "Attribute" = depth-conditional RGBA ─────────────
    s_col = mk('GeometryNodeStoreNamedAttribute', 1200, -100,
               data_type='FLOAT_COLOR', domain='CORNER')
    s_col.inputs['Name'].default_value = 'Attribute'
    link(s_uv3, 'Geometry', s_col, 'Geometry')
    nt.links.new(final_color_socket, s_col.inputs['Value'])

    # ── Output ───────────────────────────────────────────────────────
    link(s_col, 'Geometry', go, 'Geometry')

    return nt


# ============================================================================
# Tree Leaves UV Geonode — separate encoding for leaf meshes
# ============================================================================
#
# Each leaf vertex looks up the CLOSEST trunk tip (a tube vertex with
# branch_t ≈ 1.0) and inherits that tip's pivot data. So a leaf swaying
# in the wind uses its parent branch's tip as the sway origin — which is
# what the shader's pivot reconstruction expects.
#
# Writes (all FACE_CORNER on the leaves mesh):
#   - UVMap2    : (tip.branch_base_z, tree_max_z)
#   - UVMap3    : (0, 1)
#   - UVMap1    : (tip.branch_base_x, tip.branch_base_y + 1)
#       The +1 pre-compensates for the shader's V-axis flip.
#   - Attribute : (0.001, 0.001, random_per_face, 1)
#         B is a 0-1 random value indexed by face (per-leaf flutter
#         amplitude). R and G match the reference's tiny constant.

GEONODE_LEAVES_NAME = "AranTools_TreeLeavesUV"


def _ensure_leaves_geonode_group(rebuild=False):
    """Get or build the leaves UV node group."""
    existing = bpy.data.node_groups.get(GEONODE_LEAVES_NAME)
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
        nt = bpy.data.node_groups.new(GEONODE_LEAVES_NAME, 'GeometryNodeTree')

    # ── Interface ───────────────────────────────────────────────────
    nt.interface.new_socket(
        "Geometry", in_out='INPUT', socket_type='NodeSocketGeometry'
    )
    trunk_in = nt.interface.new_socket(
        "Trunk", in_out='INPUT', socket_type='NodeSocketObject'
    )
    trunk_in.description = (
        "Reference trunk mesh (post tubes geonode). The leaf vertices "
        "sample the closest tip on this object for their pivot data")
    rng = nt.interface.new_socket(
        "Random Blue Seed", in_out='INPUT', socket_type='NodeSocketInt'
    )
    rng.default_value = 0
    rng.description = "Seed for the per-face random value written into Color.B"
    nt.interface.new_socket(
        "Geometry", in_out='OUTPUT', socket_type='NodeSocketGeometry'
    )

    def mk(typ, x, y, **kw):
        n = nt.nodes.new(typ)
        n.location = (x, y)
        for k, v in kw.items():
            setattr(n, k, v)
        return n

    def link(o, oid, i, iid):
        nt.links.new(o.outputs[oid], i.inputs[iid])

    gi = mk('NodeGroupInput',  -2600, 0)
    go = mk('NodeGroupOutput',  2400, 0)

    # ── Pull trunk geometry, filter to tip verts (branch_t ≥ 0.99) ──
    obj_info = mk('GeometryNodeObjectInfo', -2400, 400)
    obj_info.transform_space = 'RELATIVE'
    link(gi, 'Trunk', obj_info, 'Object')

    branch_t = mk('GeometryNodeInputNamedAttribute', -2400, 200,
                  data_type='FLOAT')
    branch_t.inputs['Name'].default_value = 'branch_t'

    is_tip = mk('FunctionNodeCompare', -2200, 200,
                data_type='FLOAT', operation='GREATER_THAN')
    link(branch_t, 'Attribute', is_tip, 0)
    is_tip.inputs[1].default_value = 0.99

    tips = mk('GeometryNodeSeparateGeometry', -2000, 300, domain='POINT')
    link(obj_info, 'Geometry', tips, 'Geometry')
    link(is_tip,   'Result',   tips, 'Selection')

    # ── For each leaf vertex: nearest tip index on the filtered mesh ─
    leaf_pos = mk('GeometryNodeInputPosition', -2000, 0)

    nearest = mk('GeometryNodeSampleNearest', -1800, 0, domain='POINT')
    link(tips,     'Selection', nearest, 'Geometry')
    link(leaf_pos, 'Position',  nearest, 'Sample Position')

    # ── Sample tip attributes by that index ──────────────────────────
    def sample_tip_float(attr_name, y):
        attr = mk('GeometryNodeInputNamedAttribute', -1700, y + 100,
                  data_type='FLOAT')
        attr.inputs['Name'].default_value = attr_name
        si = mk('GeometryNodeSampleIndex', -1400, y,
                data_type='FLOAT', domain='POINT')
        link(tips,    'Selection', si, 'Geometry')
        link(attr,    'Attribute', si, 'Value')
        link(nearest, 'Index',     si, 'Index')
        return si

    tip_bx = sample_tip_float('branch_base_x', 600)
    tip_by = sample_tip_float('branch_base_y', 400)
    tip_bz = sample_tip_float('branch_base_z', 200)

    # ── Tree max Z, measured on the trunk reference (not the leaves) ─
    sep_trunk = mk('ShaderNodeSeparateXYZ', -2200, 500)
    trunk_pos = mk('GeometryNodeInputPosition', -2400, 500)
    link(trunk_pos, 'Position', sep_trunk, 'Vector')
    stat = mk('GeometryNodeAttributeStatistic', -1900, 500,
              data_type='FLOAT', domain='POINT')
    link(obj_info,  'Geometry', stat, 'Geometry')
    link(sep_trunk, 'Z',        stat, 'Attribute')

    # ── Compose UVs ──────────────────────────────────────────────────
    # UVMap2 = (tip_branch_base_z, tree_max_z)
    uv2 = mk('ShaderNodeCombineXYZ', -1100, 200)
    link(tip_bz, 'Value', uv2, 'X')
    link(stat,   'Max',   uv2, 'Y')

    # UVMap3 = (0, 1)
    uv3 = mk('ShaderNodeCombineXYZ', -1100, 0)
    uv3.inputs['X'].default_value = 0.0
    uv3.inputs['Y'].default_value = 1.0

    # UVMap1 = (tip_bx, tip_by + 1) — same +1 axis-flip pre-compensation
    # as the wood geonode. Without it, leaves bend around a Y axis that
    # is shifted by 1m, producing the spiral artifact.
    by_plus = mk('ShaderNodeMath', -1300, 400, operation='ADD')
    link(tip_by, 'Value', by_plus, 0)
    by_plus.inputs[1].default_value = 1.0

    lmap = mk('ShaderNodeCombineXYZ', -1100, 400)
    link(tip_bx, 'Value', lmap, 'X')
    link(by_plus, 'Value', lmap, 'Y')

    # ── Color = (0.001, 0.001, curve(random_per_face), 1) ────────────
    # B is a per-face random value in [0, 1], remapped through a
    # user-editable Float Curve so the artist can shape the distribution
    # (e.g. bias most leaves toward low wind with a few outliers).
    # R = G = 0.001 (matching the reference). A = 1.
    #
    # Use GeometryNodeFaceOfCorner.Face Index as the random ID so every
    # corner belonging to the same face gets the same random value —
    # giving a single B per leaf face, not per corner.
    foc = mk('GeometryNodeFaceOfCorner', -1500, -350)

    rng_node = mk('FunctionNodeRandomValue', -1200, -350)
    rng_node.data_type = 'FLOAT'
    rng_node.inputs['Min'].default_value = 0.0
    rng_node.inputs['Max'].default_value = 1.0
    link(foc, 'Face Index',       rng_node, 'ID')
    link(gi,  'Random Blue Seed', rng_node, 'Seed')

    # Bias the uniform random toward low values via a fixed power: most
    # leaves get small B (gentle flutter), a few approach 1 (strong
    # gust). Equivalent to a concave-up curve, no user knobs needed.
    rand_curve = mk('ShaderNodeMath', -900, -350, operation='POWER')
    link(rng_node, 'Value', rand_curve, 0)
    rand_curve.inputs[1].default_value = 2.0

    # Build the color via ShaderNodeMix RGBA: lerp between
    # (0.001, 0.001, 0, 1) and (0.001, 0.001, 1, 1) using the curved
    # random as factor. This keeps Alpha = 1 cleanly (CombineColor has
    # no Alpha input so we can't author A explicitly otherwise).
    def _mix_color_inputs(node):
        return [s for s in node.inputs
                if s.bl_idname == 'NodeSocketColor']

    def _mix_factor_input(node):
        for s in node.inputs:
            if s.name == 'Factor' and s.bl_idname.startswith('NodeSocketFloat'):
                return s
        for s in node.inputs:
            if s.bl_idname.startswith('NodeSocketFloat'):
                return s
        raise RuntimeError(
            f"Could not find Factor socket on Mix node {node.name}")

    def _mix_color_output(node):
        return next(s for s in node.outputs
                    if s.bl_idname == 'NodeSocketColor')

    mix_b = mk('ShaderNodeMix', -600, -300, data_type='RGBA')
    ca, cb = _mix_color_inputs(mix_b)
    ca.default_value = (0.001, 0.001, 0.0, 1.0)
    cb.default_value = (0.001, 0.001, 1.0, 1.0)
    nt.links.new(rand_curve.outputs['Value'], _mix_factor_input(mix_b))

    final_color_socket = _mix_color_output(mix_b)

    # ── Store UVs in TexCoord-INDEX order ───────────────────────────
    # Same FBX-creation-order rule as the wood geonode:
    #   index 1 → UVMap1 (pivot X / Y)
    #   index 2 → UVMap2 (branch_base_z, tree_max_z)
    #   index 3 → UVMap3 ((0, 1) placeholder)
    s_lmap = mk('GeometryNodeStoreNamedAttribute', 0, 300,
                data_type='FLOAT2', domain='CORNER')
    s_lmap.inputs['Name'].default_value = 'UVMap1'
    link(gi,   'Geometry', s_lmap, 'Geometry')
    link(lmap, 'Vector',   s_lmap, 'Value')

    s_uv2 = mk('GeometryNodeStoreNamedAttribute', 300, 200,
               data_type='FLOAT2', domain='CORNER')
    s_uv2.inputs['Name'].default_value = 'UVMap2'
    link(s_lmap, 'Geometry', s_uv2, 'Geometry')
    link(uv2,    'Vector',   s_uv2, 'Value')

    s_uv3 = mk('GeometryNodeStoreNamedAttribute', 600, 100,
               data_type='FLOAT2', domain='CORNER')
    s_uv3.inputs['Name'].default_value = 'UVMap3'
    link(s_uv2, 'Geometry', s_uv3, 'Geometry')
    link(uv3,   'Vector',   s_uv3, 'Value')

    s_col = mk('GeometryNodeStoreNamedAttribute', 900, -100,
               data_type='FLOAT_COLOR', domain='CORNER')
    s_col.inputs['Name'].default_value = 'Attribute'
    link(s_uv3, 'Geometry', s_col, 'Geometry')
    nt.links.new(final_color_socket, s_col.inputs['Value'])

    link(s_col, 'Geometry', go, 'Geometry')
    return nt


# ============================================================================
# Operators
# ============================================================================

class ARANTOOLS_OT_TreeAddUVGeonode(Operator):
    """Build the Tree Branch UV node group if missing, then add it as a
Geometry Nodes modifier on the active mesh. The modifier writes UVMap2,
UVMap3 and a 'Attribute' color attribute every time the depsgraph
re-evaluates — leave it on the stack while iterating."""
    bl_idname  = "arantools.tree_add_uv_geonode"
    bl_label   = "Add Branch UV Geonode"
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
        mod = obj.modifiers.new(name="Tree Branch UV", type='NODES')
        mod.node_group = ng
        self.report({'INFO'},
                    f"Added '{GEONODE_NAME}' modifier to '{obj.name}'.")
        return {'FINISHED'}


class ARANTOOLS_OT_TreeRebuildUVGeonode(Operator):
    """Rebuild the Tree Branch UV node group's contents from the addon's
current definition. Any manual edits to the group are lost. Existing
modifier references keep working — the group's identity is preserved,
only its insides are replaced."""
    bl_idname  = "arantools.tree_rebuild_uv_geonode"
    bl_label   = "Rebuild UV Node Group"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        _ensure_geonode_group(rebuild=True)
        self.report({'INFO'}, f"Rebuilt '{GEONODE_NAME}' from source.")
        return {'FINISHED'}


class ARANTOOLS_OT_TreeAddLeavesUVGeonode(Operator):
    """Build the Tree Leaves UV node group if missing, then add it as a
Geometry Nodes modifier on the active leaves mesh. The modifier samples
the nearest trunk tip for pivot data, then writes UVMap2 / UVMap3 /
UVMap1 / Color appropriately for the leaves shader."""
    bl_idname  = "arantools.tree_add_leaves_uv_geonode"
    bl_label   = "Add Leaves UV Geonode"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        obj = context.active_object
        return obj is not None and obj.type == 'MESH'

    def execute(self, context):
        obj = context.active_object
        ng = _ensure_leaves_geonode_group()
        for m in obj.modifiers:
            if m.type == 'NODES' and m.node_group == ng:
                self.report({'INFO'},
                            f"'{GEONODE_LEAVES_NAME}' modifier already on "
                            f"'{obj.name}'.")
                return {'CANCELLED'}
        mod = obj.modifiers.new(name="Tree Leaves UV", type='NODES')
        mod.node_group = ng
        self.report({'INFO'},
                    f"Added '{GEONODE_LEAVES_NAME}' to '{obj.name}'. "
                    f"Set the 'Trunk' object input on the modifier.")
        return {'FINISHED'}


class ARANTOOLS_OT_TreeRebuildLeavesUVGeonode(Operator):
    """Rebuild the Tree Leaves UV node group from source."""
    bl_idname  = "arantools.tree_rebuild_leaves_uv_geonode"
    bl_label   = "Rebuild Leaves UV Node Group"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        _ensure_leaves_geonode_group(rebuild=True)
        self.report({'INFO'},
                    f"Rebuilt '{GEONODE_LEAVES_NAME}' from source.")
        return {'FINISHED'}


# ── Canonical UV layer order expected by the Unreal shader ──────────
# index 0 = artist UVs, 1 = pivot X/Y, 2 = (base_z, max_z), 3 = (0,1).
_CANONICAL_UV_ORDER = ('UVMap', 'UVMap1', 'UVMap2', 'UVMap3')


def _reorder_uv_layers(mesh, target_order):
    """Force mesh.uv_layers into target_order. Blender's UV API has no
    direct .move(), so we snapshot the per-loop UV data, drop every
    layer, then recreate them in the desired sequence. Layers not in
    target_order are appended at the end in their original encounter
    order so we never lose data."""
    snapshots = {}
    for layer in mesh.uv_layers:
        snapshots[layer.name] = [tuple(d.uv) for d in layer.data]

    active_name = (mesh.uv_layers.active.name
                   if mesh.uv_layers.active else None)
    render_name = next((l.name for l in mesh.uv_layers
                        if getattr(l, 'active_render', False)), None)

    # Sequence: every target name that exists, then any leftovers in
    # their original order.
    seq = [n for n in target_order if n in snapshots]
    seq.extend(n for n in snapshots if n not in seq)

    # Remove all UV layers.
    while mesh.uv_layers:
        mesh.uv_layers.remove(mesh.uv_layers[0])

    # Recreate in order, restoring per-loop values.
    for name in seq:
        new = mesh.uv_layers.new(name=name)
        if new is None:
            continue
        data = snapshots[name]
        for i, uv in enumerate(data):
            if i < len(new.data):
                new.data[i].uv = uv

    # Restore active + active_render where possible.
    if active_name and active_name in mesh.uv_layers:
        mesh.uv_layers[active_name].active = True
    if render_name and render_name in mesh.uv_layers:
        mesh.uv_layers[render_name].active_render = True

    return seq


class ARANTOOLS_OT_TreeFixUVOrder(Operator):
    """Force the selected mesh's UV layers into the canonical order
the SpeedTree-style Unreal shader expects:

    [0] UVMap   (artist texture UVs)
    [1] UVMap1  (per-branch pivot X/Y)
    [2] UVMap2  (branch_base_z, tree_max_z)
    [3] UVMap3  ((0, 1) placeholder)

Run this AFTER Convert-to-Mesh / Apply Modifiers if the layer order
got jumbled. Any UV layers with other names are appended at the end
in their original order so no data is lost."""
    bl_idname  = "arantools.tree_fix_uv_order"
    bl_label   = "Fix UV Layer Order"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return any(o.type == 'MESH' for o in context.selected_objects)

    def execute(self, context):
        targets = [o for o in context.selected_objects if o.type == 'MESH']
        if not targets:
            self.report({'ERROR'}, "Select at least one mesh.")
            return {'CANCELLED'}
        reordered = []
        for obj in targets:
            if not obj.data.uv_layers:
                continue
            seq = _reorder_uv_layers(obj.data, _CANONICAL_UV_ORDER)
            reordered.append((obj.name, seq))
        if not reordered:
            self.report({'WARNING'}, "No mesh had any UV layers to reorder.")
            return {'CANCELLED'}
        first_seq = reordered[0][1]
        self.report({'INFO'},
                    f"Reordered {len(reordered)} mesh(es). "
                    f"New order: {' → '.join(first_seq)}")
        return {'FINISHED'}


# ============================================================================
# Registration
# ============================================================================

classes = [
    ARANTOOLS_OT_TreeAddUVGeonode,
    ARANTOOLS_OT_TreeRebuildUVGeonode,
    ARANTOOLS_OT_TreeAddLeavesUVGeonode,
    ARANTOOLS_OT_TreeRebuildLeavesUVGeonode,
    ARANTOOLS_OT_TreeFixUVOrder,
]


def register():
    for cls in classes:
        bpy.utils.register_class(cls)


def unregister():
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
