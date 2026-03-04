import bpy
from collections import defaultdict
from bpy.types import Operator


# ============================================================================
# Helpers
# ============================================================================

def _deform_group_indices(obj):
    """Return a set of vertex-group indices that correspond to deform bones."""
    deform_names = set()
    for mod in obj.modifiers:
        if mod.type == 'ARMATURE' and mod.object:
            for bone in mod.object.data.bones:
                if bone.use_deform:
                    deform_names.add(bone.name)

    indices = set()
    for vg in obj.vertex_groups:
        if vg.name in deform_names:
            indices.add(vg.index)
    return indices


def _find_mesh_islands(mesh):
    """Return a list of frozensets of vertex indices, one per connected island."""
    adjacency = defaultdict(set)
    for edge in mesh.edges:
        a, b = edge.vertices
        adjacency[a].add(b)
        adjacency[b].add(a)

    visited = set()
    islands = []
    for v in mesh.vertices:
        vi = v.index
        if vi in visited:
            continue
        island = set()
        stack = [vi]
        while stack:
            cur = stack.pop()
            if cur in visited:
                continue
            visited.add(cur)
            island.add(cur)
            stack.extend(adjacency[cur] - visited)
        islands.append(frozenset(island))
    return islands


# ============================================================================
# Property group
# ============================================================================

class ARANTOOLS_PG_IslandFlatten(bpy.types.PropertyGroup):
    blend: bpy.props.FloatProperty(
        name="Blend",
        description="0 = keep original weights, 1 = fully averaged per island",
        default=1.0,
        min=0.0,
        max=1.0,
        subtype='FACTOR',
    )
    only_selected: bpy.props.BoolProperty(
        name="Selected Vertices Only",
        description="Only process vertices that are selected in the mesh",
        default=False,
    )


# ============================================================================
# Operator
# ============================================================================

class ARANTOOLS_OT_IslandFlatten(Operator):
    """For each mesh island, average all deform-bone weights across every vertex
so the island bends as a rigid unit. Use Blend to apply partially."""
    bl_idname = "arantools.island_flatten_weights"
    bl_label = "Flatten Island Weights"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        obj = context.active_object
        props = context.scene.arantools_island_flatten

        if not obj or obj.type != 'MESH':
            self.report({'ERROR'}, "Active object must be a mesh.")
            return {'CANCELLED'}

        deform_indices = _deform_group_indices(obj)
        if not deform_indices:
            self.report({'WARNING'},
                        "No deform-bone vertex groups found. "
                        "Make sure the mesh has an Armature modifier.")
            return {'CANCELLED'}

        # Must be in Object mode to read/write weights reliably
        prev_mode = obj.mode
        if prev_mode != 'OBJECT':
            bpy.ops.object.mode_set(mode='OBJECT')

        mesh = obj.data
        blend = props.blend
        only_selected = props.only_selected

        islands = _find_mesh_islands(mesh)

        # Build selection mask once (only used when only_selected is True)
        if only_selected:
            selected = frozenset(v.index for v in mesh.vertices if v.select)

        for vg in obj.vertex_groups:
            if vg.index not in deform_indices:
                continue

            vg_idx = vg.index

            # Collect weights for every vertex in one pass
            weight_map = {}
            for v in mesh.vertices:
                vi = v.index
                if only_selected and vi not in selected:
                    continue
                w = 0.0
                for grp in v.groups:
                    if grp.group == vg_idx:
                        w = grp.weight
                        break
                weight_map[vi] = w

            for island in islands:
                # Vertices to process in this island
                verts = island & weight_map.keys()
                if not verts:
                    continue

                weights = [weight_map[vi] for vi in verts]
                avg = sum(weights) / len(weights)

                for vi in verts:
                    orig = weight_map[vi]
                    new_w = orig + (avg - orig) * blend
                    if new_w > 1e-6:
                        vg.add([vi], new_w, 'REPLACE')
                    else:
                        vg.remove([vi])

        if prev_mode != 'OBJECT':
            bpy.ops.object.mode_set(mode=prev_mode)

        self.report({'INFO'},
                    f"Flattened deform weights across {len(islands)} island(s).")
        return {'FINISHED'}


# ============================================================================
# Registration
# ============================================================================

classes = [
    ARANTOOLS_PG_IslandFlatten,
    ARANTOOLS_OT_IslandFlatten,
]


def register():
    for cls in classes:
        bpy.utils.register_class(cls)
    bpy.types.Scene.arantools_island_flatten = bpy.props.PointerProperty(
        type=ARANTOOLS_PG_IslandFlatten
    )


def unregister():
    del bpy.types.Scene.arantools_island_flatten
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
