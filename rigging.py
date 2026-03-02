import bpy
from bpy.types import Operator


# ============================================================================
# Selection Tools
# ============================================================================

class ARANTOOLS_OT_select_deform_bones(Operator):
    """Select all bones with Deform enabled"""
    bl_idname = "arantools.select_deform_bones"
    bl_label = "Select Deform Bones"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        armature = context.object
        if not armature or armature.type != 'ARMATURE':
            self.report({'ERROR'}, "No armature selected")
            return {'CANCELLED'}

        bpy.ops.object.mode_set(mode='EDIT')
        bpy.ops.armature.select_all(action='DESELECT')
        bpy.ops.object.mode_set(mode='OBJECT')

        for bone in armature.data.bones:
            if bone.use_deform:
                bone.select = True

        return {'FINISHED'}


class ARANTOOLS_OT_select_bone_type(Operator):
    """Select bones by naming convention (CTRL_ / MCH_) or deform flag"""
    bl_idname = "arantools.select_bone_type"
    bl_label = "Select By Type"
    bl_options = {'REGISTER', 'UNDO'}

    bone_type: bpy.props.EnumProperty(
        name="Type",
        items=[
            ('DEFORM', "Deform", "Deform bones"),
            ('CONTROL', "Control", "Bones prefixed CTRL_"),
            ('MECH', "Mech", "Bones prefixed MCH_"),
        ]
    )

    def execute(self, context):
        armature = context.object
        if not armature or armature.type != 'ARMATURE':
            self.report({'ERROR'}, "No armature selected")
            return {'CANCELLED'}

        bpy.ops.object.mode_set(mode='EDIT')
        bpy.ops.armature.select_all(action='DESELECT')
        bpy.ops.object.mode_set(mode='OBJECT')

        for bone in armature.data.bones:
            if self.bone_type == 'DEFORM' and bone.use_deform:
                bone.select = True
            elif self.bone_type == 'CONTROL' and bone.name.startswith('CTRL_'):
                bone.select = True
            elif self.bone_type == 'MECH' and bone.name.startswith('MCH_'):
                bone.select = True

        return {'FINISHED'}


# ============================================================================
# Mirror Bones
# ============================================================================

class ARANTOOLS_OT_mirror_bones(Operator):
    """Mirror .L bones to .R using Blender's Symmetrize"""
    bl_idname = "arantools.mirror_bones"
    bl_label = "Mirror Bones L→R"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        armature = context.object
        if not armature or armature.type != 'ARMATURE':
            self.report({'ERROR'}, "No armature selected")
            return {'CANCELLED'}

        bpy.ops.object.mode_set(mode='EDIT')
        bpy.ops.armature.select_all(action='DESELECT')
        bpy.ops.object.mode_set(mode='OBJECT')

        for bone in armature.data.bones:
            if bone.name.endswith('.L'):
                bone.select = True

        bpy.ops.object.mode_set(mode='EDIT')
        bpy.ops.armature.symmetrize(direction='NEGATIVE_X')
        bpy.ops.object.mode_set(mode='OBJECT')

        self.report({'INFO'}, "Mirrored .L bones to .R")
        return {'FINISHED'}


# ============================================================================
# Feather Rigger
# ============================================================================

class ARANTOOLS_OT_Rig_Feathers(Operator):
    """Auto-rig feathers/hair by detecting sharp-edge islands and binding via ARP"""
    bl_idname = "arantools.rig_feathers"
    bl_label = "Rig Feathers"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return context.mode == 'OBJECT'

    def execute(self, context):
        mesh = None
        armature = None
        for obj in context.selected_objects:
            if obj.type == 'MESH':
                mesh = obj
            elif obj.type == 'ARMATURE':
                armature = obj

        if not mesh or not armature:
            self.report({'ERROR'}, "Select both a mesh and an armature")
            return {'FINISHED'}

        mesh_data = mesh.data

        if mesh.vertex_groups:
            context.view_layer.objects.active = mesh
            bpy.ops.object.vertex_group_remove(all=True)

        context.view_layer.objects.active = armature
        bpy.ops.object.mode_set(mode='EDIT')
        selected_bones = [bone.name for bone in context.selected_bones]
        bpy.ops.object.mode_set(mode='OBJECT')

        if not selected_bones:
            self.report({'ERROR'}, "Select bones in the armature first")
            return {'FINISHED'}

        for bone_name in selected_bones:
            if bone_name not in mesh.vertex_groups:
                mesh.vertex_groups.new(name=bone_name)

        sharp_edges = {edge.index: edge for edge in mesh_data.edges if edge.use_edge_sharp}

        if not sharp_edges:
            for vertex in mesh_data.vertices:
                vertex.select = True
        else:
            for sharp_edge in sharp_edges.values():
                island_vertices = self._get_island_vertices(mesh_data, sharp_edge)
                edge_center = (
                    mesh_data.vertices[sharp_edge.vertices[0]].co +
                    mesh_data.vertices[sharp_edge.vertices[1]].co
                ) / 2
                closest_bone = self._find_closest_bone(armature, selected_bones, edge_center)
                if closest_bone:
                    for vertex_idx in island_vertices:
                        mesh_data.vertices[vertex_idx].select = True

        mesh.select_set(True)
        context.view_layer.objects.active = armature
        bpy.ops.arp.bind_to_rig()

        return {'FINISHED'}

    def _get_island_vertices(self, mesh_data, start_edge):
        island_vertices = []
        visited = set()

        def dfs(vertex_index):
            visited.add(vertex_index)
            island_vertices.append(vertex_index)
            for edge in mesh_data.edges:
                if vertex_index in edge.vertices:
                    adjacent = edge.vertices[0] if edge.vertices[1] == vertex_index else edge.vertices[1]
                    if adjacent not in visited:
                        dfs(adjacent)

        dfs(start_edge.vertices[0])
        return island_vertices

    def _find_closest_bone(self, armature, bone_names, point):
        closest_bone = None
        closest_distance = float('inf')
        for bone_name in bone_names:
            bone = armature.pose.bones.get(bone_name)
            if bone:
                distance = (bone.tail - point).length
                if distance < closest_distance:
                    closest_distance = distance
                    closest_bone = bone_name
        return closest_bone


# ============================================================================
# Advanced Rigging (Join/Bind workflow + ARP tools)
# ============================================================================

class ARANTOOLS_PG_AdvRigging(bpy.types.PropertyGroup):
    source_mesh: bpy.props.PointerProperty(
        name="Source Mesh",
        type=bpy.types.Object,
        description="Rigged character body to copy the armature and weights from",
        poll=lambda self, obj: obj.type == 'MESH'
    )
    target_collection: bpy.props.PointerProperty(
        name="Target Collection",
        type=bpy.types.Collection,
        description="Collection to place the joined result in"
    )
    mapping_method: bpy.props.EnumProperty(
        name="Mapping Method",
        items=[
            ('NEAREST', 'Nearest Vertex', 'Copy from the nearest vertex'),
            ('POLYINTERP_NEAREST', 'Nearest Face Interpolated', 'Interpolate from nearest face'),
            ('POLYINTERP_LNORPROJ', 'Projected Face Interpolated', 'Interpolate from projected face'),
        ],
        default='POLYINTERP_NEAREST'
    )
    chain_length: bpy.props.IntProperty(
        name="Chain Length",
        description="Number of parent bones to include in the ARP binding chain",
        default=3, min=0
    )
    sharp_edge_pointer_method: bpy.props.EnumProperty(
        name="Pointer Method",
        items=[
            ('SHARP_EDGE', 'Sharp Edge Center', 'Use the center of the first sharp edge on an island'),
            ('UV_Y_MAX', 'Highest UV-Y Vertex', 'Use the vertex with the highest UV Y-value'),
        ],
        default='SHARP_EDGE'
    )


class ARANTOOLS_OT_JoinTargets(Operator):
    """Duplicate and join selected objects into a single convertied mesh"""
    bl_idname = "arantools.join_targets"
    bl_label = "Join Selected Targets"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return context.selected_objects and any(
            obj.type in {'MESH', 'CURVE', 'SURFACE', 'FONT', 'META'}
            for obj in context.selected_objects
        )

    def execute(self, context):
        if context.active_object and context.active_object.mode != 'OBJECT':
            bpy.ops.object.mode_set(mode='OBJECT')

        selected = [obj for obj in context.selected_objects
                    if obj.type in {'MESH', 'CURVE', 'SURFACE', 'FONT', 'META'}]
        if not selected:
            self.report({'ERROR'}, "No convertible objects selected.")
            return {'CANCELLED'}

        originals = selected[:]
        bpy.ops.object.duplicate(linked=False)
        for obj in originals:
            obj.hide_set(True)

        duplicated = context.selected_objects
        if not duplicated:
            self.report({'ERROR'}, "Duplication failed.")
            return {'CANCELLED'}

        context.view_layer.objects.active = duplicated[0]
        try:
            bpy.ops.object.convert(target='MESH')
        except RuntimeError as e:
            self.report({'ERROR'}, f"Conversion failed: {e}")
            bpy.ops.object.delete()
            return {'CANCELLED'}

        bpy.ops.object.join()
        joined = context.active_object
        joined.name = "Joined_Target"

        props = context.scene.arantools_adv_rigging
        if props.target_collection:
            tc = props.target_collection
            if joined.name not in tc.objects:
                tc.objects.link(joined)
            for col in [c for c in joined.users_collection if c != tc]:
                col.objects.unlink(joined)

        self.report({'INFO'}, f"Created '{joined.name}'")
        return {'FINISHED'}


class ARANTOOLS_OT_BindAndTransfer(Operator):
    """Parent the active mesh to the source's armature and transfer weights"""
    bl_idname = "arantools.bind_and_transfer"
    bl_label = "Bind and Transfer Weights"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        props = context.scene.arantools_adv_rigging
        return (props.source_mesh and context.active_object
                and context.active_object.type == 'MESH')

    def find_armature(self, obj):
        if obj.parent and obj.parent.type == 'ARMATURE':
            return obj.parent
        for mod in obj.modifiers:
            if mod.type == 'ARMATURE' and mod.object:
                return mod.object
        return None

    def execute(self, context):
        props = context.scene.arantools_adv_rigging
        source = props.source_mesh
        target = context.active_object

        if source == target:
            self.report({'ERROR'}, "Source and Target cannot be the same object.")
            return {'CANCELLED'}

        armature = self.find_armature(source)
        if not armature:
            self.report({'ERROR'}, "Source mesh has no associated armature.")
            return {'CANCELLED'}

        is_posing = armature.data.pose_position == 'POSE'
        if is_posing:
            armature.data.pose_position = 'REST'

        bpy.ops.object.select_all(action='DESELECT')
        target.select_set(True)
        armature.select_set(True)
        context.view_layer.objects.active = armature
        bpy.ops.object.parent_set(type='ARMATURE_AUTO')

        bpy.ops.object.select_all(action='DESELECT')
        target.select_set(True)
        context.view_layer.objects.active = target

        mod = target.modifiers.new(name="WeightDataTransfer", type='DATA_TRANSFER')
        mod.object = source
        mod.use_vert_data = True
        mod.data_types_verts = {'VGROUP_WEIGHTS'}
        mod.vert_mapping = props.mapping_method
        bpy.ops.object.modifier_apply(modifier=mod.name)

        if is_posing:
            armature.data.pose_position = 'POSE'

        self.report({'INFO'}, f"Weights transferred from '{source.name}' to '{target.name}'")
        return {'FINISHED'}


class ARANTOOLS_OT_WeightFromPointer(Operator):
    """For each island, find the nearest bone tip and bind a chain using ARP"""
    bl_idname = "arantools.weight_from_pointer"
    bl_label = "Weight from Pointer"
    bl_options = {'REGISTER', 'UNDO'}

    def get_islands(self, me):
        adjacency = [[] for _ in me.vertices]
        for e in me.edges:
            v1, v2 = e.vertices
            adjacency[v1].append(v2)
            adjacency[v2].append(v1)
        visited = set()
        islands = []
        for v_idx in range(len(me.vertices)):
            if v_idx not in visited:
                stack, island = [v_idx], set()
                visited.add(v_idx)
                while stack:
                    cur = stack.pop()
                    island.add(cur)
                    for neigh in adjacency[cur]:
                        if neigh not in visited:
                            visited.add(neigh)
                            stack.append(neigh)
                islands.append(list(island))
        return islands

    def execute(self, context):
        props = context.scene.arantools_adv_rigging
        mesh_obj = armature_obj = None

        initial_selection = context.selected_objects[:]
        active_object = context.view_layer.objects.active

        for obj in initial_selection:
            if obj.type == 'MESH':
                mesh_obj = obj
            elif obj.type == 'ARMATURE':
                armature_obj = obj

        if not mesh_obj or not armature_obj:
            self.report({'ERROR'}, "Select one Mesh and one Armature.")
            return {'CANCELLED'}

        if not hasattr(context.scene, 'arp_bind_selected_bones'):
            self.report({'ERROR'}, "Auto-Rig Pro not found. Is it enabled?")
            return {'CANCELLED'}

        if context.active_object and context.active_object.mode != 'OBJECT':
            bpy.ops.object.mode_set(mode='OBJECT')

        mesh_data = mesh_obj.data
        context.view_layer.objects.active = armature_obj
        bpy.ops.object.mode_set(mode='EDIT')
        candidate_tips = {bone.name for bone in context.selected_bones}
        bpy.ops.object.mode_set(mode='OBJECT')

        if not candidate_tips:
            self.report({'ERROR'}, "Select tip bones in Armature Edit Mode first.")
            return {'CANCELLED'}

        islands = self.get_islands(mesh_data)
        if not islands:
            self.report({'WARNING'}, "No vertex islands found.")
            return {'CANCELLED'}

        if props.sharp_edge_pointer_method == 'UV_Y_MAX' and not mesh_data.uv_layers.active:
            self.report({'ERROR'}, "Mesh has no active UV layer.")
            return {'CANCELLED'}

        if mesh_obj.vertex_groups:
            context.view_layer.objects.active = mesh_obj
            bpy.ops.object.vertex_group_remove(all=True)

        processed = 0
        for island in islands:
            pointer_world = None

            if props.sharp_edge_pointer_method == 'SHARP_EDGE':
                island_set = set(island)
                for edge in mesh_data.edges:
                    if edge.use_edge_sharp and (edge.vertices[0] in island_set or edge.vertices[1] in island_set):
                        v1 = mesh_data.vertices[edge.vertices[0]].co
                        v2 = mesh_data.vertices[edge.vertices[1]].co
                        pointer_world = mesh_obj.matrix_world @ ((v1 + v2) / 2.0)
                        break

            elif props.sharp_edge_pointer_method == 'UV_Y_MAX':
                uv_data = mesh_data.uv_layers.active.data
                vert_uvs = {loop.vertex_index: uv_data[loop.index].uv for loop in reversed(mesh_data.loops)}
                candidates = {v for v in island if v in vert_uvs}
                if candidates:
                    best = max(candidates, key=lambda v: vert_uvs[v].y)
                    pointer_world = mesh_obj.matrix_world @ mesh_data.vertices[best].co

            if pointer_world is None:
                continue

            best_tip = None
            min_dist = float('inf')
            for bone_name in candidate_tips:
                rest_bone = armature_obj.data.bones.get(bone_name)
                if rest_bone:
                    tip_world = armature_obj.matrix_world @ rest_bone.tail_local
                    dist = (tip_world - pointer_world).length
                    if dist < min_dist:
                        min_dist = dist
                        best_tip = bone_name

            if not best_tip:
                continue

            context.view_layer.objects.active = armature_obj
            bpy.ops.object.mode_set(mode='EDIT')
            bpy.ops.armature.select_all(action='DESELECT')
            current_bone = armature_obj.data.edit_bones.get(best_tip)
            if current_bone:
                armature_obj.data.edit_bones.active = current_bone
                for _ in range(props.chain_length):
                    if current_bone:
                        current_bone.select = True
                        current_bone = current_bone.parent
                    else:
                        break
            bpy.ops.object.mode_set(mode='OBJECT')

            context.view_layer.objects.active = mesh_obj
            bpy.ops.object.mode_set(mode='EDIT')
            bpy.ops.mesh.select_all(action='DESELECT')
            bpy.ops.object.mode_set(mode='OBJECT')
            for v_idx in island:
                mesh_data.vertices[v_idx].select = True

            bpy.ops.object.select_all(action='DESELECT')
            mesh_obj.select_set(True)
            armature_obj.select_set(True)
            context.view_layer.objects.active = armature_obj
            try:
                bpy.ops.arp.bind_to_rig()
            except AttributeError:
                self.report({'ERROR'}, "Auto-Rig Pro is not enabled.")
                return {'CANCELLED'}

            processed += 1

        bpy.ops.object.mode_set(mode='OBJECT')
        bpy.ops.object.select_all(action='DESELECT')
        for obj in initial_selection:
            obj.select_set(True)
        context.view_layer.objects.active = active_object

        self.report({'INFO'}, f"Processed {processed} island(s).")
        return {'FINISHED'}


class ARANTOOLS_OT_DirectArpBind(Operator):
    """Bind the current vertex/bone selection directly using Auto-Rig Pro"""
    bl_idname = "arantools.direct_arp_bind"
    bl_label = "Direct ARP Bind"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        sel = context.selected_objects
        return any(o.type == 'MESH' for o in sel) and any(o.type == 'ARMATURE' for o in sel)

    def execute(self, context):
        mesh_obj = armature_obj = None
        active = context.active_object

        if active and active.type == 'ARMATURE':
            armature_obj = active
            mesh_obj = next((o for o in context.selected_objects if o.type == 'MESH'), None)
        elif active and active.type == 'MESH':
            mesh_obj = active
            armature_obj = next((o for o in context.selected_objects if o.type == 'ARMATURE'), None)
        else:
            mesh_obj = next((o for o in context.selected_objects if o.type == 'MESH'), None)
            armature_obj = next((o for o in context.selected_objects if o.type == 'ARMATURE'), None)

        if not mesh_obj or not armature_obj:
            self.report({'ERROR'}, "Select one Mesh and one Armature.")
            return {'CANCELLED'}

        if not hasattr(context.scene, 'arp_bind_selected_bones'):
            self.report({'ERROR'}, "Auto-Rig Pro not found. Is it enabled?")
            return {'CANCELLED'}

        if context.mode != 'OBJECT':
            bpy.ops.object.mode_set(mode='OBJECT')

        context.view_layer.objects.active = armature_obj
        bpy.ops.object.mode_set(mode='EDIT')
        selected_bone_count = len(context.selected_bones)
        bpy.ops.object.mode_set(mode='OBJECT')

        if selected_bone_count == 0:
            self.report({'ERROR'}, "No bones selected in Armature Edit Mode.")
            return {'CANCELLED'}

        bpy.ops.object.select_all(action='DESELECT')
        mesh_obj.select_set(True)
        armature_obj.select_set(True)
        context.view_layer.objects.active = armature_obj

        try:
            bpy.ops.arp.bind_to_rig()
            self.report({'INFO'}, "Direct bind successful.")
        except AttributeError:
            self.report({'ERROR'}, "Auto-Rig Pro is not enabled.")
            return {'CANCELLED'}
        except Exception as e:
            self.report({'ERROR'}, f"Bind error: {e}")
            return {'CANCELLED'}

        context.view_layer.objects.active = mesh_obj
        return {'FINISHED'}


# ============================================================================
# Registration
# ============================================================================

classes = [
    ARANTOOLS_OT_select_deform_bones,
    ARANTOOLS_OT_select_bone_type,
    ARANTOOLS_OT_mirror_bones,
    ARANTOOLS_OT_Rig_Feathers,
    ARANTOOLS_PG_AdvRigging,
    ARANTOOLS_OT_JoinTargets,
    ARANTOOLS_OT_BindAndTransfer,
    ARANTOOLS_OT_WeightFromPointer,
    ARANTOOLS_OT_DirectArpBind,
]


def register():
    for cls in classes:
        bpy.utils.register_class(cls)
    bpy.types.Scene.arantools_adv_rigging = bpy.props.PointerProperty(type=ARANTOOLS_PG_AdvRigging)


def unregister():
    del bpy.types.Scene.arantools_adv_rigging
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
