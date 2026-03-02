# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful, but
# WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTIBILITY or FITNESS FOR A PARTICULAR PURPOSE. See the GNU
# General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program. If not, see <http://www.gnu.org/licenses/>.

bl_info = {
    "name" : "Noise Add to Bone",
    "author" : "Aran", 
    "description" : "Adds Noise using FCurve Modifiers",
    "blender" : (3, 0, 0),
    "version" : (1, 3, 0),
    "location" : "",
    "warning" : "",
    "doc_url": "", 
    "tracker_url": "", 
    "category" : "Animation" 
}


import bpy
import bpy.utils.previews


addon_keymaps = {}
_icons = None
class SNA_PT_NOISE_24FDB(bpy.types.Panel):
    bl_label = 'Noise'
    bl_idname = 'SNA_PT_NOISE_24FDB'
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_context = ''
    bl_category = 'Animation'
    bl_order = 0
    bl_ui_units_x=0

    @classmethod
    def poll(cls, context):
        return not (False)

    def draw_header(self, context):
        layout = self.layout

    def draw(self, context):
        layout = self.layout
        col_0D289 = layout.column(heading='Noise Control', align=False)
        col_0D289.alert = False
        col_0D289.enabled = True
        col_0D289.active = True
        col_0D289.use_property_split = False
        col_0D289.use_property_decorate = False
        col_0D289.scale_x = 1.0
        col_0D289.scale_y = 1.4919999837875366
        col_0D289.alignment = 'Expand'.upper()
        col_0D289.operator_context = "INVOKE_DEFAULT" if True else "EXEC_DEFAULT"
        col_0D289.prop(bpy.context.scene, 'sna_rotation_scale', text='Rotation Speed Scale', icon_value=0, emboss=True, slider=True)
        col_0D289.prop(bpy.context.scene, 'sna_rotation_strength', text='Rotation Strength', icon_value=0, emboss=True, slider=True)
        col_0D289.prop(bpy.context.scene, 'sna_location_scale', text='Location Speed Scale', icon_value=0, emboss=True, slider=True)
        col_0D289.prop(bpy.context.scene, 'sna_location_strenght', text='Location Strength', icon_value=0, emboss=True, slider=True)
        col_0D289.prop(bpy.context.scene, 'sna_advanced_options', text='Advanced Options', icon_value=0, emboss=True, slider=False)
        if bpy.context.scene.sna_advanced_options:
            col_177D1 = col_0D289.column(heading='Axis Controls', align=False)
            col_177D1.alert = False
            col_177D1.enabled = True
            col_177D1.active = True
            col_177D1.use_property_split = False
            col_177D1.use_property_decorate = False
            col_177D1.scale_x = 1.0
            col_177D1.scale_y = 0.6520000100135803
            col_177D1.alignment = 'Expand'.upper()
            col_177D1.operator_context = "INVOKE_DEFAULT" if True else "EXEC_DEFAULT"
            col_177D1.prop(bpy.context.scene, 'sna_location_axis_multipliers', text='Location Stength Multipliers', icon_value=0, emboss=True, slider=True)
            col_177D1.prop(bpy.context.scene, 'sna_location_axis_multiplier_speed', text='Location Speed  Muiltipliers', icon_value=0, emboss=True, slider=True)
            col_177D1.prop(bpy.context.scene, 'sna_rotation_axis_multipliers', text='Rotation Strength Multipliers', icon_value=0, emboss=True, slider=True)
            col_177D1.prop(bpy.context.scene, 'sna_rotation_axis_multiplier_speed', text='Rotation Speed Multipliers', icon_value=0, emboss=True, slider=True)
        col_DCC3C = col_0D289.column(heading='Duration', align=False)
        col_DCC3C.alert = False
        col_DCC3C.enabled = True
        col_DCC3C.active = True
        col_DCC3C.use_property_split = False
        col_DCC3C.use_property_decorate = False
        col_DCC3C.scale_x = 1.0
        col_DCC3C.scale_y = 1.0
        col_DCC3C.alignment = 'Expand'.upper()
        col_DCC3C.operator_context = "INVOKE_DEFAULT" if True else "EXEC_DEFAULT"
        col_DCC3C.prop(bpy.context.scene, 'sna_frame_length', text='Last Frame', icon_value=0, emboss=True)
        col_DCC3C.prop(bpy.context.scene, 'sna_blend_duration', text='Blend Duration', icon_value=0, emboss=True)
        col_6AF10 = col_0D289.column(heading='Finalize', align=False)
        col_6AF10.alert = False
        col_6AF10.enabled = True
        col_6AF10.active = True
        col_6AF10.use_property_split = False
        col_6AF10.use_property_decorate = False
        col_6AF10.scale_x = 1.0
        col_6AF10.scale_y = 1.0
        col_6AF10.alignment = 'Expand'.upper()
        col_6AF10.operator_context = "INVOKE_DEFAULT" if True else "EXEC_DEFAULT"
        op = col_6AF10.operator('sna.apply_noise_rotation_ad6b9', text='Apply Noise Rotation', icon_value=487, emboss=True, depress=False)
        op = col_6AF10.operator('sna.apply_noise_location_64994', text='Apply Noise Location', icon_value=487, emboss=True, depress=False)


class SNA_OT_Apply_Noise_Rotation_Ad6B9(bpy.types.Operator):
    bl_idname = "sna.apply_noise_rotation_ad6b9"
    bl_label = "Apply Noise Rotation"
    bl_description = ""
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        if bpy.app.version >= (3, 0, 0) and True:
            cls.poll_message_set('')
        return not False

    def execute(self, context):
        strength = bpy.context.scene.sna_rotation_strength
        scale = bpy.context.scene.sna_rotation_scale
        length = bpy.context.scene.sna_frame_length
        blend = bpy.context.scene.sna_blend_duration
        axis_vector = bpy.context.scene.sna_rotation_axis_multipliers
        speed_vector = bpy.context.scene.sna_rotation_axis_multiplier_speed
        import random
        # Function to set up the F-Curve modifier

        def setup_fcurve_modifier(fcurve, modifier_name, axis, speed):
            # Check if the modifier is already present
            fmodifier = next((modifier for modifier in fcurve.modifiers if modifier.name == modifier_name), None)
            # Add or overwrite "Aran_Noise" F-Curve modifier
            if fmodifier is None:
                fmodifier = fcurve.modifiers.new(type='NOISE')
                fmodifier.name = modifier_name
            else:
                print ("Modifier Found")
            # Set up basic parameters for the modifier
            fmodifier.strength = strength * axis  # Adjust as needed
            fmodifier.scale = scale / speed  # Adjust as needed
            fmodifier.phase = random.uniform(0, 1000)  # Random offset
            fmodifier.frame_start = 0
            fmodifier.frame_end = length
            fmodifier.use_restricted_range = True
            fmodifier.blend_in = blend
            fmodifier.blend_out = blend
            return fmodifier
        # Check if in Pose Mode
        if bpy.context.mode != 'POSE':
            print("Switch to Pose Mode and run the script again.")
        else:
            # Get the active armature
            armature = bpy.context.active_object
            # Check if an armature is selected
            if armature and armature.type == 'ARMATURE':
                # Iterate through selected pose bones
                for bone in bpy.context.selected_pose_bones:
                    print("Pose Bone:", bone.name)
                    # Iterate through rotation channels (X, Y, Z)
                    for channel in ['rotation_euler', 'rotation_quaternion']:
                        for axis in range(3):
                            # Get the f-curve for the current bone and axis
                            fcurve = armature.animation_data.action.fcurves.find(
                                f'pose.bones["{bone.name}"].{channel}', index=axis)
                            if fcurve is not None:
                                # Set up the "Aran_Noise" F-Curve modifier
                                fmodifier = setup_fcurve_modifier(fcurve, "Aran_Noise", axis_vector[axis], speed_vector[axis])
                                # Print the types of all F-Curve Modifiers
                                print("F-Curve Modifier Type:", fmodifier.type)
            else:
                print("Select an armature in Pose Mode before running the script.")
        return {"FINISHED"}

    def invoke(self, context, event):
        return self.execute(context)


class SNA_OT_Apply_Noise_Location_64994(bpy.types.Operator):
    bl_idname = "sna.apply_noise_location_64994"
    bl_label = "Apply Noise Location"
    bl_description = ""
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        if bpy.app.version >= (3, 0, 0) and True:
            cls.poll_message_set('')
        return not False

    def execute(self, context):
        strength = bpy.context.scene.sna_location_strenght
        scale = bpy.context.scene.sna_location_scale
        length = bpy.context.scene.sna_frame_length
        blend = bpy.context.scene.sna_blend_duration
        axis_vector = bpy.context.scene.sna_location_axis_multipliers
        speed_vector = bpy.context.scene.sna_location_axis_multiplier_speed
        import random
        # Function to set up the F-Curve modifier

        def setup_fcurve_modifier(fcurve, modifier_name, axis, speed):
            # Check if the modifier is already present
            fmodifier = next((modifier for modifier in fcurve.modifiers if modifier.name == modifier_name), None)
            # Add or overwrite "Aran_Noise" F-Curve modifier
            if fmodifier is None:
                fmodifier = fcurve.modifiers.new(type='NOISE')
                fmodifier.name = modifier_name
            else:
                print ("Modifier Found")
            # Set up basic parameters for the modifier
            fmodifier.strength = strength * axis  # Adjust as needed
            fmodifier.scale = scale / speed  # Adjust as needed
            fmodifier.phase = random.uniform(0, 1000)  # Random offset
            fmodifier.frame_start = 0
            fmodifier.frame_end = length
            fmodifier.use_restricted_range = True
            fmodifier.blend_in = blend
            fmodifier.blend_out = blend
            return fmodifier
        # Check if in Pose Mode
        if bpy.context.mode != 'POSE':
            print("Switch to Pose Mode and run the script again.")
        else:
            # Get the active armature
            armature = bpy.context.active_object
            # Check if an armature is selected
            if armature and armature.type == 'ARMATURE':
                # Iterate through selected pose bones
                for bone in bpy.context.selected_pose_bones:
                    print("Pose Bone:", bone.name)
                    # Iterate through rotation channels (X, Y, Z)
                    for channel in ['location']:
                        for axis in range(3):
                            # Get the f-curve for the current bone and axis
                            fcurve = armature.animation_data.action.fcurves.find(
                                f'pose.bones["{bone.name}"].{channel}', index=axis)
                            if fcurve is not None:
                                # Set up the "Aran_Noise" F-Curve modifier
                                fmodifier = setup_fcurve_modifier(fcurve, "Aran_Noise", axis_vector[axis], speed_vector[axis])
                                # Print the types of all F-Curve Modifiers
                                print("F-Curve Modifier Type:", fmodifier.type)
            else:
                print("Select an armature in Pose Mode before running the script.")
        return {"FINISHED"}

    def invoke(self, context, event):
        return self.execute(context)


class SNA_PT_ARAN_TOOLS_F6EF3(bpy.types.Panel):
    bl_label = 'Aran Tools'
    bl_idname = 'SNA_PT_ARAN_TOOLS_F6EF3'
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_context = ''
    bl_category = 'ARP'
    bl_order = 0
    bl_ui_units_x=0

    @classmethod
    def poll(cls, context):
        return not (False)

    def draw_header(self, context):
        layout = self.layout

    def draw(self, context):
        layout = self.layout
        col_89C72 = layout.column(heading='Feather Rigger', align=False)
        col_89C72.alert = False
        col_89C72.enabled = True
        col_89C72.active = True
        col_89C72.use_property_split = False
        col_89C72.use_property_decorate = False
        col_89C72.scale_x = 1.0
        col_89C72.scale_y = 1.437999963760376
        col_89C72.alignment = 'Center'.upper()
        col_89C72.operator_context = "INVOKE_DEFAULT" if True else "EXEC_DEFAULT"
        col_89C72.prop(None, 'None', text='Chain Length', icon_value=0, emboss=True)
        op = col_89C72.operator('sna.rig_feathers_5c6e7', text='Rig Feathers', icon_value=618, emboss=True, depress=False)
        op = col_89C72.operator('object.select_pattern', text='Select Wing Bones', icon_value=177, emboss=True, depress=False)
        op.pattern = '*_feather_[01]*'
        op.case_sensitive = True


class SNA_OT_Rig_Feathers_5C6E7(bpy.types.Operator):
    bl_idname = "sna.rig_feathers_5c6e7"
    bl_label = "Rig Feathers"
    bl_description = ""
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        if bpy.app.version >= (3, 0, 0) and True:
            cls.poll_message_set('')
        return not False

    def execute(self, context):
        chainlength = 0
        import math
        # ------- Get Variables from selection
        # Initialize variables for the mesh and armature
        mesh = None
        armature = None
        # Iterate through the selected objects
        for obj in bpy.context.selected_objects:
            if obj.type == 'MESH':
                mesh = obj
            elif obj.type == 'ARMATURE':
                armature = obj
        # Print the selected mesh and armature
        if mesh:
            print(f"Selected Mesh: {mesh.name}")
        else:
            print("No mesh selected.")
        if armature:
            print(f"Selected Armature: {armature.name}")
        else:
            print("No armature selected.")
        # Access the mesh data
        mesh_data = mesh.data
        armature_data = armature.data
        if mesh.vertex_groups:
            bpy.context.view_layer.objects.active = mesh
            bpy.ops.object.vertex_group_remove(all=True)
        # ------- Get The Selected Bones Vertex Weights
        bpy.context.view_layer.objects.active = armature
         # Switch to Edit Mode
        bpy.ops.object.mode_set(mode='EDIT')
            # Get a list of selected bones' names
        selected_bones = [bone.name for bone in bpy.context.selected_bones]
        if len(selected_bones) == 0:
            self.report({"ERROR"}, "Select Bones in Armature Edit Mode")
            # Switch back to Object Mode
        bpy.ops.object.mode_set(mode='OBJECT')
            # Print the selected bones' names
        print("Selected Bones in Edit Mode:")
        for bone_name in selected_bones:
            print(bone_name)
        # Create an empty vertex group in the mesh for each selected bone
        for bone_name in selected_bones:
            if bone_name not in mesh.vertex_groups:
                vertex_group = mesh.vertex_groups.new(name=bone_name)
        #------------- Identify sharp edges        
        bpy.context.view_layer.objects.active = mesh
        # Create a dictionary to store sharp edges
        sharp_edges = {}
        bpy.ops.object.mode_set(mode='OBJECT')
        # Loop through the edges
        for edge in mesh_data.edges:
            if edge.use_edge_sharp:
                # Store the edge's ID in the dictionary
                sharp_edges[edge.index] = edge.index
        # Print the dictionary of sharp edges
        print("Sharp Edges:")
        for edge_index in sharp_edges.values():
            print(f"Edge {edge_index}")
        # Iterate through all sharp edges
        for sharp_edge_index in sharp_edges.values():
            # Get the sharp edge
            sharp_edge = mesh_data.edges[sharp_edge_index]
            # Get the vertices of the sharp edge
            vertex1 = mesh_data.vertices[sharp_edge.vertices[0]]
            vertex2 = mesh_data.vertices[sharp_edge.vertices[1]]
            # Calculate the center of the sharp edge
            edge_center = (vertex1.co + vertex2.co) / 2
            print(f"Center of Sharp Edge {sharp_edge_index}: {edge_center}")
            # ---------- Save the Island Vertices
             # Create a list to store the vertices in the island
            island_vertices = []
                # --------DOES DFS TO POPULATE THAT ARRAY
                # Create a set to keep track of visited vertices
            visited_vertices = set()
                # Define a DFS function to traverse the island

            def dfs(vertex_index):
                visited_vertices.add(vertex_index)
                island_vertices.append(vertex_index)
                for edge in mesh_data.edges:
                    if vertex_index in edge.vertices:
                        adjacent_vertex_index = edge.vertices[0] if edge.vertices[1] == vertex_index else edge.vertices[1]
                        if adjacent_vertex_index not in visited_vertices:
                            dfs(adjacent_vertex_index)
            # Start the DFS traversal from the specified vertex
            dfs(sharp_edge.vertices[0])
            # Print the list of vertices in the island
            print(f"Vertices in the Island: {len(island_vertices)}")
            print("--")
            # ---------------  Identify the starting bone
            # Initialize variables to keep track of the closest bone and its distance
            closest_bone_name = None
            closest_distance = float('inf')  # Initialize with positive infinity
            # Iterate through all the bones in the selected bones list
            for bone_name in selected_bones:
                # Get the bone by name
                bone = armature.pose.bones.get(bone_name)
                if bone:
                    # Calculate the distance between the bone's tail and the center of the sharp edge
                    distance = (bone.tail - edge_center).length
                    # Check if this bone is closer than the current closest bone
                    if distance < closest_distance:
                        closest_distance = distance
                        closest_bone_name = bone_name
                else:
                    print(f"Bone {bone_name} not found in armature.")
            # ----- select the armature
            bpy.context.view_layer.objects.active = armature
            bpy.ops.object.mode_set(mode='EDIT')
            bpy.ops.armature.select_all(action='DESELECT')
            new_bone = armature_data.edit_bones[closest_bone_name]
            armature_data.edit_bones.active = new_bone
            bpy.ops.armature.select_hierarchy(direction='PARENT', extend=False)
            bpy.ops.armature.select_hierarchy(direction='CHILD', extend=False)
            i=0
            # select the bones in the chain
            while i<chainlength:
                bpy.ops.armature.select_hierarchy(direction='PARENT', extend=True)
                i=i+1
            bpy.ops.object.mode_set(mode='EDIT')
            bpy.ops.object.mode_set(mode='OBJECT')
             # ----- select the mesh
            bpy.context.view_layer.objects.active = mesh
            bpy.ops.object.mode_set(mode='EDIT')
            bpy.ops.mesh.select_all(action='SELECT')
            bpy.ops.mesh.select_all(action='DESELECT')
            bpy.ops.object.mode_set(mode='OBJECT')
            for vertex_index in island_vertices:
                mesh_data.vertices[vertex_index].select = True
                print(f"Selected Vertex: {vertex_index}")
            bpy.ops.object.mode_set(mode='EDIT')
            bpy.ops.object.mode_set(mode='OBJECT')
            mesh.select_set(True)
            bpy.context.view_layer.objects.active = armature
            bpy.ops.arp.bind_to_rig()
        return {"FINISHED"}

    def invoke(self, context, event):
        return self.execute(context)


class SNA_OT_Select_Wings_A6Cbb(bpy.types.Operator):
    bl_idname = "sna.select_wings_a6cbb"
    bl_label = "Select Wings"
    bl_description = ""
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        if bpy.app.version >= (3, 0, 0) and True:
            cls.poll_message_set('')
        return not False

    def execute(self, context):
        return {"FINISHED"}

    def invoke(self, context, event):
        return self.execute(context)


def register():
    global _icons
    _icons = bpy.utils.previews.new()
    bpy.types.Scene.sna_rotation_strength = bpy.props.FloatProperty(name='Rotation Strength', description='', default=0.20000000298023224, subtype='NONE', unit='NONE', min=0.0, soft_max=1.0, step=3, precision=2)
    bpy.types.Scene.sna_rotation_scale = bpy.props.FloatProperty(name='Rotation Scale', description='', default=5.0, subtype='NONE', unit='NONE', soft_min=1.0, soft_max=40.0, step=3, precision=2)
    bpy.types.Scene.sna_location_strenght = bpy.props.FloatProperty(name='Location Strenght', description='', default=30.0, subtype='NONE', unit='NONE', min=0.0, soft_max=100.0, step=3, precision=2)
    bpy.types.Scene.sna_location_scale = bpy.props.FloatProperty(name='Location Scale', description='', default=5.0, subtype='NONE', unit='NONE', soft_min=0.0, soft_max=40.0, step=3, precision=2)
    bpy.types.Scene.sna_frame_length = bpy.props.IntProperty(name='Frame Length', description='', default=200, subtype='NONE', min=0, max=1000)
    bpy.types.Scene.sna_blend_duration = bpy.props.IntProperty(name='Blend Duration', description='', default=10, subtype='NONE')
    bpy.types.Scene.sna_location_axis_multipliers = bpy.props.FloatVectorProperty(name='Location Axis Multipliers', description='', size=3, default=(1.0, 1.0, 1.0), subtype='NONE', unit='NONE', soft_min=0.0, soft_max=1.0, step=3, precision=2)
    bpy.types.Scene.sna_location_axis_multiplier_speed = bpy.props.FloatVectorProperty(name='Location Axis Multiplier Speed', description='', size=3, default=(1.0, 1.0, 1.0), subtype='NONE', unit='NONE', soft_min=0.0, soft_max=1.0, step=3, precision=2)
    bpy.types.Scene.sna_rotation_axis_multipliers = bpy.props.FloatVectorProperty(name='Rotation Axis Multipliers', description='', size=3, default=(1.0, 1.0, 1.0), subtype='NONE', unit='NONE', soft_min=0.0, soft_max=1.0, step=3, precision=2)
    bpy.types.Scene.sna_rotation_axis_multiplier_speed = bpy.props.FloatVectorProperty(name='Rotation Axis Multiplier Speed', description='', size=3, default=(1.0, 1.0, 1.0), subtype='NONE', unit='NONE', soft_min=0.0, soft_max=1.0, step=3, precision=2)
    bpy.types.Scene.sna_advanced_options = bpy.props.BoolProperty(name='Advanced Options', description='', default=False)
    bpy.utils.register_class(SNA_PT_NOISE_24FDB)
    bpy.utils.register_class(SNA_OT_Apply_Noise_Rotation_Ad6B9)
    bpy.utils.register_class(SNA_OT_Apply_Noise_Location_64994)
    bpy.utils.register_class(SNA_PT_ARAN_TOOLS_F6EF3)
    bpy.utils.register_class(SNA_OT_Rig_Feathers_5C6E7)
    bpy.utils.register_class(SNA_OT_Select_Wings_A6Cbb)


def unregister():
    global _icons
    bpy.utils.previews.remove(_icons)
    wm = bpy.context.window_manager
    kc = wm.keyconfigs.addon
    for km, kmi in addon_keymaps.values():
        km.keymap_items.remove(kmi)
    addon_keymaps.clear()
    del bpy.types.Scene.sna_advanced_options
    del bpy.types.Scene.sna_rotation_axis_multiplier_speed
    del bpy.types.Scene.sna_rotation_axis_multipliers
    del bpy.types.Scene.sna_location_axis_multiplier_speed
    del bpy.types.Scene.sna_location_axis_multipliers
    del bpy.types.Scene.sna_blend_duration
    del bpy.types.Scene.sna_frame_length
    del bpy.types.Scene.sna_location_scale
    del bpy.types.Scene.sna_location_strenght
    del bpy.types.Scene.sna_rotation_scale
    del bpy.types.Scene.sna_rotation_strength
    bpy.utils.unregister_class(SNA_PT_NOISE_24FDB)
    bpy.utils.unregister_class(SNA_OT_Apply_Noise_Rotation_Ad6B9)
    bpy.utils.unregister_class(SNA_OT_Apply_Noise_Location_64994)
    bpy.utils.unregister_class(SNA_PT_ARAN_TOOLS_F6EF3)
    bpy.utils.unregister_class(SNA_OT_Rig_Feathers_5C6E7)
    bpy.utils.unregister_class(SNA_OT_Select_Wings_A6Cbb)
