import bpy
import random
from bpy.types import Operator


_NOISE_NAME = "Aran_Noise"


# ============================================================================
# Shared helpers
# ============================================================================

def _setup_noise(fcurve, axis_idx, strength, scale, frame_length, blend,
                 axis_strength_mult, axis_speed_mult,
                 strength_divisor=1.0, scale_divisor=1.0):
    """Add or replace the Aran_Noise modifier on a single F-curve.

    The divisors allow the UI to display large, human-friendly values while
    feeding sane numbers to the modifier:
      effective_strength = (strength / strength_divisor) * axis_mult
      effective_scale    = scale / (scale_divisor * axis_speed_mult)
    """
    mod = next((m for m in fcurve.modifiers if m.name == _NOISE_NAME), None)
    if mod is None:
        mod = fcurve.modifiers.new(type='NOISE')
        mod.name = _NOISE_NAME

    eff_str_div  = max(0.001, strength_divisor)
    eff_scl_div  = max(0.001, scale_divisor)
    axis_speed   = max(0.001, axis_speed_mult[axis_idx])

    mod.strength             = (strength / eff_str_div) * axis_strength_mult[axis_idx]
    mod.scale                = scale / (eff_scl_div * axis_speed)
    mod.phase                = random.uniform(0, 1000)
    mod.frame_start          = 0
    mod.frame_end            = frame_length
    mod.use_restricted_range = True
    mod.blend_in             = blend
    mod.blend_out            = blend


def _collect_rot_args(scene):
    return dict(
        strength          = scene.arantools_rotation_strength,
        scale             = scene.arantools_rotation_scale,
        axis_strength_mult = scene.arantools_rotation_axis_multipliers,
        axis_speed_mult   = scene.arantools_rotation_axis_multiplier_speed,
        strength_divisor  = 1.0,
        scale_divisor     = scene.arantools_scale_divisor,
    )


def _collect_loc_args(scene):
    return dict(
        strength          = scene.arantools_location_strenght,
        scale             = scene.arantools_location_scale,
        axis_strength_mult = scene.arantools_location_axis_multipliers,
        axis_speed_mult   = scene.arantools_location_axis_multiplier_speed,
        strength_divisor  = scene.arantools_location_strength_divisor,
        scale_divisor     = scene.arantools_scale_divisor,
    )


def _apply_rotation_noise(context):
    scene    = context.scene
    armature = context.active_object
    kwargs   = _collect_rot_args(scene)
    common   = dict(frame_length=scene.arantools_frame_length,
                    blend=scene.arantools_blend_duration)
    for bone in context.selected_pose_bones:
        for channel in ('rotation_euler', 'rotation_quaternion'):
            for axis in range(3):
                fc = armature.animation_data.action.fcurves.find(
                    f'pose.bones["{bone.name}"].{channel}', index=axis)
                if fc:
                    _setup_noise(fc, axis, **kwargs, **common)


def _apply_location_noise(context):
    scene    = context.scene
    armature = context.active_object
    kwargs   = _collect_loc_args(scene)
    common   = dict(frame_length=scene.arantools_frame_length,
                    blend=scene.arantools_blend_duration)
    for bone in context.selected_pose_bones:
        for axis in range(3):
            fc = armature.animation_data.action.fcurves.find(
                f'pose.bones["{bone.name}"].location', index=axis)
            if fc:
                _setup_noise(fc, axis, **kwargs, **common)


def _check_pose(context):
    if context.mode != 'POSE':
        return "Must be in Pose Mode."
    arm = context.active_object
    if not arm or arm.type != 'ARMATURE':
        return "Active object must be an armature."
    if not arm.animation_data or not arm.animation_data.action:
        return "Armature has no active Action."
    if not context.selected_pose_bones:
        return "No pose bones selected."
    return None


# ============================================================================
# Operators
# ============================================================================

class ARANTOOLS_OT_Apply_Noise_Rotation(Operator):
    """Add or update Aran_Noise FCurve modifiers on the rotation channels
of every selected pose bone. Re-running randomises the phase offset."""
    bl_idname  = "arantools.apply_noise_rotation"
    bl_label   = "Rotation"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return context.mode == 'POSE'

    def execute(self, context):
        err = _check_pose(context)
        if err:
            self.report({'ERROR'}, err)
            return {'CANCELLED'}
        _apply_rotation_noise(context)
        return {'FINISHED'}


class ARANTOOLS_OT_Apply_Noise_Location(Operator):
    """Add or update Aran_Noise FCurve modifiers on the location channels
of every selected pose bone. Re-running randomises the phase offset."""
    bl_idname  = "arantools.apply_noise_location"
    bl_label   = "Location"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return context.mode == 'POSE'

    def execute(self, context):
        err = _check_pose(context)
        if err:
            self.report({'ERROR'}, err)
            return {'CANCELLED'}
        _apply_location_noise(context)
        return {'FINISHED'}


class ARANTOOLS_OT_Apply_Noise_Both(Operator):
    """Add or update Aran_Noise on both rotation and location channels
of every selected pose bone in a single click."""
    bl_idname  = "arantools.apply_noise_both"
    bl_label   = "Both"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return context.mode == 'POSE'

    def execute(self, context):
        err = _check_pose(context)
        if err:
            self.report({'ERROR'}, err)
            return {'CANCELLED'}
        _apply_rotation_noise(context)
        _apply_location_noise(context)
        return {'FINISHED'}


class ARANTOOLS_OT_Remove_Noise(Operator):
    """Remove all Aran_Noise FCurve modifiers from every F-curve belonging
to the selected pose bones in the active action."""
    bl_idname  = "arantools.remove_noise"
    bl_label   = "Remove Noise"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return (context.mode == 'POSE'
                and context.active_object
                and context.active_object.animation_data
                and context.active_object.animation_data.action)

    def execute(self, context):
        action  = context.active_object.animation_data.action
        removed = 0
        for bone in context.selected_pose_bones:
            prefix = f'pose.bones["{bone.name}"]'
            for fc in action.fcurves:
                if not fc.data_path.startswith(prefix):
                    continue
                for mod in list(fc.modifiers):
                    if mod.name == _NOISE_NAME:
                        fc.modifiers.remove(mod)
                        removed += 1
        self.report({'INFO'}, f"Removed {removed} noise modifier(s).")
        return {'FINISHED'}


# ============================================================================
# Registration
# ============================================================================

classes = [
    ARANTOOLS_OT_Apply_Noise_Rotation,
    ARANTOOLS_OT_Apply_Noise_Location,
    ARANTOOLS_OT_Apply_Noise_Both,
    ARANTOOLS_OT_Remove_Noise,
]


def register():
    for cls in classes:
        bpy.utils.register_class(cls)

    # ── Global controls ───────────────────────────────────────────────────────
    bpy.types.Scene.arantools_rotation_strength = bpy.props.FloatProperty(
        name='Rotation Strength',
        description='Overall amplitude of the rotation noise. '
                    'Multiplied by each axis\'s strength multiplier',
        default=0.2, min=0.0, soft_max=2.0)

    bpy.types.Scene.arantools_rotation_scale = bpy.props.FloatProperty(
        name='Rotation Scale',
        description='Noise pattern scale for rotation. '
                    'HIGHER = slower, broader oscillation. '
                    'LOWER = tighter, faster oscillation. '
                    'Divided internally by the Scale Divisor',
        default=5.0, soft_min=0.1, soft_max=40.0)

    bpy.types.Scene.arantools_location_strenght = bpy.props.FloatProperty(
        name='Location Strength',
        description='Overall amplitude of the location noise. '
                    'Divided internally by the Location Strength Divisor '
                    '(default ÷100) to keep values human-friendly',
        default=30.0, min=0.0, soft_max=500.0)

    bpy.types.Scene.arantools_location_scale = bpy.props.FloatProperty(
        name='Location Scale',
        description='Noise pattern scale for location. '
                    'HIGHER = slower, broader oscillation. '
                    'LOWER = tighter, faster oscillation. '
                    'Divided internally by the Scale Divisor',
        default=5.0, soft_min=0.1, soft_max=40.0)

    bpy.types.Scene.arantools_frame_length = bpy.props.IntProperty(
        name='Last Frame',
        description='Frame at which the noise fades out completely',
        default=200, min=1, max=10000)

    bpy.types.Scene.arantools_blend_duration = bpy.props.IntProperty(
        name='Blend In/Out',
        description='Number of frames to fade the noise in at frame 0 '
                    'and out at the Last Frame',
        default=10, min=0)

    # ── Divisors (advanced) ───────────────────────────────────────────────────
    bpy.types.Scene.arantools_location_strength_divisor = bpy.props.FloatProperty(
        name='Loc Strength ÷',
        description='Location strength is divided by this value before being '
                    'sent to the modifier. Increase to reduce positional movement. '
                    'Default 100: a Strength of 30 produces modifier strength 0.3',
        default=100.0, min=1.0, soft_max=1000.0)

    bpy.types.Scene.arantools_scale_divisor = bpy.props.FloatProperty(
        name='Scale ÷',
        description='The Scale value is divided by this before being sent to the '
                    'modifier. Increase to make the noise faster overall. '
                    'Default 2: a Scale of 5 produces modifier scale 2.5',
        default=2.0, min=0.01, soft_max=20.0)

    # ── Per-axis multipliers ──────────────────────────────────────────────────
    bpy.types.Scene.arantools_rotation_axis_multipliers = bpy.props.FloatVectorProperty(
        name='Rot Strength  X / Y / Z',
        description='Per-axis strength multiplier for rotation noise. '
                    '1.0 = full global strength   0.0 = axis silenced',
        size=3, default=(1.0, 1.0, 1.0), min=0.0, soft_max=2.0)

    bpy.types.Scene.arantools_rotation_axis_multiplier_speed = bpy.props.FloatVectorProperty(
        name='Rot Scale  X / Y / Z',
        description='Per-axis scale multiplier for rotation noise. '
                    'Values above 1.0 slow this axis down; below 1.0 speed it up',
        size=3, default=(1.0, 1.0, 1.0), min=0.01, soft_max=4.0)

    bpy.types.Scene.arantools_location_axis_multipliers = bpy.props.FloatVectorProperty(
        name='Loc Strength  X / Y / Z',
        description='Per-axis strength multiplier for location noise. '
                    '1.0 = full global strength   0.0 = axis silenced',
        size=3, default=(1.0, 1.0, 1.0), min=0.0, soft_max=2.0)

    bpy.types.Scene.arantools_location_axis_multiplier_speed = bpy.props.FloatVectorProperty(
        name='Loc Scale  X / Y / Z',
        description='Per-axis scale multiplier for location noise. '
                    'Values above 1.0 slow this axis down; below 1.0 speed it up',
        size=3, default=(1.0, 1.0, 1.0), min=0.01, soft_max=4.0)

    bpy.types.Scene.arantools_advanced_options = bpy.props.BoolProperty(
        name='Advanced',
        description='Show per-axis multipliers and divisor overrides',
        default=False)


def unregister():
    del bpy.types.Scene.arantools_advanced_options
    del bpy.types.Scene.arantools_location_axis_multiplier_speed
    del bpy.types.Scene.arantools_location_axis_multipliers
    del bpy.types.Scene.arantools_rotation_axis_multiplier_speed
    del bpy.types.Scene.arantools_rotation_axis_multipliers
    del bpy.types.Scene.arantools_scale_divisor
    del bpy.types.Scene.arantools_location_strength_divisor
    del bpy.types.Scene.arantools_blend_duration
    del bpy.types.Scene.arantools_frame_length
    del bpy.types.Scene.arantools_location_scale
    del bpy.types.Scene.arantools_location_strenght
    del bpy.types.Scene.arantools_rotation_scale
    del bpy.types.Scene.arantools_rotation_strength

    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
