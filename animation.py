import bpy
import random
from bpy.types import Operator, Panel


# ============================================================================
# Operators
# ============================================================================

class ARANTOOLS_OT_Apply_Noise_Rotation(Operator):
    """Apply noise to bone rotation F-curves"""
    bl_idname = "arantools.apply_noise_rotation"
    bl_label = "Apply Noise Rotation"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return context.mode == 'POSE'

    def execute(self, context):
        scene = context.scene
        armature = context.active_object

        if not armature or armature.type != 'ARMATURE':
            self.report({'ERROR'}, "Select armature in Pose Mode")
            return {'FINISHED'}

        for bone in context.selected_pose_bones:
            for channel in ['rotation_euler', 'rotation_quaternion']:
                for axis in range(3):
                    fcurve = armature.animation_data.action.fcurves.find(
                        f'pose.bones["{bone.name}"].{channel}', index=axis)
                    if fcurve is not None:
                        self._setup_noise_modifier(
                            fcurve, axis,
                            scene.arantools_rotation_strength,
                            scene.arantools_rotation_scale,
                            scene.arantools_frame_length,
                            scene.arantools_blend_duration,
                            scene.arantools_rotation_axis_multipliers,
                            scene.arantools_rotation_axis_multiplier_speed
                        )

        return {'FINISHED'}

    def _setup_noise_modifier(self, fcurve, axis, strength, scale, length, blend, axis_mult, speed_mult):
        """Add or update noise modifier on F-curve"""
        modifier = next((m for m in fcurve.modifiers if m.name == "Aran_Noise"), None)
        if modifier is None:
            modifier = fcurve.modifiers.new(type='NOISE')
            modifier.name = "Aran_Noise"

        modifier.strength = strength * axis_mult[axis]
        modifier.scale = scale / speed_mult[axis]
        modifier.phase = random.uniform(0, 1000)
        modifier.frame_start = 0
        modifier.frame_end = length
        modifier.use_restricted_range = True
        modifier.blend_in = blend
        modifier.blend_out = blend


class ARANTOOLS_OT_Apply_Noise_Location(Operator):
    """Apply noise to bone location F-curves"""
    bl_idname = "arantools.apply_noise_location"
    bl_label = "Apply Noise Location"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return context.mode == 'POSE'

    def execute(self, context):
        scene = context.scene
        armature = context.active_object

        if not armature or armature.type != 'ARMATURE':
            self.report({'ERROR'}, "Select armature in Pose Mode")
            return {'FINISHED'}

        for bone in context.selected_pose_bones:
            for axis in range(3):
                fcurve = armature.animation_data.action.fcurves.find(
                    f'pose.bones["{bone.name}"].location', index=axis)
                if fcurve is not None:
                    self._setup_noise_modifier(
                        fcurve, axis,
                        scene.arantools_location_strenght,
                        scene.arantools_location_scale,
                        scene.arantools_frame_length,
                        scene.arantools_blend_duration,
                        scene.arantools_location_axis_multipliers,
                        scene.arantools_location_axis_multiplier_speed
                    )

        return {'FINISHED'}

    def _setup_noise_modifier(self, fcurve, axis, strength, scale, length, blend, axis_mult, speed_mult):
        """Add or update noise modifier on F-curve"""
        modifier = next((m for m in fcurve.modifiers if m.name == "Aran_Noise"), None)
        if modifier is None:
            modifier = fcurve.modifiers.new(type='NOISE')
            modifier.name = "Aran_Noise"

        modifier.strength = strength * axis_mult[axis]
        modifier.scale = scale / speed_mult[axis]
        modifier.phase = random.uniform(0, 1000)
        modifier.frame_start = 0
        modifier.frame_end = length
        modifier.use_restricted_range = True
        modifier.blend_in = blend
        modifier.blend_out = blend


# ============================================================================
# Registration
# ============================================================================

classes = [
    ARANTOOLS_OT_Apply_Noise_Rotation,
    ARANTOOLS_OT_Apply_Noise_Location,
]


def register():
    for cls in classes:
        bpy.utils.register_class(cls)

    # Scene properties
    bpy.types.Scene.arantools_rotation_strength = bpy.props.FloatProperty(
        name='Rotation Strength', default=0.2, min=0.0, soft_max=1.0)
    bpy.types.Scene.arantools_rotation_scale = bpy.props.FloatProperty(
        name='Rotation Scale', default=5.0, soft_min=1.0, soft_max=40.0)
    bpy.types.Scene.arantools_location_strenght = bpy.props.FloatProperty(
        name='Location Strength', default=30.0, min=0.0, soft_max=100.0)
    bpy.types.Scene.arantools_location_scale = bpy.props.FloatProperty(
        name='Location Scale', default=5.0, soft_min=0.0, soft_max=40.0)
    bpy.types.Scene.arantools_frame_length = bpy.props.IntProperty(
        name='Frame Length', default=200, min=0, max=1000)
    bpy.types.Scene.arantools_blend_duration = bpy.props.IntProperty(
        name='Blend Duration', default=10)
    bpy.types.Scene.arantools_location_axis_multipliers = bpy.props.FloatVectorProperty(
        name='Location Axis Multipliers', size=3, default=(1.0, 1.0, 1.0), soft_min=0.0, soft_max=1.0)
    bpy.types.Scene.arantools_location_axis_multiplier_speed = bpy.props.FloatVectorProperty(
        name='Location Axis Multiplier Speed', size=3, default=(1.0, 1.0, 1.0), soft_min=0.0, soft_max=1.0)
    bpy.types.Scene.arantools_rotation_axis_multipliers = bpy.props.FloatVectorProperty(
        name='Rotation Axis Multipliers', size=3, default=(1.0, 1.0, 1.0), soft_min=0.0, soft_max=1.0)
    bpy.types.Scene.arantools_rotation_axis_multiplier_speed = bpy.props.FloatVectorProperty(
        name='Rotation Axis Multiplier Speed', size=3, default=(1.0, 1.0, 1.0), soft_min=0.0, soft_max=1.0)
    bpy.types.Scene.arantools_advanced_options = bpy.props.BoolProperty(
        name='Advanced Options', default=False)


def unregister():
    del bpy.types.Scene.arantools_advanced_options
    del bpy.types.Scene.arantools_rotation_axis_multiplier_speed
    del bpy.types.Scene.arantools_rotation_axis_multipliers
    del bpy.types.Scene.arantools_location_axis_multiplier_speed
    del bpy.types.Scene.arantools_location_axis_multipliers
    del bpy.types.Scene.arantools_blend_duration
    del bpy.types.Scene.arantools_frame_length
    del bpy.types.Scene.arantools_location_scale
    del bpy.types.Scene.arantools_location_strenght
    del bpy.types.Scene.arantools_rotation_scale
    del bpy.types.Scene.arantools_rotation_strength

    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
