import bpy
import math
import random
import re
from bpy.types import Operator


_NOISE_NAME = "Aran_Noise"

# ============================================================================
# Animation Organization — helpers, properties, operators, list, timer
# ============================================================================

_DURATION_PATTERN = re.compile(r'_(\d+)$')

# Per-armature memory of the last seen active-action name so the background
# timer only re-applies the timeline length when it actually changes.
_animorg_last_action = {}


def _parse_duration(action_name):
    """Return integer duration parsed from trailing '_NNN', or None."""
    if not action_name:
        return None
    m = _DURATION_PATTERN.search(action_name)
    return int(m.group(1)) if m else None


def _is_armature_action(action):
    """True if any fcurve targets a pose bone."""
    for fc in action.fcurves:
        if fc.data_path.startswith('pose.bones['):
            return True
    return False


def _apply_duration_to_timeline(scene, duration):
    if duration is None or duration < 1:
        return
    scene.frame_start = 1
    scene.frame_end = duration


def _assign_action(arm, action):
    """Assign `action` to armature, handling 4.4+ slotted actions."""
    if arm.animation_data is None:
        arm.animation_data_create()
    arm.animation_data.action = action
    if hasattr(arm.animation_data, 'action_slot'):
        slot = None
        if len(action.slots) > 0:
            slot = action.slots[0]
        else:
            try:
                slot = action.slots.new('OBJECT', arm.name)
            except Exception:
                slot = None
        if slot is not None:
            try:
                arm.animation_data.action_slot = slot
            except Exception:
                pass


def _poll_armature(self, obj):
    return obj.type == 'ARMATURE'


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
# Animation Organization — property group, UI list, operators
# ============================================================================

class ARANTOOLS_AnimOrg_Props(bpy.types.PropertyGroup):
    armature: bpy.props.PointerProperty(
        name="Armature",
        description="Armature whose actions you want to organize",
        type=bpy.types.Object,
        poll=_poll_armature,
    )
    new_action_name: bpy.props.StringProperty(
        name="New Action",
        description="Name with a trailing '_NNN' duration "
                    "(e.g. 'TurnWithStick_400'). The number becomes the "
                    "timeline end frame when the action is created or activated",
        default="NewAction_100",
    )
    auto_sync_timeline: bpy.props.BoolProperty(
        name="Auto-Sync Timeline",
        description="Continuously watch the armature's active action. When its "
                    "name (or the action itself) changes, re-parse the '_NNN' "
                    "suffix and apply it to the scene end frame",
        default=True,
    )
    only_armature_actions: bpy.props.BoolProperty(
        name="Only Armature Actions",
        description="Hide actions whose F-curves don't animate any pose bone",
        default=True,
    )
    action_index: bpy.props.IntProperty(default=0)


class ARANTOOLS_UL_AnimOrg_Actions(bpy.types.UIList):
    """Lists actions: active marker, editable name, fake-user toggle,
set-active button, delete button."""

    def draw_item(self, context, layout, data, item, icon,
                  active_data, active_propname, index):
        action = item
        props = context.scene.arantools_anim_org
        arm = props.armature
        is_active = (arm is not None
                     and arm.animation_data is not None
                     and arm.animation_data.action == action)

        row = layout.row(align=True)
        row.label(text="", icon='RADIOBUT_ON' if is_active else 'RADIOBUT_OFF')
        row.prop(action, "name", text="", emboss=False)
        row.prop(action, "use_fake_user", text="",
                 icon='FAKE_USER_ON' if action.use_fake_user else 'FAKE_USER_OFF',
                 emboss=False)
        op = row.operator("arantools.animorg_set_active", text="", icon='PLAY')
        op.action_name = action.name
        op = row.operator("arantools.animorg_delete_action", text="", icon='X')
        op.action_name = action.name

    def filter_items(self, context, data, propname):
        actions = getattr(data, propname)
        props = context.scene.arantools_anim_org
        helper = bpy.types.UI_UL_list

        if self.filter_name:
            flt_flags = helper.filter_items_by_name(
                self.filter_name, self.bitflag_filter_item, actions, "name",
                reverse=self.use_filter_invert,
            )
        else:
            flt_flags = [self.bitflag_filter_item] * len(actions)

        if props.only_armature_actions:
            for i, action in enumerate(actions):
                if not _is_armature_action(action):
                    flt_flags[i] = 0

        flt_neworder = helper.sort_items_by_name(actions, "name")
        return flt_flags, flt_neworder


class ARANTOOLS_OT_AnimOrg_NewAction(Operator):
    """Create a new action, enable Fake User so it survives a save,
assign it to the selected armature, and (if the name ends in '_NNN')
set the scene end frame to N."""
    bl_idname = "arantools.animorg_new_action"
    bl_label = "Create Action"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return context.scene.arantools_anim_org.armature is not None

    def execute(self, context):
        props = context.scene.arantools_anim_org
        arm = props.armature
        if arm is None:
            self.report({'ERROR'}, "Select an armature first.")
            return {'CANCELLED'}

        name = props.new_action_name.strip()
        if not name:
            self.report({'ERROR'}, "Action name cannot be empty.")
            return {'CANCELLED'}

        action = bpy.data.actions.new(name)
        action.use_fake_user = True
        _assign_action(arm, action)

        duration = _parse_duration(action.name)
        if duration is not None:
            _apply_duration_to_timeline(context.scene, duration)
        _animorg_last_action[f"{context.scene.name}|{arm.name}"] = action.name

        self.report({'INFO'}, f"Created action '{action.name}'.")
        return {'FINISHED'}


class ARANTOOLS_OT_AnimOrg_SetActive(Operator):
    """Make this action the armature's active action, ensure Fake User is on,
and (if the name ends in '_NNN') set the scene end frame to N."""
    bl_idname = "arantools.animorg_set_active"
    bl_label = "Set Active Action"
    bl_options = {'REGISTER', 'UNDO'}

    action_name: bpy.props.StringProperty()

    def execute(self, context):
        props = context.scene.arantools_anim_org
        arm = props.armature
        if arm is None:
            self.report({'ERROR'}, "Select an armature first.")
            return {'CANCELLED'}

        action = bpy.data.actions.get(self.action_name)
        if action is None:
            self.report({'ERROR'}, f"Action '{self.action_name}' not found.")
            return {'CANCELLED'}

        _assign_action(arm, action)
        action.use_fake_user = True

        duration = _parse_duration(action.name)
        if duration is not None:
            _apply_duration_to_timeline(context.scene, duration)
        _animorg_last_action[f"{context.scene.name}|{arm.name}"] = action.name
        return {'FINISHED'}


class ARANTOOLS_OT_AnimOrg_Delete(Operator):
    """Permanently delete this action from the file."""
    bl_idname = "arantools.animorg_delete_action"
    bl_label = "Delete Action"
    bl_options = {'REGISTER', 'UNDO'}

    action_name: bpy.props.StringProperty()

    @classmethod
    def description(cls, context, properties):
        return f"Permanently delete action '{properties.action_name}' from the file"

    def invoke(self, context, event):
        return context.window_manager.invoke_confirm(self, event)

    def execute(self, context):
        action = bpy.data.actions.get(self.action_name)
        if action is None:
            self.report({'ERROR'}, f"Action '{self.action_name}' not found.")
            return {'CANCELLED'}
        bpy.data.actions.remove(action)
        self.report({'INFO'}, f"Deleted '{self.action_name}'.")
        return {'FINISHED'}


class ARANTOOLS_OT_AnimOrg_SyncTimeline(Operator):
    """Re-parse the active action's '_NNN' suffix and apply it to the timeline.
Useful after renaming an action manually."""
    bl_idname = "arantools.animorg_sync_timeline"
    bl_label = "Sync Timeline to Action"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        props = context.scene.arantools_anim_org
        arm = props.armature
        if (arm is None or arm.animation_data is None
                or arm.animation_data.action is None):
            self.report({'ERROR'}, "No active action on the armature.")
            return {'CANCELLED'}
        action = arm.animation_data.action
        duration = _parse_duration(action.name)
        if duration is None:
            self.report({'WARNING'}, f"'{action.name}' has no '_NNN' suffix.")
            return {'CANCELLED'}
        _apply_duration_to_timeline(context.scene, duration)
        _animorg_last_action[f"{context.scene.name}|{arm.name}"] = action.name
        self.report({'INFO'}, f"Timeline set to 1–{duration}.")
        return {'FINISHED'}


# ── Background timer: polls the armature's active action every 0.4s
# and re-applies the parsed duration when the name or assignment changes.
def _animorg_timer():
    try:
        for scene in bpy.data.scenes:
            props = getattr(scene, 'arantools_anim_org', None)
            if props is None or not props.auto_sync_timeline or props.armature is None:
                continue
            arm = props.armature
            if arm.animation_data is None or arm.animation_data.action is None:
                continue
            key = f"{scene.name}|{arm.name}"
            current = arm.animation_data.action.name
            if _animorg_last_action.get(key) != current:
                _animorg_last_action[key] = current
                duration = _parse_duration(current)
                if duration is not None:
                    _apply_duration_to_timeline(scene, duration)
    except Exception as e:
        print(f"[AranTools animorg timer] {e}")
    return 0.4


# ============================================================================
# Spring Smooth — damped-spring resampler for transform F-curves
# ============================================================================

_POSE_BONE_DP = re.compile(r'pose\.bones\["([^"]+)"\]\.(\w+)')

# Rotation tracks come in three flavours depending on the bone's rotation_mode.
_ROT_PROPS = ('rotation_euler', 'rotation_quaternion', 'rotation_axis_angle')


def _axis_enabled_for_fcurve(prop_name, array_index, loc_axes, rot_axes, scl_axes):
    """Return True if the channel + array_index passes the per-axis locks.

    For quaternion (W,X,Y,Z) and axis_angle (angle,X,Y,Z) the leading component
    (index 0) isn't an axis and is always processed when rotation is enabled —
    only indices 1/2/3 are gated by the X/Y/Z locks."""
    if prop_name == 'location':
        return 0 <= array_index <= 2 and loc_axes[array_index]
    if prop_name == 'scale':
        return 0 <= array_index <= 2 and scl_axes[array_index]
    if prop_name == 'rotation_euler':
        return 0 <= array_index <= 2 and rot_axes[array_index]
    if prop_name in ('rotation_quaternion', 'rotation_axis_angle'):
        if array_index == 0:
            return True
        return 1 <= array_index <= 3 and rot_axes[array_index - 1]
    return False


def _collect_smooth_fcurves(context, props):
    """Yield armature-action fcurves for selected pose bones, filtered by
    the Location/Rotation/Scale toggles AND the per-axis X/Y/Z locks."""
    obj = context.active_object
    if (obj is None or obj.type != 'ARMATURE'
            or obj.animation_data is None or obj.animation_data.action is None):
        return []

    bones = context.selected_pose_bones or []
    if not bones:
        return []
    bone_names = {b.name for b in bones}

    loc_axes = props.location_axes
    rot_axes = props.rotation_axes
    scl_axes = props.scale_axes

    out = []
    for fc in obj.animation_data.action.fcurves:
        m = _POSE_BONE_DP.match(fc.data_path)
        if not m:
            continue
        bname, prop = m.group(1), m.group(2)
        if bname not in bone_names:
            continue
        if prop == 'location' and not props.apply_location:
            continue
        if prop in _ROT_PROPS and not props.apply_rotation:
            continue
        if prop == 'scale' and not props.apply_scale:
            continue
        if prop not in ('location', 'scale') and prop not in _ROT_PROPS:
            continue
        if not _axis_enabled_for_fcurve(prop, fc.array_index,
                                        loc_axes, rot_axes, scl_axes):
            continue
        out.append(fc)
    return out


def _find_anchor_indices(keys, preserve_stops, stop_tol):
    """Return sorted unique indices of 'anchor' keys.

    An anchor is the first key, the last key, or any keyframe whose value
    matches an adjacent keyframe within `stop_tol` — i.e. a 'stop point'
    where the animator held the value. The spring sim is restarted at every
    anchor with zero velocity so holds stay crisp and don't get smeared."""
    n = len(keys)
    anchors = {0, n - 1}
    if preserve_stops:
        for i in range(n):
            v = keys[i].co[1]
            if i > 0 and abs(keys[i - 1].co[1] - v) < stop_tol:
                anchors.add(i)
            if i < n - 1 and abs(keys[i + 1].co[1] - v) < stop_tol:
                anchors.add(i)
    return sorted(anchors)


def _spring_smooth_fcurve(fcurve, stiffness, damping, blend,
                          preserve_stops, stop_tol, substeps):
    """Replace fcurve keys with a per-frame bake of a critically-damped
    spring chasing the original curve. Anchor keys (stop points + endpoints)
    are preserved exactly and reset the spring velocity to zero."""
    keys = sorted(fcurve.keyframe_points, key=lambda k: k.co[0])
    if len(keys) < 2:
        return 0

    omega_n  = math.sqrt(max(1e-6, stiffness))
    damp_co  = 2.0 * damping * omega_n
    omega_sq = omega_n * omega_n
    substeps = max(1, int(substeps))
    dt       = 1.0 / substeps

    anchor_idx = _find_anchor_indices(keys, preserve_stops, stop_tol)

    f_first = int(round(keys[0].co[0]))
    f_last  = int(round(keys[-1].co[0]))
    target_at = {f: fcurve.evaluate(f) for f in range(f_first, f_last + 1)}

    smoothed = {}
    for seg in range(len(anchor_idx) - 1):
        a = anchor_idx[seg]
        b = anchor_idx[seg + 1]
        f_a = int(round(keys[a].co[0]))
        f_b = int(round(keys[b].co[0]))

        pos = keys[a].co[1]
        vel = 0.0
        smoothed[f_a] = keys[a].co[1]

        # Substep-integrate within each frame interval [f-1, f]
        for f in range(f_a + 1, f_b):
            t_prev = target_at[f - 1]
            t_curr = target_at[f]
            for s in range(substeps):
                u = (s + 1) / substeps
                target = t_prev + (t_curr - t_prev) * u
                accel  = omega_sq * (target - pos) - damp_co * vel
                vel   += accel * dt
                pos   += vel * dt
            orig = target_at[f]
            smoothed[f] = orig + (pos - orig) * blend

        smoothed[f_b] = keys[b].co[1]

    # Wipe and rewrite the curve. Using remove(..., fast=True) in a reversed
    # loop is much faster than mutating the collection mid-iteration.
    while len(fcurve.keyframe_points) > 0:
        fcurve.keyframe_points.remove(fcurve.keyframe_points[-1], fast=True)
    for f in sorted(smoothed.keys()):
        kp = fcurve.keyframe_points.insert(f, smoothed[f], options={'FAST'})
        kp.interpolation     = 'BEZIER'
        kp.handle_left_type  = 'AUTO_CLAMPED'
        kp.handle_right_type = 'AUTO_CLAMPED'
    fcurve.update()
    return len(smoothed)


class ARANTOOLS_CurveSmooth_Props(bpy.types.PropertyGroup):
    apply_location: bpy.props.BoolProperty(
        name="Location",
        description="Smooth the location channels of selected pose bones",
        default=True,
    )
    apply_rotation: bpy.props.BoolProperty(
        name="Rotation",
        description="Smooth the rotation channels (euler / quaternion / axis-angle) "
                    "of selected pose bones",
        default=True,
    )
    apply_scale: bpy.props.BoolProperty(
        name="Scale",
        description="Smooth the scale channels of selected pose bones",
        default=False,
    )
    location_axes: bpy.props.BoolVectorProperty(
        name="Location Axes",
        description="Per-axis lock for Location. Unticked axes are left untouched",
        size=3, default=(True, True, True), subtype='XYZ',
    )
    rotation_axes: bpy.props.BoolVectorProperty(
        name="Rotation Axes",
        description="Per-axis lock for Rotation. Applies to X/Y/Z for euler, "
                    "quaternion (X/Y/Z components), and axis-angle (X/Y/Z axis "
                    "components). The W / angle component is always smoothed",
        size=3, default=(True, True, True), subtype='XYZ',
    )
    scale_axes: bpy.props.BoolVectorProperty(
        name="Scale Axes",
        description="Per-axis lock for Scale. Unticked axes are left untouched",
        size=3, default=(True, True, True), subtype='XYZ',
    )
    stiffness: bpy.props.FloatProperty(
        name="Stiffness",
        description="Spring strength pulling the simulation toward the original curve. "
                    "HIGHER = follows the original more tightly (less smoothing). "
                    "LOWER = lazier, more lag, more smoothing",
        default=0.25, min=0.01, soft_max=10.0,
    )
    damping: bpy.props.FloatProperty(
        name="Damping Ratio",
        description="1.0 = critically damped (no overshoot, fastest settle). "
                    "Below 1.0 = underdamped (overshoots and bounces — Unreal-style spring). "
                    "Above 1.0 = overdamped (sluggish, never overshoots)",
        default=1.0, min=0.05, soft_max=2.0,
    )
    blend: bpy.props.FloatProperty(
        name="Strength",
        description="Mix between the original curve (0%) and the spring-smoothed result (100%)",
        default=1.0, min=0.0, max=1.0, subtype='FACTOR',
    )
    preserve_stops: bpy.props.BoolProperty(
        name="Preserve Stop Points",
        description="Detect held keys (two adjacent keys with the same value) and anchor "
                    "them exactly, resetting the spring velocity to zero. Keeps explicit "
                    "stops crisp instead of smearing through them",
        default=True,
    )
    stop_tolerance: bpy.props.FloatProperty(
        name="Stop Tolerance",
        description="Value difference below which two adjacent keys count as the same — "
                    "i.e. as a stop. 0.001 is sane for meters, radians, and scale alike",
        default=0.001, min=0.0, soft_max=1.0, precision=4,
    )
    substeps: bpy.props.IntProperty(
        name="Substeps",
        description="Spring integration substeps per frame. Higher = more stable at high "
                    "stiffness, slower to compute. 4 is plenty for typical settings",
        default=4, min=1, soft_max=16,
    )
    decimate_after: bpy.props.BoolProperty(
        name="Decimate After",
        description="After spring-smoothing, run Blender's graph.decimate to thin the "
                    "per-frame keys back down while preserving curve shape",
        default=True,
    )
    decimate_mode: bpy.props.EnumProperty(
        name="Decimate Mode",
        description="Strategy used by Blender's built-in graph.decimate operator",
        items=[
            ('ERROR', "Max Error",
             "Keep removing keys until any further removal would deviate from the "
             "baked curve by more than the tolerance. Recommended"),
            ('RATIO', "Remove Ratio",
             "Remove a fixed fraction of keys (Blender chooses which contribute least)"),
        ],
        default='ERROR',
    )
    decimate_error: bpy.props.FloatProperty(
        name="Max Error",
        description="Maximum allowed deviation from the baked curve (units match the "
                    "channel: meters for location, radians for rotation, factor for scale)",
        default=0.001, min=0.0, soft_max=1.0, precision=4,
    )
    decimate_ratio: bpy.props.FloatProperty(
        name="Remove Ratio",
        description="Fraction of keyframes to remove. 0 = keep all, 1 = remove all",
        default=0.5, min=0.0, max=1.0, subtype='FACTOR',
    )


def _decimate_fcurves(context, target_fcurves, mode, error, ratio):
    """Run Blender's built-in graph.decimate on the keyframes of
    `target_fcurves` only. Selection is set explicitly first, then we
    override the operator's context to a Graph Editor area (hijacking
    one's type briefly if none is open). Returns the number of keys
    removed across all curves."""
    if not target_fcurves:
        return 0

    obj = context.active_object
    if obj is None or obj.animation_data is None or obj.animation_data.action is None:
        return 0
    action = obj.animation_data.action

    # Select only the target curves' keys; deselect everything else so
    # decimate doesn't touch unrelated channels.
    target_ids = {id(fc) for fc in target_fcurves}
    for fc in action.fcurves:
        is_target = id(fc) in target_ids
        fc.select = is_target
        for kp in fc.keyframe_points:
            kp.select_control_point = is_target
            kp.select_left_handle   = is_target
            kp.select_right_handle  = is_target

    window = context.window
    screen = window.screen if window else None
    if screen is None or not screen.areas:
        return 0

    # Prefer an already-open Graph Editor; otherwise hijack the biggest
    # area for the duration of the call. The type swap and revert happen
    # within a single op tick — no visible flicker.
    area = next((a for a in screen.areas if a.type == 'GRAPH_EDITOR'), None)
    restore = None
    if area is None:
        area = max(screen.areas, key=lambda a: a.width * a.height)
        restore = (area, area.type)
        area.type = 'GRAPH_EDITOR'
    region = next((r for r in area.regions if r.type == 'WINDOW'), area.regions[-1])

    keys_before = sum(len(fc.keyframe_points) for fc in target_fcurves)
    try:
        with context.temp_override(window=window, area=area, region=region):
            if mode == 'ERROR':
                bpy.ops.graph.decimate(mode='ERROR', remove_error_margin=error)
            else:
                bpy.ops.graph.decimate(mode='RATIO', factor=ratio)
    finally:
        if restore is not None:
            restore[0].type = restore[1]

    keys_after = sum(len(fc.keyframe_points) for fc in target_fcurves)
    return keys_before - keys_after


class ARANTOOLS_OT_SpringSmoothCurves(Operator):
    """Spring-smooth the transform F-curves of every selected pose bone.
A critically-damped spring (tunable) chases the original curve frame by frame;
the result is baked back as keyframes. Stop points are detected and preserved."""
    bl_idname  = "arantools.spring_smooth_curves"
    bl_label   = "Spring Smooth"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return (context.mode == 'POSE'
                and context.active_object is not None
                and context.active_object.type == 'ARMATURE')

    def execute(self, context):
        props = context.scene.arantools_curve_smooth
        if not (props.apply_location or props.apply_rotation or props.apply_scale):
            self.report({'ERROR'}, "Enable at least one of Location / Rotation / Scale.")
            return {'CANCELLED'}

        fcurves = _collect_smooth_fcurves(context, props)
        if not fcurves:
            self.report({'WARNING'}, "No matching F-curves on selected pose bones.")
            return {'CANCELLED'}

        total_keys = 0
        for fc in fcurves:
            total_keys += _spring_smooth_fcurve(
                fc,
                stiffness     = props.stiffness,
                damping       = props.damping,
                blend         = props.blend,
                preserve_stops = props.preserve_stops,
                stop_tol      = props.stop_tolerance,
                substeps      = props.substeps,
            )

        removed = 0
        if props.decimate_after:
            removed = _decimate_fcurves(
                context, fcurves,
                props.decimate_mode,
                props.decimate_error,
                props.decimate_ratio,
            )

        msg = f"Spring-smoothed {len(fcurves)} curve(s), {total_keys} keys baked"
        if props.decimate_after:
            msg += f", {removed} removed by decimate"
        self.report({'INFO'}, msg + ".")
        return {'FINISHED'}


class ARANTOOLS_OT_DecimateCurves(Operator):
    """Run Blender's built-in graph.decimate on the same pose-bone transform
F-curves targeted by Spring Smooth (respects the same channel + axis filters).
Use this to thin out keys without re-running the spring sim."""
    bl_idname  = "arantools.decimate_smooth_curves"
    bl_label   = "Decimate Now"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return (context.mode == 'POSE'
                and context.active_object is not None
                and context.active_object.type == 'ARMATURE')

    def execute(self, context):
        props = context.scene.arantools_curve_smooth
        if not (props.apply_location or props.apply_rotation or props.apply_scale):
            self.report({'ERROR'}, "Enable at least one of Location / Rotation / Scale.")
            return {'CANCELLED'}
        fcurves = _collect_smooth_fcurves(context, props)
        if not fcurves:
            self.report({'WARNING'}, "No matching F-curves on selected pose bones.")
            return {'CANCELLED'}
        removed = _decimate_fcurves(
            context, fcurves,
            props.decimate_mode,
            props.decimate_error,
            props.decimate_ratio,
        )
        self.report({'INFO'}, f"Decimated {len(fcurves)} curve(s), {removed} keys removed.")
        return {'FINISHED'}


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
    ARANTOOLS_AnimOrg_Props,
    ARANTOOLS_UL_AnimOrg_Actions,
    ARANTOOLS_OT_AnimOrg_NewAction,
    ARANTOOLS_OT_AnimOrg_SetActive,
    ARANTOOLS_OT_AnimOrg_Delete,
    ARANTOOLS_OT_AnimOrg_SyncTimeline,
    ARANTOOLS_CurveSmooth_Props,
    ARANTOOLS_OT_SpringSmoothCurves,
    ARANTOOLS_OT_DecimateCurves,
    ARANTOOLS_OT_Apply_Noise_Rotation,
    ARANTOOLS_OT_Apply_Noise_Location,
    ARANTOOLS_OT_Apply_Noise_Both,
    ARANTOOLS_OT_Remove_Noise,
]


def register():
    for cls in classes:
        bpy.utils.register_class(cls)

    bpy.types.Scene.arantools_anim_org = bpy.props.PointerProperty(
        type=ARANTOOLS_AnimOrg_Props,
    )
    bpy.types.Scene.arantools_curve_smooth = bpy.props.PointerProperty(
        type=ARANTOOLS_CurveSmooth_Props,
    )

    if not bpy.app.timers.is_registered(_animorg_timer):
        bpy.app.timers.register(_animorg_timer, persistent=True)

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
    if bpy.app.timers.is_registered(_animorg_timer):
        bpy.app.timers.unregister(_animorg_timer)
    del bpy.types.Scene.arantools_curve_smooth
    del bpy.types.Scene.arantools_anim_org

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
