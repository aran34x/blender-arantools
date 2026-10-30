"""Mirror animation: copy the keys of the selected pose bones onto their
mirror counterparts (Bone.L -> Bone.R, _l -> _r, Left -> Right ...).

Keys, handles and interpolation are copied 1:1 (nothing is resampled). The
mirror uses the same convention as Blender's "Paste Flipped Pose" (X mirror,
for bones created with Symmetrize): location X, and rotation Y/Z (Euler or
quaternion) are negated."""

import re

import bpy
from bpy.types import Operator
from bpy.props import BoolProperty

from .animation import _iter_action_fcurves, _find_action_fcurve

_PATH_RE = re.compile(r'^pose\.bones\["(.+?)"\]\.(\w+)$')

# prop -> set of array indices whose values get negated
_FLIP = {
    'location': {0},
    'rotation_quaternion': {2, 3},
    'rotation_euler': {1, 2},
    'rotation_axis_angle': {2, 3},
    'scale': set(),
}


def _mirror_name(name):
    flipped = bpy.utils.flip_name(name)
    return flipped if flipped != name else None


def _target_fcurve(ob, action, bone_name, prop, index):
    path = f'pose.bones["{bone_name}"].{prop}'
    fc = _find_action_fcurve(action, path, index)
    if fc is not None:
        return fc
    pb = ob.pose.bones[bone_name]
    try:
        pb.keyframe_insert(prop, index=index, frame=bpy.context.scene.frame_current)
    except Exception:
        return None
    return _find_action_fcurve(action, path, index)


def _copy_curve(src, dst, flip):
    s = -1.0 if flip else 1.0
    for kp in reversed(list(dst.keyframe_points)):
        dst.keyframe_points.remove(kp, fast=True)
    pts = src.keyframe_points
    dst.keyframe_points.add(len(pts))
    for d, k in zip(dst.keyframe_points, pts):
        d.co = (k.co[0], k.co[1] * s)
        d.interpolation = k.interpolation
        d.easing = k.easing
        d.handle_left_type = k.handle_left_type
        d.handle_right_type = k.handle_right_type
        d.handle_left = (k.handle_left[0], k.handle_left[1] * s)
        d.handle_right = (k.handle_right[0], k.handle_right[1] * s)
        d.type = k.type
    dst.extrapolation = src.extrapolation
    dst.update()


class ARANTOOLS_OT_MirrorAnim(Operator):
    """Copy the animation of the selected bones onto their mirror bones
(.L/.R, _l/_r, Left/Right). Keys and handles are copied, not resampled"""
    bl_idname = "arantools.mirror_anim"
    bl_label = "Mirror Animation"
    bl_options = {'REGISTER', 'UNDO'}

    flip: BoolProperty(
        name="Mirror Values",
        description="Negate location X and rotation Y/Z (symmetrized rigs). "
                    "Disable for a plain copy to the other side",
        default=True)

    @classmethod
    def poll(cls, context):
        ob = context.active_object
        return (context.mode == 'POSE' and ob is not None and ob.type == 'ARMATURE'
                and ob.animation_data is not None and ob.animation_data.action is not None)

    def execute(self, context):
        ob = context.active_object
        action = ob.animation_data.action
        sel = {pb.name for pb in (context.selected_pose_bones or [])}
        if not sel:
            self.report({'WARNING'}, "Select the bones to mirror.")
            return {'CANCELLED'}

        todo = []
        for fc in _iter_action_fcurves(action):
            m = _PATH_RE.match(fc.data_path)
            if not m or m.group(1) not in sel or m.group(2) not in _FLIP:
                continue
            todo.append((fc, m.group(1), m.group(2)))

        bones_done, skipped = set(), set()
        for fc, bname, prop in todo:
            mname = _mirror_name(bname)
            if mname is None or mname not in ob.pose.bones:
                skipped.add(bname)
                continue
            if mname in sel:
                # Both sides selected: the other side would be overwritten
                # with data that is itself being overwritten.
                skipped.add(bname)
                continue
            dst = _target_fcurve(ob, action, mname, prop, fc.array_index)
            if dst is None:
                skipped.add(bname)
                continue
            _copy_curve(fc, dst, self.flip and fc.array_index in _FLIP[prop])
            bones_done.add(mname)

        if not bones_done:
            self.report({'WARNING'}, "No mirror bones found (or both sides selected).")
            return {'CANCELLED'}
        msg = f"Mirrored onto {len(bones_done)} bone(s)."
        if skipped:
            msg += f" Skipped: {', '.join(sorted(skipped))}"
        self.report({'INFO'}, msg)
        context.view_layer.update()
        return {'FINISHED'}


classes = [ARANTOOLS_OT_MirrorAnim]


def register():
    for c in classes:
        bpy.utils.register_class(c)


def unregister():
    for c in reversed(classes):
        bpy.utils.unregister_class(c)
