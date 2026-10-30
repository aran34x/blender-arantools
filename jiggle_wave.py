"""Looped procedural bakes for pose bones, limited to the scene frame range.

* Jiggle Bake - spring/secondary-motion physics on chosen "jiggle bones". Every
  run restarts from the ORIGINAL animation (a backup of the bones' rotation
  curves is kept in the action), so runs never accumulate. The simulation is
  pre-rolled until it is periodic, so the baked result loops seamlessly.
* Wave Bake - a travelling sine wave along the selected bones (root -> tip),
  periodic over the frame range, added on top of the original animation. Also
  non-cumulative.

Both write per-frame rotation keys to the bones' rotation channels. Use
"Restore Original" to go back, "Keep Result" to drop the backup and accept.
"""

import json
import math

import bpy
from bpy.types import Operator, PropertyGroup, UIList
from bpy.props import (BoolProperty, CollectionProperty, EnumProperty,
                       FloatProperty, FloatVectorProperty, IntProperty,
                       PointerProperty, StringProperty)
from mathutils import Euler, Matrix, Quaternion, Vector

from .animation import _iter_action_fcurves

_BACKUP_KEYS = {'JIGGLE': "arantools_jiggle_backup",
                'WAVE': "arantools_wave_backup"}


# ---------------------------------------------------------------------------
# Rotation / fcurve helpers
# ---------------------------------------------------------------------------

def _rot_prop(pb):
    if pb.rotation_mode == 'QUATERNION':
        return 'rotation_quaternion', 4
    if pb.rotation_mode == 'AXIS_ANGLE':
        return 'rotation_axis_angle', 4
    return 'rotation_euler', 3


def _dp(pb, prop):
    return f'pose.bones["{pb.name}"].{prop}'


def _find_fc(action, data_path, index):
    for fc in _iter_action_fcurves(action):
        if fc.data_path == data_path and fc.array_index == index:
            return fc
    return None


def _remove_fc(action, fc):
    try:
        action.fcurves.remove(fc)
        return
    except (AttributeError, RuntimeError, TypeError):
        pass
    for layer in getattr(action, 'layers', []):
        for strip in getattr(layer, 'strips', []):
            for cbag in getattr(strip, 'channelbags', []):
                try:
                    cbag.fcurves.remove(fc)
                    return
                except (RuntimeError, TypeError, ReferenceError):
                    continue


def _clear_keys(fc):
    try:
        fc.keyframe_points.clear()
    except AttributeError:
        while len(fc.keyframe_points):
            fc.keyframe_points.remove(fc.keyframe_points[-1], fast=True)


def _bone_depth(pb):
    d = 0
    p = pb.parent
    while p is not None:
        d += 1
        p = p.parent
    return d


def _quat_from_vals(mode, vals):
    if mode == 'QUATERNION':
        q = Quaternion(vals)
        if q.magnitude < 1e-9:
            return Quaternion()
        return q.normalized()
    if mode == 'AXIS_ANGLE':
        axis = Vector(vals[1:4])
        if axis.length < 1e-9:
            return Quaternion()
        return Quaternion(axis.normalized(), vals[0])
    return Euler(vals, mode).to_quaternion()


def _vals_from_quat(mode, q, prev):
    if mode == 'QUATERNION':
        v = list(q)
        if prev is not None and sum(a * b for a, b in zip(v, prev)) < 0.0:
            v = [-x for x in v]
        return tuple(v)
    if mode == 'AXIS_ANGLE':
        axis, ang = q.to_axis_angle()
        return (ang, axis.x, axis.y, axis.z)
    e = q.to_euler(mode, Euler(prev, mode)) if prev is not None else q.to_euler(mode)
    return (e.x, e.y, e.z)


def _write_rot_samples(obj, pb, samples):
    """samples: list of (frame, values tuple) written as per-frame keys."""
    prop, ncomp = _rot_prop(pb)
    action = obj.animation_data.action
    dp = _dp(pb, prop)
    for i in range(ncomp):
        fc = _find_fc(action, dp, i)
        if fc is None:
            pb.keyframe_insert(prop, index=i, frame=samples[0][0], group=pb.name)
            fc = _find_fc(action, dp, i)
        if fc is None:
            continue
        _clear_keys(fc)
        for f, vals in samples:
            kp = fc.keyframe_points.insert(f, vals[i], options={'FAST'})
            kp.interpolation = 'BEZIER'
            kp.handle_left_type = 'AUTO_CLAMPED'
            kp.handle_right_type = 'AUTO_CLAMPED'
        fc.update()


def _eval_rot_vals(action, pb, f):
    prop, ncomp = _rot_prop(pb)
    vals = list(getattr(pb, prop))
    dp = _dp(pb, prop)
    for i in range(ncomp):
        fc = _find_fc(action, dp, i)
        if fc is not None:
            vals[i] = fc.evaluate(f)
    return tuple(vals)


# ---------------------------------------------------------------------------
# Backup / restore (what makes every run start from the original animation)
# ---------------------------------------------------------------------------

def _backup_bones(obj, kind, bone_names):
    action = obj.animation_data.action
    key = _BACKUP_KEYS[kind]
    data = json.loads(action.get(key, "{}"))
    for name in bone_names:
        if name in data:
            continue
        pb = obj.pose.bones[name]
        prop, ncomp = _rot_prop(pb)
        entry = {"prop": prop, "static": list(getattr(pb, prop)), "curves": []}
        for i in range(ncomp):
            fc = _find_fc(action, _dp(pb, prop), i)
            if fc is None:
                continue
            keys = [[k.co[0], k.co[1], k.handle_left[0], k.handle_left[1],
                     k.handle_right[0], k.handle_right[1], k.interpolation,
                     k.handle_left_type, k.handle_right_type]
                    for k in fc.keyframe_points]
            entry["curves"].append({"i": i, "k": keys})
        data[name] = entry
    action[key] = json.dumps(data)


def _restore_bones(obj, kind, bone_names=None):
    """Put the backed-up rotation animation back. Returns bones restored."""
    action = obj.animation_data.action if obj.animation_data else None
    if action is None:
        return 0
    key = _BACKUP_KEYS[kind]
    if key not in action:
        return 0
    data = json.loads(action[key])
    count = 0
    for name, entry in data.items():
        if bone_names is not None and name not in bone_names:
            continue
        pb = obj.pose.bones.get(name)
        if pb is None:
            continue
        prop = entry["prop"]
        ncomp = 3 if prop == 'rotation_euler' else 4
        saved = {c["i"]: c["k"] for c in entry["curves"]}
        for i in range(ncomp):
            fc = _find_fc(action, _dp(pb, prop), i)
            if i not in saved:
                if fc is not None:
                    _remove_fc(action, fc)
                continue
            if fc is None:
                pb.keyframe_insert(prop, index=i, frame=saved[i][0][0],
                                   group=pb.name)
                fc = _find_fc(action, _dp(pb, prop), i)
            _clear_keys(fc)
            fc.keyframe_points.add(len(saved[i]))
            for kp, k in zip(fc.keyframe_points, saved[i]):
                kp.co = (k[0], k[1])
                kp.interpolation = k[6]
                kp.handle_left_type = k[7]
                kp.handle_right_type = k[8]
                kp.handle_left = (k[2], k[3])
                kp.handle_right = (k[4], k[5])
            fc.update()
        setattr(pb, prop, entry["static"])
        count += 1
    return count


def _commit_backup(obj, kind):
    action = obj.animation_data.action if obj.animation_data else None
    key = _BACKUP_KEYS[kind]
    if action is not None and key in action:
        del action[key]


# ---------------------------------------------------------------------------
# Properties
# ---------------------------------------------------------------------------

class ARANTOOLS_JiggleBone(PropertyGroup):
    name: StringProperty(name="Bone")


class ARANTOOLS_Jiggle_Props(PropertyGroup):
    weight: FloatProperty(
        name="Weight",
        description="Mass of the jiggling tip. Heavier = slower, lazier, and "
                    "gravity sags it more",
        default=1.0, min=0.05, soft_max=10.0)
    response: FloatProperty(
        name="Response (frames)",
        description="Spring period at Weight 1 (shorter = stiffer, faster jiggle)",
        default=10.0, min=1.0, soft_max=60.0)
    damping: FloatProperty(
        name="Damping",
        description="1 = no wobble. Lower = more bounce / follow-through",
        default=0.35, min=0.02, soft_max=1.5)
    strength: FloatProperty(
        name="Amount", description="0 = original pose, 1 = full jiggle",
        default=1.0, min=0.0, max=1.0, subtype='FACTOR')
    gravity: FloatProperty(
        name="Gravity",
        description="Downward (-Z world, armature space) acceleration in "
                    "units/s^2. 0 = off",
        default=0.0, soft_min=0.0, soft_max=20.0)
    max_angle: FloatProperty(
        name="Max Angle",
        description="Hard limit (degrees) on how far the tip may swing away from the animated direction. Prevents bones from spinning around",
        default=70.0, min=1.0, max=170.0)
    limit_softness: FloatProperty(
        name="Limit Softness",
        description="How softly the bone meets Max Angle. 0 = hard wall (abrupt stop), "
                    "1 = cushion starts right away and the bone eases into the limit",
        default=0.5, min=0.0, max=1.0, subtype='FACTOR')
    substeps: IntProperty(
        name="Substeps", description="Minimum simulation substeps per frame",
        default=4, min=1, soft_max=16)


class ARANTOOLS_JiggleGroup(PropertyGroup):
    """A named set of jiggle bones that share one set of settings."""
    name: StringProperty(name="Group", default="Group")
    bones: CollectionProperty(type=ARANTOOLS_JiggleBone)
    bone_index: IntProperty(default=0)
    settings: PointerProperty(type=ARANTOOLS_Jiggle_Props)

class ARANTOOLS_Wave_Props(PropertyGroup):
    amplitude: FloatVectorProperty(
        name="Amplitude", size=3, subtype='XYZ', default=(15.0, 0.0, 0.0),
        description="Wave rotation in degrees around each bone's local X / Y / "
                    "Z (Y is the bone's length axis = twist)")
    cycles: IntProperty(
        name="Frequency",
        description="Multiplies the sine input: how many full waves fit in the frame range (whole number so it loops). 1 = one slow wave, 4 = four times faster. Per-bone delay is multiplied too, so the wave shape stays the same",
        default=1, min=1, soft_max=8)
    phase_mode: EnumProperty(
        name="Shape By",
        description="How the wave is laid out along the chain",
        items=[('WAVES', "Waves Along Chain",
                "Set how many full S-curves you see along the chain at once"),
               ('DEGREES', "Delay / Bone",
                "Set the delay of each bone in degrees (advanced)")],
        default='WAVES')
    chain_waves: FloatProperty(
        name="Waves Along Chain",
        description="How many full waves (S-curves) are visible along the chain "
                    "at any moment. 0.5 = a single C bend, 1 = one S, 2 = two S "
                    "shapes... Independent from Frequency (which is how fast it moves)",
        default=1.5, min=0.0, soft_max=6.0)
    phase: FloatProperty(
        name="Delay / Bone", description="How late each bone follows its parent, in degrees of the BASE wave (0 = whole chain moves together, 30 = gentle ripple)",
        default=30.0, soft_min=-180.0, soft_max=180.0)
    tip_scale: FloatProperty(
        name="Tip Scale",
        description="Amplitude multiplier at the tip relative to the root "
                    "(0 = fade out, 1 = uniform, >1 = grow)",
        default=1.0, min=0.0, soft_max=4.0)
    z_offset: FloatProperty(
        name="Z Phase Offset",
        description="Extra phase for the Z axis. 90 with X and Z both set gives "
                    "a circular motion",
        default=90.0, soft_min=-180.0, soft_max=180.0)
    reverse: BoolProperty(
        name="Reverse", description="Wave travels tip -> root", default=False)
    additive: BoolProperty(
        name="Additive",
        description="Add the wave to the existing animation (otherwise replace it)",
        default=True)
    key_mode: EnumProperty(
        name="Keys",
        description="Which keys the wave creates",
        items=[('EXTREMES', "Peaks & Valleys",
                "Only key the peak and valley of the wave (plus the loop ends); "
                "handles follow the true sine curve"),
               ('STEPS', "Every N Frames", "Bake a key every N frames")],
        default='EXTREMES')
    snap_keys: BoolProperty(
        name="Whole Frames",
        description="Snap peak/valley keys to whole frames (otherwise they sit on "
                    "the exact, possibly fractional, frame)",
        default=True)
    key_step: IntProperty(
        name="Key Every", description="Frames between baked keys",
        default=1, min=1, soft_max=4)


# ---------------------------------------------------------------------------
# Jiggle simulation
# ---------------------------------------------------------------------------

def _jiggle_sim(H, T, L, omega, zeta, grav, substeps, max_ang=math.radians(70.0),
                softness=0.5):
    """Spring-damper tail point constrained to distance L from the head.
    H, T: per-frame head / animated-tail positions (periodic). Returns the
    simulated tail position per frame; pre-rolled to be periodic.
    softness (0-1): fraction of max_ang used as a cushion zone before the wall."""
    N = len(H) - 1
    softness = max(0.0, min(1.0, softness))
    soft_start = max_ang * (1.0 - softness)
    wall_gain = 6.0 / max(softness, 0.05)
    n_sub = max(substeps, int(math.ceil(omega * 4.0)))
    dt = 1.0 / n_sub
    w2 = omega * omega
    c = 2.0 * zeta * omega

    P = T[0].copy()
    V = Vector((0.0, 0.0, 0.0))

    def cycle(rec):
        nonlocal P, V
        out = [P.copy()] if rec else None
        for k in range(N):
            for s in range(n_sub):
                a, b = s / n_sub, (s + 1) / n_sub
                ha, hb = H[k].lerp(H[k + 1], a), H[k].lerp(H[k + 1], b)
                ta, tb = T[k].lerp(T[k + 1], a), T[k].lerp(T[k + 1], b)
                hv = (hb - ha) / dt
                tv = (tb - ta) / dt
                acc = w2 * (tb - P) + c * (tv - V) + grav
                if soft_start < max_ang:
                    # Progressive "cushion": inside the soft zone a growing
                    # force + damping opposes the outward motion, so the bone
                    # eases into the limit instead of hitting a hard stop.
                    dp = P - ha
                    td0 = ta - ha
                    if dp.length > 1e-9 and td0.length > 1e-9:
                        ang0 = td0.normalized().angle(dp.normalized(), 0.0)
                        if ang0 > soft_start:
                            x = min(1.0, (ang0 - soft_start) / (max_ang - soft_start))
                            away = (dp.normalized() - td0.normalized())
                            if away.length > 1e-9:
                                away.normalize()
                                rv = (V - hv).dot(away)
                                acc = acc - away * (w2 * L * x * wall_gain)
                                if rv > 0.0:
                                    acc = acc - away * (c * rv * x)
                V = V + acc * dt
                P = P + V * dt
                d = P - hb
                ln = d.length
                if ln > 1e-9:
                    n = d / ln
                    P = hb + n * L
                    V = V - n * ((V - hv).dot(n))
                    td = tb - hb
                    if td.length > 1e-9:
                        tn = td.normalized()
                        ang = tn.angle(n, 0.0)
                        if ang > max_ang:
                            q = tn.rotation_difference(n)
                            q = Quaternion().slerp(q, max_ang / ang)
                            n_new = (q @ tn).normalized()
                            away = n_new - tn
                            P = hb + n_new * L
                            if away.length > 1e-9:
                                away.normalize()
                                rv = V - hv
                                out_v = rv.dot(away)
                                if out_v > 0.0:
                                    V = V - away * out_v
            if rec:
                out.append(P.copy())
        return out

    prev = None
    for _ in range(16):
        cycle(False)
        if prev is not None and (P - prev).length < 1e-4 * max(L, 1e-6):
            break
        prev = P.copy()
    out = cycle(True)
    out[N] = out[0].copy()
    return out


def bake_jiggle(context, obj, entries):
    """entries: list of (bone_name, settings). Each bone is simulated with its
    own group's settings; all bones are processed parent-first so groups still
    influence each other correctly."""
    scene = context.scene
    f0, f1 = scene.frame_start, scene.frame_end
    N = f1 - f0
    if N < 2:
        raise ValueError("Frame range is too short.")
    settings = {n: s for n, s in entries if n in obj.pose.bones}
    bones = [obj.pose.bones[n] for n in settings]
    bones.sort(key=_bone_depth)
    if not bones:
        raise ValueError("No valid jiggle bones.")

    bnames = [b.name for b in bones]
    _backup_bones(obj, 'JIGGLE', bnames)
    _restore_bones(obj, 'JIGGLE', set(bnames))

    fps = max(1.0, scene.render.fps / scene.render.fps_base)

    # ONE sweep through the timeline: record the original armature-space pose
    # matrices of every jiggle bone and its parent. Everything after this is
    # pure matrix math (no more depsgraph evaluations), which is what makes
    # the bake fast.
    need = {}
    for pb in bones:
        need[pb.name] = pb
        if pb.parent is not None:
            need[pb.parent.name] = pb.parent
    orig = {n: [] for n in need}
    saved_frame = scene.frame_current
    try:
        for f in range(f0, f1 + 1):
            scene.frame_set(f)
            for n, pb in need.items():
                orig[n].append(pb.matrix.copy())
    finally:
        scene.frame_set(saved_frame)

    new_m = {}   # name -> per-frame simulated armature-space matrices
    inv_cache = {}

    def parent_new(pb, k):
        par = pb.parent
        if par is None:
            return None
        lst = new_m.get(par.name)
        return lst[k] if lst is not None else orig[par.name][k]

    for pb in bones:
        p = settings[pb.name]
        ts = max(0.05, getattr(scene, "arantools_time_scale", 1.0))
        g = Vector((0.0, 0.0, -p.gravity * ts * ts / (fps * fps)))
        omega = 2.0 * math.pi / (max(1.0, p.response) * math.sqrt(max(0.01, p.weight)))
        omega *= ts
        L = pb.bone.length
        par = pb.parent
        H, T, M3, MT = [], [], [], []
        for k in range(N + 1):
            if par is not None:
                # Target pose = (simulated) parent pose * original relative pose.
                rel = orig[par.name][k].inverted_safe() @ orig[pb.name][k]
                M = parent_new(pb, k) @ rel
            else:
                M = orig[pb.name][k]
            m3 = M.to_3x3()
            head = M.translation.copy()
            H.append(head)
            T.append(head + m3 @ Vector((0.0, L, 0.0)))
            M3.append(m3)
        L_eff = (T[0] - H[0]).length or L
        sim = _jiggle_sim(H, T, L_eff, omega, p.damping, g, p.substeps,
                          math.radians(p.max_angle), p.limit_softness)

        if par is not None:
            rest_rel_inv = (par.bone.matrix_local.inverted_safe()
                            @ pb.bone.matrix_local).inverted_safe()
        else:
            rest_rel_inv = pb.bone.matrix_local.inverted_safe()

        mats, samples, prev = [], [], None
        mode = pb.rotation_mode
        for k, f in enumerate(range(f0, f1 + 1)):
            cur = T[k] - H[k]
            tgt = sim[k] - H[k]
            m3 = M3[k]
            if cur.length > 1e-9 and tgt.length > 1e-9:
                cur_n = cur.normalized()
                new_dir = cur_n.lerp(tgt.normalized(), p.strength)
                if new_dir.length < 1e-9:
                    new_dir = cur_n
                new_dir.normalize()
                m3 = cur_n.rotation_difference(new_dir).to_matrix() @ m3
            M = Matrix.Translation(H[k]) @ m3.to_4x4()
            mats.append(M)
            pm = parent_new(pb, k)
            basis = rest_rel_inv @ (pm.inverted_safe() @ M if pm is not None else M)
            q = basis.to_3x3().normalized().to_quaternion()
            vals = _vals_from_quat(mode, q, prev)
            prev = vals
            samples.append((f, vals))
        new_m[pb.name] = mats
        _write_rot_samples(obj, pb, samples)
    scene.frame_set(scene.frame_current)
    return len(bones)

# ---------------------------------------------------------------------------
# Wave bake
# ---------------------------------------------------------------------------

def bake_wave(context, obj, names, p):
    scene = context.scene
    f0, f1 = scene.frame_start, scene.frame_end
    N = f1 - f0
    if N < 2:
        raise ValueError("Frame range is too short.")
    sel = {n for n in names if n in obj.pose.bones}
    bones = [obj.pose.bones[n] for n in sel]
    if not bones:
        raise ValueError("Select the bones of the chain first.")

    # Wave index = number of selected ancestors (branches stay in sync).
    def index_of(pb):
        i, q = 0, pb.parent
        while q is not None:
            if q.name in sel:
                i += 1
            q = q.parent
        return i

    idx = {pb.name: index_of(pb) for pb in bones}
    max_idx = max(idx.values()) or 1
    bones.sort(key=_bone_depth)

    bnames = [b.name for b in bones]
    _backup_bones(obj, 'WAVE', bnames)
    _restore_bones(obj, 'WAVE', set(bnames))
    action = obj.animation_data.action

    frames_step = list(range(f0, f1 + 1, p.key_step))
    if frames_step[-1] != f1:
        frames_step.append(f1)
    amp = [math.radians(a) for a in p.amplitude]
    sgn = -1.0 if p.reverse else 1.0
    axis_ph = (0.0, math.pi * 0.5, math.radians(p.z_offset))
    two_pi = 2.0 * math.pi
    # Per-bone phase step in radians of the *final* sine argument (not scaled by Frequency).
    if p.phase_mode == 'WAVES':
        step_rad = two_pi * p.chain_waves / max_idx
    else:
        step_rad = math.radians(p.phase) * p.cycles

    def extreme_frames(i):
        """Frames where any active axis of this bone's wave peaks/bottoms out."""
        out = []
        shift = sgn * step_rad * i
        for a in range(3):
            if abs(amp[a]) < 1e-9:
                continue
            base = (math.pi * 0.5 - axis_ph[a] + shift) / p.cycles
            step = math.pi / p.cycles
            n0 = int(math.floor((-base) / step)) - 1
            n1 = int(math.ceil((two_pi - base) / step)) + 1
            for n in range(n0, n1 + 1):
                f = f0 + N / two_pi * (base + n * step)
                if f0 - 1e-6 <= f <= f1 + 1e-6:
                    out.append(min(max(f, f0), f1))
        return out

    def wave_quat(i, env, f):
        theta = p.cycles * two_pi * (f - f0) / N - sgn * step_rad * i
        return Euler((amp[0] * env * math.sin(theta + axis_ph[0]),
                      amp[1] * env * math.sin(theta + axis_ph[1]),
                      amp[2] * env * math.sin(theta + axis_ph[2])),
                     'XYZ').to_quaternion()

    for pb in bones:
        i = idx[pb.name]
        t = i / max_idx
        env = 1.0 + (p.tip_scale - 1.0) * t
        mode = pb.rotation_mode

        def vals_at(f, prev):
            base = (_quat_from_vals(mode, _eval_rot_vals(action, pb, f))
                    if p.additive else Quaternion())
            return _vals_from_quat(mode, base @ wave_quat(i, env, f), prev)

        if p.key_mode == 'STEPS':
            frames = frames_step
        else:
            fs = [float(f0), float(f1)] + extreme_frames(i)
            if p.additive:
                # Keep the keys the original animation already had.
                prop, ncomp = _rot_prop(pb)
                for c in range(ncomp):
                    fc0 = _find_fc(action, _dp(pb, prop), c)
                    if fc0 is not None:
                        fs += [k.co[0] for k in fc0.keyframe_points if f0 <= k.co[0] <= f1]
            if p.snap_keys:
                fs = [float(round(f)) for f in fs]
            fs.sort()
            frames = []
            for f in fs:
                if not frames or f - frames[-1] > 1e-3:
                    frames.append(f)
            if len(frames) < 2:
                frames = [float(f0), float(f1)]

        prev, samples = None, []
        for f in frames:
            prev = vals_at(f, prev)
            samples.append((f, prev))

        if p.key_mode == 'STEPS':
            _write_rot_samples(obj, pb, samples)
            continue

        # Peaks & valleys: key those frames only, with handles taken from the
        # true curve slope (flat at peaks/valleys, correct at the loop ends),
        # so a few keys still reproduce the smooth sine.
        eps = 0.01
        ncomp = len(samples[0][1])
        slopes = []
        for f, v in samples:
            a = vals_at(f - eps, v)
            b = vals_at(f + eps, v)
            slopes.append([(b[c] - a[c]) / (2.0 * eps) for c in range(ncomp)])
        prop, _n = _rot_prop(pb)
        dp = _dp(pb, prop)
        for c in range(ncomp):
            fc = _find_fc(action, dp, c)
            if fc is None:
                pb.keyframe_insert(prop, index=c, frame=frames[0], group=pb.name)
                fc = _find_fc(action, dp, c)
            if fc is None:
                continue
            _clear_keys(fc)
            fc.keyframe_points.add(len(samples))
            for k, (kp, (f, v)) in enumerate(zip(fc.keyframe_points, samples)):
                gl = (f - samples[k - 1][0]) if k > 0 else (samples[1][0] - f)
                gr = (samples[k + 1][0] - f) if k < len(samples) - 1 else gl
                kp.co = (f, v[c])
                kp.interpolation = 'BEZIER'
                kp.handle_left_type = 'FREE'
                kp.handle_right_type = 'FREE'
                s = slopes[k][c]
                kp.handle_left = (f - gl / 3.0, v[c] - s * gl / 3.0)
                kp.handle_right = (f + gr / 3.0, v[c] + s * gr / 3.0)
            fc.update()
    return len(bones)


# ---------------------------------------------------------------------------
# Operators
# ---------------------------------------------------------------------------

def _pose_poll(context):
    ob = context.active_object
    return (context.mode == 'POSE' and ob is not None and ob.type == 'ARMATURE'
            and ob.animation_data is not None and ob.animation_data.action is not None)


def _groups(ob):
    return ob.arantools_jiggle_groups


def _active_group(ob):
    gs = ob.arantools_jiggle_groups
    if 0 <= ob.arantools_jiggle_group_index < len(gs):
        return gs[ob.arantools_jiggle_group_index]
    return None


def _all_group_entries(ob, only_group=None):
    entries = []
    for gi, grp in enumerate(ob.arantools_jiggle_groups):
        # RNA wrappers are recreated on each access, so `is` never matches;
        # compare by index instead.
        if only_group is not None and gi != ob.arantools_jiggle_group_index:
            continue
        for b in grp.bones:
            entries.append((b.name, grp.settings))
    return entries


class ARANTOOLS_UL_JiggleGroups(UIList):
    def draw_item(self, context, layout, data, item, icon, active_data,
                  active_propname, index):
        row = layout.row(align=True)
        row.prop(item, "name", text="", emboss=False, icon='GROUP_BONE')
        row.label(text=str(len(item.bones)))


class ARANTOOLS_UL_JiggleBones(UIList):
    def draw_item(self, context, layout, data, item, icon, active_data,
                  active_propname, index):
        layout.label(text=item.name, icon='BONE_DATA')


class _GroupOp:
    @classmethod
    def poll(cls, context):
        return context.active_object is not None and context.active_object.type == 'ARMATURE'


class ARANTOOLS_OT_JiggleAddGroup(_GroupOp, Operator):
    """Create a new jiggle group (with its own settings)"""
    bl_idname = "arantools.jiggle_add_group"
    bl_label = "Add Group"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        ob = context.active_object
        grp = ob.arantools_jiggle_groups.add()
        grp.name = f"Group {len(ob.arantools_jiggle_groups)}"
        ob.arantools_jiggle_group_index = len(ob.arantools_jiggle_groups) - 1
        return {'FINISHED'}


class ARANTOOLS_OT_JiggleRemoveGroup(_GroupOp, Operator):
    """Delete the active jiggle group (animation already baked is kept)"""
    bl_idname = "arantools.jiggle_remove_group"
    bl_label = "Remove Group"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        ob = context.active_object
        i = ob.arantools_jiggle_group_index
        if 0 <= i < len(ob.arantools_jiggle_groups):
            ob.arantools_jiggle_groups.remove(i)
            ob.arantools_jiggle_group_index = max(0, i - 1)
        return {'FINISHED'}


class ARANTOOLS_OT_JiggleAddBones(_GroupOp, Operator):
    """Add the selected pose bones to the active group (a bone can only be in one group)"""
    bl_idname = "arantools.jiggle_add_bones"
    bl_label = "Add Selected"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        ob = context.active_object
        grp = _active_group(ob)
        if grp is None:
            self.report({'WARNING'}, "Create a group first.")
            return {'CANCELLED'}
        sel = [pb.name for pb in (context.selected_pose_bones or [])]
        # Move bones out of any other group.
        for oi, other in enumerate(ob.arantools_jiggle_groups):
            if oi == ob.arantools_jiggle_group_index:
                continue
            for i in reversed(range(len(other.bones))):
                if other.bones[i].name in sel:
                    other.bones.remove(i)
        have = {b.name for b in grp.bones}
        n = 0
        for name in sel:
            if name not in have:
                grp.bones.add().name = name
                n += 1
        self.report({'INFO'}, f"Added {n} bone(s) to '{grp.name}'.")
        return {'FINISHED'}


class ARANTOOLS_OT_JiggleRemoveBones(_GroupOp, Operator):
    """Remove the selected pose bones from the active group"""
    bl_idname = "arantools.jiggle_remove_bones"
    bl_label = "Remove Selected"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        grp = _active_group(context.active_object)
        if grp is None:
            return {'CANCELLED'}
        sel = {pb.name for pb in (context.selected_pose_bones or [])}
        removed = 0
        for i in reversed(range(len(grp.bones))):
            if grp.bones[i].name in sel:
                grp.bones.remove(i)
                removed += 1
        if not removed and 0 <= grp.bone_index < len(grp.bones):
            # Nothing selected in the viewport: remove the highlighted list entry.
            grp.bones.remove(grp.bone_index)
            grp.bone_index = max(0, grp.bone_index - 1)
        return {'FINISHED'}


class ARANTOOLS_OT_JiggleSelectBones(_GroupOp, Operator):
    """Select the bones of the active group"""
    bl_idname = "arantools.jiggle_select_bones"
    bl_label = "Select Group Bones"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        ob = context.active_object
        grp = _active_group(ob)
        if grp is None:
            return {'CANCELLED'}
        names = {b.name for b in grp.bones}
        for pb in ob.pose.bones:
            pb.bone.select = pb.name in names
        return {'FINISHED'}


class ARANTOOLS_OT_JiggleBake(Operator):
    """Simulate jiggle on the group bones over the scene frame range and bake it
as a seamless loop. Always restarts from the original animation (no accumulation)"""
    bl_idname = "arantools.jiggle_bake"
    bl_label = "Bake Jiggle"
    bl_options = {'REGISTER', 'UNDO'}

    only_active: BoolProperty(
        name="Active Group Only",
        description="Bake just the active group instead of all groups",
        default=False)

    @classmethod
    def poll(cls, context):
        return _pose_poll(context)

    def execute(self, context):
        ob = context.active_object
        grp = _active_group(ob) if self.only_active else None
        entries = _all_group_entries(ob, grp)
        if not entries:
            self.report({'WARNING'}, "No jiggle bones - add bones to a group first.")
            return {'CANCELLED'}
        try:
            n = bake_jiggle(context, ob, entries)
        except ValueError as e:
            self.report({'ERROR'}, str(e))
            return {'CANCELLED'}
        self.report({'INFO'}, f"Jiggle baked on {n} bone(s), frames "
                              f"{context.scene.frame_start}-{context.scene.frame_end}.")
        return {'FINISHED'}

class ARANTOOLS_OT_WaveBake(Operator):
    """Bake a looping travelling wave onto the selected bones (root to tip) over
the scene frame range. Restarts from the original animation on every run"""
    bl_idname = "arantools.wave_bake"
    bl_label = "Bake Wave"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return _pose_poll(context)

    def execute(self, context):
        ob = context.active_object
        names = [pb.name for pb in (context.selected_pose_bones or [])]
        try:
            n = bake_wave(context, ob, names, context.scene.arantools_wave)
        except ValueError as e:
            self.report({'ERROR'}, str(e))
            return {'CANCELLED'}
        self.report({'INFO'}, f"Wave baked on {n} bone(s).")
        return {'FINISHED'}


class ARANTOOLS_OT_RotBakeRestore(Operator):
    """Restore the original rotation animation from before the bake"""
    bl_idname = "arantools.rot_bake_restore"
    bl_label = "Restore Original"
    bl_options = {'REGISTER', 'UNDO'}

    kind: EnumProperty(items=[('JIGGLE', "Jiggle", ""), ('WAVE', "Wave", "")])

    @classmethod
    def poll(cls, context):
        return _pose_poll(context)

    def execute(self, context):
        ob = context.active_object
        if self.kind == 'JIGGLE':
            names = {n for n, _s in _all_group_entries(ob)}
        else:
            names = {pb.name for pb in (context.selected_pose_bones or [])}
        n = _restore_bones(ob, self.kind, names or None)
        if not n:
            self.report({'WARNING'}, "Nothing to restore for these bones.")
            return {'CANCELLED'}
        context.scene.frame_set(context.scene.frame_current)
        self.report({'INFO'}, f"Restored {n} bone(s).")
        return {'FINISHED'}


class ARANTOOLS_OT_RotBakeCommit(Operator):
    """Accept the baked result: delete the stored original so it can't be
restored (use before hand-editing the baked keys)"""
    bl_idname = "arantools.rot_bake_commit"
    bl_label = "Keep Result"
    bl_options = {'REGISTER', 'UNDO'}

    kind: EnumProperty(items=[('JIGGLE', "Jiggle", ""), ('WAVE', "Wave", "")])

    @classmethod
    def poll(cls, context):
        return _pose_poll(context)

    def execute(self, context):
        _commit_backup(context.active_object, self.kind)
        self.report({'INFO'}, "Backup removed; result kept.")
        return {'FINISHED'}


classes = [
    ARANTOOLS_JiggleBone,
    ARANTOOLS_Jiggle_Props,
    ARANTOOLS_JiggleGroup,
    ARANTOOLS_Wave_Props,
    ARANTOOLS_UL_JiggleGroups,
    ARANTOOLS_UL_JiggleBones,
    ARANTOOLS_OT_JiggleAddGroup,
    ARANTOOLS_OT_JiggleRemoveGroup,
    ARANTOOLS_OT_JiggleAddBones,
    ARANTOOLS_OT_JiggleRemoveBones,
    ARANTOOLS_OT_JiggleSelectBones,
    ARANTOOLS_OT_JiggleBake,
    ARANTOOLS_OT_WaveBake,
    ARANTOOLS_OT_RotBakeRestore,
    ARANTOOLS_OT_RotBakeCommit,
]


def register():
    for c in classes:
        bpy.utils.register_class(c)
    bpy.types.Scene.arantools_wave = PointerProperty(type=ARANTOOLS_Wave_Props)
    bpy.types.Object.arantools_jiggle_groups = CollectionProperty(type=ARANTOOLS_JiggleGroup)
    bpy.types.Object.arantools_jiggle_group_index = IntProperty(default=0)


def unregister():
    for attr, owner in (('arantools_wave', bpy.types.Scene),
                        ('arantools_jiggle_groups', bpy.types.Object),
                        ('arantools_jiggle_group_index', bpy.types.Object)):
        if hasattr(owner, attr):
            delattr(owner, attr)
    for c in reversed(classes):
        bpy.utils.unregister_class(c)
