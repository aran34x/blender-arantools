import bpy
from bpy.types import Operator

# ============================================================================
# Operators
# ============================================================================

class ARANTOOLS_OT_Rig_Feathers(Operator):
    """Auto-rig feathers with intelligent vertex selection"""
    bl_idname = "arantools.rig_feathers"
    bl_label = "Rig Feathers"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return context.mode == 'OBJECT'

    def execute(self, context):
        # Get mesh and armature from selection
        mesh = None
        armature = None
        for obj in context.selected_objects:
            if obj.type == 'MESH':
                mesh = obj
            elif obj.type == 'ARMATURE':
                armature = obj

        if not mesh or not armature:
            self.report({'ERROR'}, "Select both mesh and armature")
            return {'FINISHED'}

        mesh_data = mesh.data
        armature_data = armature.data

        # Clear existing vertex groups
        if mesh.vertex_groups:
            context.view_layer.objects.active = mesh
            bpy.ops.object.vertex_group_remove(all=True)

        # Get selected bones
        context.view_layer.objects.active = armature
        bpy.ops.object.mode_set(mode='EDIT')
        selected_bones = [bone.name for bone in context.selected_bones]
        bpy.ops.object.mode_set(mode='OBJECT')

        if not selected_bones:
            self.report({'ERROR'}, "Select bones in armature")
            return {'FINISHED'}

        # Create vertex groups for selected bones
        for bone_name in selected_bones:
            if bone_name not in mesh.vertex_groups:
                mesh.vertex_groups.new(name=bone_name)

        # Identify sharp edges
        sharp_edges = {edge.index: edge for edge in mesh_data.edges if edge.use_edge_sharp}

        if not sharp_edges:
            # Auto-select everything
            for vertex in mesh_data.vertices:
                vertex.select = True
        else:
            # Process each sharp edge island
            for sharp_edge_index, sharp_edge in sharp_edges.items():
                island_vertices = self._get_island_vertices(mesh_data, sharp_edge)

                # Find closest bone
                edge_center = (mesh_data.vertices[sharp_edge.vertices[0]].co +
                              mesh_data.vertices[sharp_edge.vertices[1]].co) / 2
                closest_bone = self._find_closest_bone(armature, selected_bones, edge_center)

                if closest_bone:
                    for vertex_idx in island_vertices:
                        mesh_data.vertices[vertex_idx].select = True

        # Bind to rig
        mesh.select_set(True)
        context.view_layer.objects.active = armature
        bpy.ops.arp.bind_to_rig()

        return {'FINISHED'}

    def _get_island_vertices(self, mesh_data, start_edge):
        """Get all vertices in an island using DFS from a sharp edge"""
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
        """Find the closest bone to a point"""
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


class ARANTOOLS_OT_select_deform_bones(Operator):
    """Select all bones with deform enabled"""
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


class ARANTOOLS_OT_mirror_bones(Operator):
    """Mirror bones from left to right (requires .L / .R naming)"""
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

        # Select left bones
        for bone in armature.data.bones:
            if bone.name.endswith('.L'):
                bone.select = True

        bpy.ops.object.mode_set(mode='EDIT')
        bpy.ops.armature.symmetrize(direction='NEGATIVE_X')
        bpy.ops.object.mode_set(mode='OBJECT')

        self.report({'INFO'}, "Mirrored bones from left to right")
        return {'FINISHED'}


class ARANTOOLS_OT_select_bone_type(Operator):
    """Toggle selection of bones by type (Deform/Control/Mech)"""
    bl_idname = "arantools.select_bone_type"
    bl_label = "Select By Type"
    bl_options = {'REGISTER', 'UNDO'}

    bone_type: bpy.props.EnumProperty(
        name="Type",
        items=[
            ('DEFORM', "Deform", "Deform bones"),
            ('CONTROL', "Control", "Control bones"),
            ('MECH', "Mech", "Mechanical bones"),
        ]
    )

    def execute(self, context):
        armature = context.object
        if not armature or armature.type != 'ARMATURE':
            self.report({'ERROR'}, "No armature selected")
            return {'CANCELLED'}

        bpy.ops.object.mode_set(mode='OBJECT')
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
# Registration
# ============================================================================

classes = [
    ARANTOOLS_OT_Rig_Feathers,
    ARANTOOLS_OT_select_deform_bones,
    ARANTOOLS_OT_mirror_bones,
    ARANTOOLS_OT_select_bone_type,
]


def register():
    for cls in classes:
        bpy.utils.register_class(cls)


def unregister():
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
