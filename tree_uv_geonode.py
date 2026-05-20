"""
Tree Branch UV Geonode â€” bakes the SpeedTree-style identification UVs
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
    isn't read from there â€” it's likely AO / stiffness / unused.
    Leaving A = 1 to match the reference's typical case.

So this geonode writes (all FACE_CORNER domain):
  - UVMap2    (Float2): (branch_base_z, tree_max_z)
  - UVMap3    (Float2): (0, 1)  â€” placeholder constant
  - UVMap1 (Float2): (branch_base_x, branch_base_y + 1)
        Repurposed: NOT an actual lightmap. The wood shader
        reconstructs the world-space pivot via
        `(UVMap1.R, 1 - UVMap1.G, UVMap2.R) * -1 ?` â€”
        we pre-bake the "+ 1" so the shader's "1 - V" undo recovers
        the original Y in mesh-local meters.
  - Attribute (Color): (R, 0, 0, 1)
        R = 0.0 for trunk (depth 0), 0.1 for branches (depth â‰¥ 1).
        The wood shader uses these to pick the wind branch tier.

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
        # Wipe the interface too â€” recreated below
        for item in list(existing.interface.items_tree):
            try:
                existing.interface.remove(item)
            except Exception:
                pass
        nt = existing
    else:
        nt = bpy.data.node_groups.new(GEONODE_NAME, 'GeometryNodeTree')

    # â”€â”€ Interface (inputs / outputs of the group) â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    nt.interface.new_socket(
        "Geometry", in_out='INPUT', socket_type='NodeSocketGeometry'
    )
    # Wind amplitude / height-falloff inputs intentionally removed â€” the
    # wood shader doesn't read per-vertex wind from color (the reference
    # asset's wood color is all zero on RGB; alpha holds a depth tier).
    nt.interface.new_socket(
        "Geometry", in_out='OUTPUT', socket_type='NodeSocketGeometry'
    )

    # â”€â”€ Local helpers â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    def mk(typ, x, y, **kw):
        node = nt.nodes.new(typ)
        node.location = (x, y)
        for k, v in kw.items():
            setattr(node, k, v)
        return node

    def link(out_node, out_id, in_node, in_id):
        nt.links.new(out_node.outputs[out_id], in_node.inputs[in_id])

    # â”€â”€ Group I/O â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    gi = mk('NodeGroupInput',  -1800, 0)
    go = mk('NodeGroupOutput',  1800, 0)

    # â”€â”€ Tree max Z: needed for UVMap2.V (the per-tree max-Z pivot) â”€â”€
    pos = mk('GeometryNodeInputPosition', -1600, -300)
    sep = mk('ShaderNodeSeparateXYZ',     -1400, -300)
    link(pos, 'Position', sep, 'Vector')

    stat = mk('GeometryNodeAttributeStatistic', -1200, -300,
              data_type='FLOAT', domain='POINT')
    link(gi,  'Geometry', stat, 'Geometry')
    link(sep, 'Z',        stat, 'Attribute')

    # â”€â”€ Per-branch pivot: read branch_base_z directly â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    base_attr = mk('GeometryNodeInputNamedAttribute', -1000, 300,
                   data_type='FLOAT')
    base_attr.inputs['Name'].default_value = 'branch_base_z'

    # â”€â”€ Compose UVMap2 = (branch_base_z, tree_max_z, 0) â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    uv2_combine = mk('ShaderNodeCombineXYZ', -600, 300)
    link(base_attr, 'Attribute', uv2_combine, 'X')
    link(stat,      'Max',       uv2_combine, 'Y')

    # â”€â”€ Compose UVMap3 = (0, 1, 0) â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    uv3_combine = mk('ShaderNodeCombineXYZ', -600, 100)
    uv3_combine.inputs['X'].default_value = 0.0
    uv3_combine.inputs['Y'].default_value = 1.0
    uv3_combine.inputs['Z'].default_value = 0.0

    # â”€â”€ Color.R = trunk(0.0) vs branch(0.1) â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    # Per user spec: trunk (depth 0) gets R = 0.0, any branch (depth â‰¥ 1)
    # gets R = 0.1. Implementation: clamp branch_depth to [0, 1] (so
    # depth 0â†’0, depth â‰¥1 â†’ 1), then multiply by 0.1.
    depth_attr = mk('GeometryNodeInputNamedAttribute', -1000, -100,
                    data_type='INT')
    depth_attr.inputs['Name'].default_value = 'branch_depth'

    depth_clamp = mk('ShaderNodeClamp', -700, -100, clamp_type='MINMAX')
    depth_clamp.inputs['Min'].default_value = 0.0
    depth_clamp.inputs['Max'].default_value = 1.0
    link(depth_attr, 'Attribute', depth_clamp, 'Value')

    depth_tier = mk('ShaderNodeMath', -500, -100, operation='MULTIPLY')
    link(depth_clamp, 'Result', depth_tier, 0)
    depth_tier.inputs[1].default_value = 0.1

    # â”€â”€ Compose Color = (depth_tier, 0, 0, 1) â€” wind tier in R â”€â”€â”€â”€â”€â”€â”€
    # MF_VertexColorID.R * 255 = wind branch tier ID (Unreal shader).
    # Trunk=0.0 â†’ ID 0, depth1=0.333 â†’ ID 85, depth2=0.666 â†’ 170,
    # depthâ‰¥3=1.0 â†’ 255.
    color_rgb = mk('FunctionNodeCombineColor', -200, -100, mode='RGB')
    color_rgb.inputs['Green'].default_value = 0.0
    color_rgb.inputs['Blue'].default_value  = 0.0
    link(depth_tier, 'Value', color_rgb, 'Red')

    # â”€â”€ Per-branch pivot X / Y â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    bx_attr = mk('GeometryNodeInputNamedAttribute', -1000, 600,
                 data_type='FLOAT')
    bx_attr.inputs['Name'].default_value = 'branch_base_x'

    by_attr = mk('GeometryNodeInputNamedAttribute', -1000, 450,
                 data_type='FLOAT')
    by_attr.inputs['Name'].default_value = 'branch_base_y'

    # The shader undoes "Fix Axis" via (1 - V) * -1 = V - 1, so we pre-
    # bake V = branch_base_y + 1. That way the shader recovers the
    # original Y.
    by_plus_one = mk('ShaderNodeMath', -800, 450, operation='ADD')
    link(by_attr, 'Attribute', by_plus_one, 0)
    by_plus_one.inputs[1].default_value = 1.0

    # â”€â”€ Compose UVMap1 = (branch_base_x, branch_base_y + 1) â”€â”€â”€â”€â”€â”€
    lmap_combine = mk('ShaderNodeCombineXYZ', -600, 500)
    link(bx_attr,      'Attribute', lmap_combine, 'X')
    link(by_plus_one,  'Value',     lmap_combine, 'Y')

    # â”€â”€ Store UVMap2 â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    s_uv2 = mk('GeometryNodeStoreNamedAttribute', 0, 400,
               data_type='FLOAT2', domain='CORNER')
    s_uv2.inputs['Name'].default_value = 'UVMap2'
    link(gi,          'Geometry', s_uv2, 'Geometry')
    link(uv2_combine, 'Vector',   s_uv2, 'Value')

    # â”€â”€ Store UVMap3 â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    s_uv3 = mk('GeometryNodeStoreNamedAttribute', 400, 200,
               data_type='FLOAT2', domain='CORNER')
    s_uv3.inputs['Name'].default_value = 'UVMap3'
    link(s_uv2,       'Geometry', s_uv3, 'Geometry')
    link(uv3_combine, 'Vector',   s_uv3, 'Value')

    # â”€â”€ Store UVMap1 (repurposed as per-branch pivot X / Y) â”€â”€â”€â”€â”€
    s_lmap = mk('GeometryNodeStoreNamedAttribute', 800, 400,
                data_type='FLOAT2', domain='CORNER')
    s_lmap.inputs['Name'].default_value = 'UVMap1'
    link(s_uv3,        'Geometry', s_lmap, 'Geometry')
    link(lmap_combine, 'Vector',   s_lmap, 'Value')

    # â”€â”€ Store color "Attribute" = (depth_tier, 0, 0, 1) â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    s_col = mk('GeometryNodeStoreNamedAttribute', 1200, -100,
               data_type='FLOAT_COLOR', domain='CORNER')
    s_col.inputs['Name'].default_value = 'Attribute'
    link(s_lmap,    'Geometry', s_col, 'Geometry')
    link(color_rgb, 'Color',    s_col, 'Value')

    # â”€â”€ Output â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    link(s_col, 'Geometry', go, 'Geometry')

    return nt


# ============================================================================
# Tree Leaves UV Geonode â€” separate encoding for leaf meshes
# ============================================================================
#
# Each leaf vertex looks up the CLOSEST trunk tip (a tube vertex with
# branch_t â‰ˆ 1.0) and inherits that tip's pivot data. So a leaf swaying
# in the wind uses its parent branch's tip as the sway origin â€” which is
# what the shader's pivot reconstruction expects.
#
# Writes (all FACE_CORNER on the leaves mesh):
#   - UVMap2    : (tip.branch_base_z, tree_max_z)
#   - UVMap3    : (0, 1)
#   - UVMap1 : (tip.branch_base_x, tip.branch_base_y + 1)
#   - Attribute  : (0.001, 0.001, wind_amp, random_per_face)
#         where wind_amp is a height-based falloff from the leaf's own
#         vertex Z, and A is a 0-1 noise value indexed by face.

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

    # â”€â”€ Interface â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    nt.interface.new_socket(
        "Geometry", in_out='INPUT', socket_type='NodeSocketGeometry'
    )
    trunk_in = nt.interface.new_socket(
        "Trunk", in_out='INPUT', socket_type='NodeSocketObject'
    )
    trunk_in.description = (
        "Reference trunk mesh (post tubes geonode). The leaf vertices "
        "sample the closest tip on this object for their pivot data")
    falloff = nt.interface.new_socket(
        "Wind Falloff Power", in_out='INPUT', socket_type='NodeSocketFloat'
    )
    falloff.default_value = 1.5
    falloff.min_value = 0.1
    falloff.max_value = 5.0
    falloff.description = "Exponent on (leaf_z / tree_max_z) for Color.B"
    rng = nt.interface.new_socket(
        "Random Alpha Seed", in_out='INPUT', socket_type='NodeSocketInt'
    )
    rng.default_value = 0
    rng.description = "Seed for the per-face random alpha"
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

    # â”€â”€ Pull trunk geometry, filter to tip verts (branch_t â‰¥ 0.99) â”€â”€
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

    # â”€â”€ For each leaf vertex: nearest tip index on the filtered mesh â”€
    leaf_pos = mk('GeometryNodeInputPosition', -2000, 0)

    nearest = mk('GeometryNodeSampleNearest', -1800, 0, domain='POINT')
    link(tips,     'Selection', nearest, 'Geometry')
    link(leaf_pos, 'Position',  nearest, 'Sample Position')

    # â”€â”€ Sample tip attributes by that index â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
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

    # â”€â”€ Tree max Z, measured on the trunk reference (not the leaves) â”€
    sep_trunk = mk('ShaderNodeSeparateXYZ', -2200, 500)
    trunk_pos = mk('GeometryNodeInputPosition', -2400, 500)
    link(trunk_pos, 'Position', sep_trunk, 'Vector')
    stat = mk('GeometryNodeAttributeStatistic', -1900, 500,
              data_type='FLOAT', domain='POINT')
    link(obj_info,  'Geometry', stat, 'Geometry')
    link(sep_trunk, 'Z',        stat, 'Attribute')

    # â”€â”€ Compose UVs â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    # UVMap2 = (tip_branch_base_z, tree_max_z)
    uv2 = mk('ShaderNodeCombineXYZ', -1100, 200)
    link(tip_bz, 'Value', uv2, 'X')
    link(stat,   'Max',   uv2, 'Y')

    # UVMap3 = (0, 1)
    uv3 = mk('ShaderNodeCombineXYZ', -1100, 0)
    uv3.inputs['X'].default_value = 0.0
    uv3.inputs['Y'].default_value = 1.0

    # UVMap1 = (tip_bx, tip_by + 1)
    by_plus = mk('ShaderNodeMath', -1300, 500, operation='ADD')
    link(tip_by, 'Value', by_plus, 0)
    by_plus.inputs[1].default_value = 1.0
    lmap = mk('ShaderNodeCombineXYZ', -1100, 400)
    link(tip_bx,  'Value', lmap, 'X')
    link(by_plus, 'Value', lmap, 'Y')

    # â”€â”€ Color = (0.001, 0.001, wind_amp, random_per_face) â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    # wind_amp from the leaf vertex Z normalized to [0, tree_max_z],
    # then raised to falloff power. (Range using Min as well to be
    # consistent with the wood-side approach.)
    sep_leaf = mk('ShaderNodeSeparateXYZ', -2000, -300)
    link(leaf_pos, 'Position', sep_leaf, 'Vector')

    z_above = mk('ShaderNodeMath', -1700, -300, operation='SUBTRACT')
    link(sep_leaf, 'Z',   z_above, 0)
    link(stat,     'Min', z_above, 1)

    z_range = mk('ShaderNodeMath', -1700, -500, operation='SUBTRACT')
    link(stat, 'Max', z_range, 0)
    link(stat, 'Min', z_range, 1)
    safe_range = mk('ShaderNodeMath', -1500, -500, operation='MAXIMUM')
    link(z_range, 'Value', safe_range, 0)
    safe_range.inputs[1].default_value = 1e-4

    h_ratio = mk('ShaderNodeMath', -1300, -300, operation='DIVIDE')
    h_ratio.use_clamp = True
    link(z_above,    'Value', h_ratio, 0)
    link(safe_range, 'Value', h_ratio, 1)

    pow_h = mk('ShaderNodeMath', -1100, -300, operation='POWER')
    link(h_ratio, 'Value',                pow_h, 0)
    link(gi,      'Wind Falloff Power',   pow_h, 1)

    # Random alpha per face: hash on Index (FACE domain).
    rng_node = mk('FunctionNodeRandomValue', -900, -550)
    rng_node.data_type = 'FLOAT'
    rng_node.inputs['Min'].default_value = 0.0
    rng_node.inputs['Max'].default_value = 1.0
    face_idx = mk('GeometryNodeInputID', -1100, -550)
    link(face_idx, 'ID',                 rng_node, 'ID')
    link(gi,       'Random Alpha Seed',  rng_node, 'Seed')

    color = mk('FunctionNodeCombineColor', -700, -300, mode='RGB')
    color.inputs['Red'].default_value   = 0.001
    color.inputs['Green'].default_value = 0.001
    link(pow_h, 'Value', color, 'Blue')

    # â”€â”€ Store all the attrs in sequence â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    s_uv2 = mk('GeometryNodeStoreNamedAttribute', 0, 200,
               data_type='FLOAT2', domain='CORNER')
    s_uv2.inputs['Name'].default_value = 'UVMap2'
    link(gi,  'Geometry', s_uv2, 'Geometry')
    link(uv2, 'Vector',   s_uv2, 'Value')

    s_uv3 = mk('GeometryNodeStoreNamedAttribute', 300, 100,
               data_type='FLOAT2', domain='CORNER')
    s_uv3.inputs['Name'].default_value = 'UVMap3'
    link(s_uv2, 'Geometry', s_uv3, 'Geometry')
    link(uv3,   'Vector',   s_uv3, 'Value')

    s_lmap = mk('GeometryNodeStoreNamedAttribute', 600, 300,
                data_type='FLOAT2', domain='CORNER')
    s_lmap.inputs['Name'].default_value = 'UVMap1'
    link(s_uv3, 'Geometry', s_lmap, 'Geometry')
    link(lmap,  'Vector',   s_lmap, 'Value')

    s_col = mk('GeometryNodeStoreNamedAttribute', 900, -100,
               data_type='FLOAT_COLOR', domain='CORNER')
    s_col.inputs['Name'].default_value = 'Attribute'
    link(s_lmap, 'Geometry', s_col, 'Geometry')
    link(color,  'Color',    s_col, 'Value')

    # Random alpha gets stored as a separate FLOAT_COLOR write on the
    # alpha channel â€” but since CombineColor has no Alpha input, we use
    # a follow-up Store on the FLOAT 'leaf_alpha' so the artist (or a
    # shader-side patch) can read it as the per-face alpha.
    s_alpha = mk('GeometryNodeStoreNamedAttribute', 1500, -500,
                 data_type='FLOAT', domain='FACE')
    s_alpha.inputs['Name'].default_value = 'leaf_alpha'
    link(s_col,    'Geometry', s_alpha, 'Geometry')
    link(rng_node, 'Value',    s_alpha, 'Value')

    link(s_alpha, 'Geometry', go, 'Geometry')
    return nt


# ============================================================================
# Operators
# ============================================================================

class ARANTOOLS_OT_TreeAddUVGeonode(Operator):
    """Build the Tree Branch UV node group if missing, then add it as a
Geometry Nodes modifier on the active mesh. The modifier writes UVMap2,
UVMap3 and a 'Attribute' color attribute every time the depsgraph
re-evaluates â€” leave it on the stack while iterating."""
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
modifier references keep working â€” the group's identity is preserved,
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


# ============================================================================
# Registration
# ============================================================================

classes = [
    ARANTOOLS_OT_TreeAddUVGeonode,
    ARANTOOLS_OT_TreeRebuildUVGeonode,
    ARANTOOLS_OT_TreeAddLeavesUVGeonode,
    ARANTOOLS_OT_TreeRebuildLeavesUVGeonode,
]


def register():
    for cls in classes:
        bpy.utils.register_class(cls)


def unregister():
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
