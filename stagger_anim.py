import bpy
from bpy.types import Operator


# ============================================================================
# Helpers
# ============================================================================

def _shift_fcurve(fc, shift_frames):
    """Shift every keyframe on an F-curve by shift_frames."""
    for kp in fc.keyframe_points:
        kp.co.x           += shift_frames
        kp.handle_left.x  += shift_frames
        kp.handle_right.x += shift_frames


_CHANNEL_SUFFIXES = {
    'ALL':   None,
    'LOC':   ('location',),
    'ROT':   ('rotation_euler', 'rotation_quaternion', 'rotation_axis_angle'),
    'SCALE': ('scale',),
}


def _fcurve_matches_channel(fc, channel_filter):
    suffixes = _CHANNEL_SUFFIXES.get(channel_filter)
    if suffixes is None:
        return True
    return any(fc.data_path.endswith(s) for s in suffixes)


def _sort_bones(bones, order, armature):
    if order == 'NAME_ASC':
        return sorted(bones, key=lambda b: b.name)
    if order == 'NAME_DESC':
        return sorted(bones, key=lambda b: b.name, reverse=True)
    if order == 'INDEX':
        bone_order = {b.name: i for i, b in enumerate(armature.data.bones)}
        return sorted(bones, key=lambda b: bone_order.get(b.name, 99999))
    return list(bones)


def _sort_objects(objects, order):
    if order == 'NAME_ASC':
        return sorted(objects, key=lambda o: o.name)
    if order == 'NAME_DESC':
        return sorted(objects, key=lambda o: o.name, reverse=True)
    # INDEX falls back to name sort in object mode
    return sorted(objects, key=lambda o: o.name)


# ============================================================================
# PropertyGroup
# ============================================================================

class ARANTOOLS_PG_StaggerAnim(bpy.types.PropertyGroup):
    offset: bpy.props.IntProperty(
        name="Frame Offset",
        description="Frames to delay each successive item. "
                    "Item 0 is unchanged, item 1 shifts by 1×offset, "
                    "item 2 by 2×offset, and so on",
        default=2, min=0, max=500)

    order: bpy.props.EnumProperty(
        name="Sort By",
        description="How to order selected items before assigning offsets",
        items=[
            ('NAME_ASC',  'Name A→Z',   'Sort alphabetically A to Z'),
            ('NAME_DESC', 'Name Z→A',   'Sort alphabetically Z to A'),
            ('INDEX',     'Bone Index', "Use each bone's position in the "
                                        "armature (Pose mode only; "
                                        "falls back to A→Z in Object mode)"),
        ],
        default='NAME_ASC')

    channels: bpy.props.EnumProperty(
        name="Channels",
        description="Which animation channels to shift",
        items=[
            ('ALL',   'All',      'Shift all animation channels'),
            ('LOC',   'Location', 'Shift only location channels'),
            ('ROT',   'Rotation', 'Shift only rotation channels'),
            ('SCALE', 'Scale',    'Shift only scale channels'),
        ],
        default='ALL')

    reverse: bpy.props.BoolProperty(
        name="Reverse Order",
        description="Flip the sort so the last item in the sorted list "
                    "receives the largest offset instead of the first",
        default=False)


# ============================================================================
# Operators
# ============================================================================

class ARANTOOLS_OT_StaggerApply(Operator):
    """Offset the keyframes of each selected bone (Pose mode) or object
(Object mode) by a cumulative amount, creating a staggered ripple effect.
The first item in the sorted list is unchanged; each subsequent item is
shifted by one additional frame-offset step"""
    bl_idname  = "arantools.stagger_apply"
    bl_label   = "Apply Stagger"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return context.mode in {'POSE', 'OBJECT'}

    def execute(self, context):
        props = context.scene.arantools_stagger_anim
        if context.mode == 'POSE':
            return self._apply_pose(context, props)
        return self._apply_object(context, props)

    # ── Pose mode ─────────────────────────────────────────────────────────────

    def _apply_pose(self, context, props):
        arm = context.active_object
        if not arm or arm.type != 'ARMATURE':
            self.report({'ERROR'}, "Active object must be an armature.")
            return {'CANCELLED'}
        if not arm.animation_data or not arm.animation_data.action:
            self.report({'ERROR'}, "Armature has no active Action.")
            return {'CANCELLED'}
        bones = context.selected_pose_bones
        if not bones:
            self.report({'ERROR'}, "No pose bones selected.")
            return {'CANCELLED'}

        ordered = _sort_bones(list(bones), props.order, arm)
        if props.reverse:
            ordered.reverse()

        action  = arm.animation_data.action
        shifted = 0
        for i, bone in enumerate(ordered):
            shift = i * props.offset
            if shift == 0:
                continue
            prefix = f'pose.bones["{bone.name}"]'
            for fc in action.fcurves:
                if not fc.data_path.startswith(prefix):
                    continue
                if not _fcurve_matches_channel(fc, props.channels):
                    continue
                _shift_fcurve(fc, shift)
                shifted += 1

        self.report({'INFO'},
                    f"Staggered {len(ordered)} bone(s), shifted {shifted} F-curve(s).")
        return {'FINISHED'}

    # ── Object mode ───────────────────────────────────────────────────────────

    def _apply_object(self, context, props):
        objects = context.selected_objects
        if not objects:
            self.report({'ERROR'}, "No objects selected.")
            return {'CANCELLED'}

        ordered = _sort_objects(list(objects), props.order)
        if props.reverse:
            ordered.reverse()

        shifted = 0
        for i, obj in enumerate(ordered):
            shift = i * props.offset
            if shift == 0:
                continue
            if not obj.animation_data or not obj.animation_data.action:
                continue
            for fc in obj.animation_data.action.fcurves:
                if not _fcurve_matches_channel(fc, props.channels):
                    continue
                _shift_fcurve(fc, shift)
                shifted += 1

        self.report({'INFO'},
                    f"Staggered {len(ordered)} object(s), shifted {shifted} F-curve(s).")
        return {'FINISHED'}


# ============================================================================
# Registration
# ============================================================================

classes = [
    ARANTOOLS_PG_StaggerAnim,
    ARANTOOLS_OT_StaggerApply,
]


def register():
    for cls in classes:
        bpy.utils.register_class(cls)
    bpy.types.Scene.arantools_stagger_anim = bpy.props.PointerProperty(
        type=ARANTOOLS_PG_StaggerAnim)


def unregister():
    del bpy.types.Scene.arantools_stagger_anim
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
