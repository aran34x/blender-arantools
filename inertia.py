"""Inertia Fix - loop-aware, weight-based "inertial" retiming of EXISTING keys.

A damped spring with a mass ("weight") chases the original animation curve.
Instead of baking that simulation into a pile of sampled keys, the result is
*fitted back onto the keys you already have*:

  * peaks / valleys are moved in time to where the heavy simulation would put
    them (and get the simulated amplitude),
  * every other key gets the simulated value,
  * handles are re-aimed with the simulated slope (Bezier + aligned handles),
    so the interpolation between the same keys follows the inertial motion.

Loops are respected: when the curve is cyclic (first value == last value, or a
Cycles modifier is present) the simulation is wrapped and pre-rolled until it is
periodic, first/last keys stay on their frames and share one value + one slope,
so the seam stays seamless.
"""

import math
import re

import bpy
from bpy.types import Operator
from bpy.props import (BoolProperty, EnumProperty, FloatProperty)

from .animation import _iter_action_fcurves

_POSE_DP = re.compile(r'pose\.bones\["([^"]+)"\]\.(\w+)')
_OBJ_PROPS = ('location', 'scale', 'rotation_euler',
              'rotation_quaternion', 'rotation_axis_angle')
_ROT_PROPS = ('rotation_euler', 'rotation_quaternion', 'rotation_axis_angle')

_GRID = 0.25      # simulation sample spacing in frames
_MAX_CYCLES = 10  # pre-roll cap for looping curves


# ---------------------------------------------------------------------------
# Simulation
# ---------------------------------------------------------------------------

def _simulate(targets, h, omega, zeta, loop):
    """Integrate x'' = w^2 (T - x) - 2 z w x' over the sampled target.
    Returns samples S[0..N] (same spacing as targets). For loops the sim is
    pre-rolled until periodic and then forced closed."""
    N = len(targets) - 1
    n_sub = max(1, int(math.ceil(h * omega * 5.0)))
    dt = h / n_sub
    w2 = omega * omega
    c = 2.0 * zeta * omega

    x = targets[0]
    v = 0.0

    def run_cycle(record):
        nonlocal x, v
        out = [x] if record else None
        for k in range(N):
            ta, tb = targets[k], targets[k + 1]
            for s in range(n_sub):
                tgt = ta + (tb - ta) * ((s + 1) / n_sub)
                v += (w2 * (tgt - x) - c * v) * dt
                x += v * dt
            if record:
                out.append(x)
        return out

    if not loop:
        return run_cycle(True)

    span = (max(targets) - min(targets)) or 1.0
    prev_end = None
    for _ in range(_MAX_CYCLES):
        run_cycle(False)
        if prev_end is not None and abs(x - prev_end) < 1e-5 * span:
            break
        prev_end = x
    out = run_cycle(True)
    # Force closure: remove the remaining start/end mismatch with a ramp.
    err = out[-1] - out[0]
    if err:
        for k in range(N + 1):
            out[k] -= err * (k / N)
    return out


class _Sim:
    """Wraps the sampled simulation for value / slope queries."""

    def __init__(self, samples, t0, h, loop):
        self.s, self.t0, self.h, self.loop = samples, t0, h, loop
        self.N = len(samples) - 1

    def value(self, t):
        u = (t - self.t0) / self.h
        if self.loop:
            u %= self.N
        else:
            u = min(max(u, 0.0), float(self.N))
        i = min(int(u), self.N - 1)
        f = u - i
        return self.s[i] + (self.s[i + 1] - self.s[i]) * f

    def slope(self, t):
        h = self.h
        return (self.value(t + h) - self.value(t - h)) / (2.0 * h)


# ---------------------------------------------------------------------------
# Per-curve fitting
# ---------------------------------------------------------------------------

def _key_slopes(kp):
    """(left, right) slope of the key's current handles."""
    dl = kp.co[0] - kp.handle_left[0]
    dr = kp.handle_right[0] - kp.co[0]
    sl = (kp.co[1] - kp.handle_left[1]) / dl if abs(dl) > 1e-6 else 0.0
    sr = (kp.handle_right[1] - kp.co[1]) / dr if abs(dr) > 1e-6 else 0.0
    return sl, sr


def _is_loop(fc, mode, tol):
    if mode == 'OFF':
        return False
    if mode == 'ON':
        return True
    if any(m.type == 'CYCLES' and not m.mute for m in fc.modifiers):
        return True
    pts = fc.keyframe_points
    return abs(pts[0].co[1] - pts[-1].co[1]) <= tol


def _inertia_fit_fcurve(fc, o):
    """Fit the inertial simulation onto the existing keys of `fc`.
    Returns (keys_moved, max_deviation) or None if skipped."""
    kps = sorted(fc.keyframe_points, key=lambda k: k.co[0])
    n = len(kps)
    if n < 3:
        return None

    T = [k.co[0] for k in kps]
    V = [k.co[1] for k in kps]
    t0, t1 = T[0], T[-1]
    span = t1 - t0
    if span < 1e-4:
        return None

    loop = _is_loop(fc, o.loop_mode, o.hold_tolerance)

    N = max(2, int(math.ceil(span / _GRID)))
    h = span / N
    targets = [fc.evaluate(t0 + k * h) for k in range(N + 1)]
    if loop:
        targets[-1] = targets[0] if o.loop_mode == 'ON' else targets[-1]

    omega = 2.0 * math.pi / (max(1.0, o.response) * math.sqrt(max(0.01, o.weight)))
    # Global time scale (<1 = physics calculated in slow motion).
    omega *= max(0.05, getattr(bpy.context.scene, "arantools_time_scale", 1.0))
    sim = _Sim(_simulate(targets, h, omega, o.damping, loop), t0, h, loop)

    s = o.strength
    tol = o.hold_tolerance
    orig_gap_min = min(T[i + 1] - T[i] for i in range(n - 1))
    gap_min = max(1e-3, min(0.5, 0.25 * orig_gap_min))

    t_new = list(T)
    v_new = list(V)
    m_new = [0.0] * n
    touched = [False] * n

    def is_hold(i):
        return ((i > 0 and abs(V[i] - V[i - 1]) <= tol)
                or (i < n - 1 and abs(V[i + 1] - V[i]) <= tol))

    for i in range(n):
        kp = kps[i]
        if kp.interpolation == 'CONSTANT':
            continue
        if o.preserve_holds and is_hold(i):
            continue
        end = i in (0, n - 1)
        sl, sr = _key_slopes(kp)
        orig_slope = 0.5 * (sl + sr) if 0 < i < n - 1 else (sr if i == 0 else sl)

        if end:
            if loop:
                t_eval = t0
                sv = sim.value(t_eval)
                v_new[i] = V[i] + (sv - V[i]) * s
                m_new[i] = orig_slope + (sim.slope(t_eval) - orig_slope) * s
            else:
                m_new[i] = orig_slope
            touched[i] = loop
            continue

        shift = 0.0
        if o.move_keys:
            is_max = V[i] > V[i - 1] + tol and V[i] > V[i + 1] + tol
            is_min = V[i] < V[i - 1] - tol and V[i] < V[i + 1] - tol
            if is_max or is_min:
                wl = min(o.max_shift, 0.5 * (T[i] - T[i - 1]))
                wr = min(o.max_shift, 0.75 * (T[i + 1] - T[i]))
                steps = int((wl + wr) / h)
                best_j, best_v = None, None
                for j in range(steps + 1):
                    sv = sim.value(T[i] - wl + j * h)
                    if best_v is None or (sv > best_v if is_max else sv < best_v):
                        best_j, best_v = j, sv
                if best_j is not None and 0 < best_j < steps:
                    shift = (T[i] - wl + best_j * h) - T[i]
        t_new[i] = T[i] + shift * s
        touched[i] = True
        # (value / slope filled after ordering is resolved)

    # Keep key order sane after moving.
    for i in range(1, n - 1):
        t_new[i] = max(t_new[i], t_new[i - 1] + gap_min)
    for i in range(n - 2, 0, -1):
        t_new[i] = min(t_new[i], t_new[i + 1] - gap_min)

    for i in range(1, n - 1):
        if not touched[i]:
            continue
        kp = kps[i]
        sl, sr = _key_slopes(kp)
        orig_slope = 0.5 * (sl + sr)
        sv = sim.value(t_new[i])
        v_new[i] = V[i] + (sv - V[i]) * s
        m_new[i] = orig_slope + (sim.slope(t_new[i]) - orig_slope) * s

    # Write back.
    for i in range(n):
        if not touched[i]:
            continue
        kp = kps[i]
        t, v, m = t_new[i], v_new[i], m_new[i]
        gl = (t - t_new[i - 1]) if i > 0 else (t_new[1] - t)
        gr = (t_new[i + 1] - t) if i < n - 1 else (t - t_new[i - 1])
        kp.interpolation = 'BEZIER'
        kp.handle_left_type = 'ALIGNED'
        kp.handle_right_type = 'ALIGNED'
        kp.co = (t, v)
        kp.handle_left = (t - gl / 3.0, v - m * gl / 3.0)
        kp.handle_right = (t + gr / 3.0, v + m * gr / 3.0)
    fc.update()

    moved = sum(1 for i in range(n) if touched[i])
    dev = 0.0
    for k in range(N + 1):
        t = t0 + k * h
        dev = max(dev, abs(fc.evaluate(t) - sim.value(t)))
    return moved, dev


# ---------------------------------------------------------------------------
# Operator
# ---------------------------------------------------------------------------

def _collect_fcurves(context, o):
    objs = []
    active = context.active_object
    if context.mode == 'POSE':
        if active is None or active.type != 'ARMATURE':
            return []
        names = {b.name for b in (context.selected_pose_bones or [])}
        objs = [(active, names)]
    else:
        sel = list(context.selected_objects) or ([active] if active else [])
        objs = [(ob, None) for ob in sel]

    out, seen = [], set()
    for ob, names in objs:
        ad = ob.animation_data
        if ad is None or ad.action is None:
            continue
        for fc in _iter_action_fcurves(ad.action):
            if fc.lock or fc.mute or id(fc) in seen:
                continue
            if names is not None:
                m = _POSE_DP.match(fc.data_path)
                if not m or m.group(1) not in names:
                    continue
                prop = m.group(2)
            else:
                prop = fc.data_path
            if prop == 'location' and not o.apply_location:
                continue
            if prop in _ROT_PROPS and not o.apply_rotation:
                continue
            if prop == 'scale' and not o.apply_scale:
                continue
            if prop not in _OBJ_PROPS:
                continue
            seen.add(id(fc))
            out.append(fc)
    return out


class ARANTOOLS_OT_InertiaFix(Operator):
    """Add weighted inertia to the selected bones' / objects' animation by moving
the EXISTING keys and re-aiming their handles (no sampled keys). Loop-aware"""
    bl_idname = "arantools.inertia_fix"
    bl_label = "Inertia Fix"
    bl_options = {'REGISTER', 'UNDO'}

    weight: FloatProperty(
        name="Weight",
        description="Mass of the simulated follower. Heavier = more lag, rounder, "
                    "bigger overshoot of direction changes; lighter = tighter",
        default=1.0, min=0.05, soft_max=10.0)
    response: FloatProperty(
        name="Response (frames)",
        description="Period of the spring at Weight 1. Larger = looser, slower "
                    "settle",
        default=8.0, min=1.0, soft_max=60.0)
    damping: FloatProperty(
        name="Damping",
        description="1 = no overshoot. Lower = bouncier / more follow-through",
        default=0.8, min=0.05, soft_max=2.0)
    strength: FloatProperty(
        name="Strength",
        description="Blend between original (0) and fully inertial (1) keys",
        default=1.0, min=0.0, max=1.0, subtype='FACTOR')
    move_keys: BoolProperty(
        name="Move Peaks In Time",
        description="Shift peak/valley keys to where the simulation puts them "
                    "(otherwise only values and handles change)",
        default=True)
    max_shift: FloatProperty(
        name="Max Shift (frames)",
        description="Limit on how far a key may be moved in time",
        default=4.0, min=0.0, soft_max=24.0)
    loop_mode: EnumProperty(
        name="Loop",
        items=[('AUTO', "Auto", "Loop if first/last values match or a Cycles "
                                "modifier exists"),
               ('ON', "Always", "Treat every curve as a seamless loop"),
               ('OFF', "Never", "Treat curves as one-shot")],
        default='AUTO')
    preserve_holds: BoolProperty(
        name="Preserve Holds",
        description="Leave keys that hold a value (equal to a neighbour) alone",
        default=True)
    hold_tolerance: FloatProperty(
        name="Tolerance", default=0.001, min=0.0, soft_max=0.1, precision=4,
        description="Value difference treated as equal (holds / loop detection "
                    "/ what counts as a peak)")
    apply_location: BoolProperty(name="Location", default=True)
    apply_rotation: BoolProperty(name="Rotation", default=True)
    apply_scale: BoolProperty(name="Scale", default=False)

    @classmethod
    def poll(cls, context):
        return context.active_object is not None

    def invoke(self, context, event):
        return context.window_manager.invoke_props_dialog(self, width=300)

    def draw(self, context):
        col = self.layout.column(align=True)
        col.prop(self, "weight")
        col.prop(self, "response")
        col.prop(self, "damping", slider=True)
        col.prop(self, "strength", slider=True)
        col.separator()
        col.prop(self, "move_keys")
        sub = col.column()
        sub.enabled = self.move_keys
        sub.prop(self, "max_shift")
        col.prop(self, "loop_mode")
        col.prop(self, "preserve_holds")
        col.prop(self, "hold_tolerance")
        row = col.row(align=True)
        row.prop(self, "apply_location", toggle=True)
        row.prop(self, "apply_rotation", toggle=True)
        row.prop(self, "apply_scale", toggle=True)

    def execute(self, context):
        if not (self.apply_location or self.apply_rotation or self.apply_scale):
            self.report({'ERROR'}, "Enable Location / Rotation / Scale.")
            return {'CANCELLED'}
        fcurves = _collect_fcurves(context, self)
        if not fcurves:
            self.report({'WARNING'}, "No animated channels on the selection.")
            return {'CANCELLED'}
        done = moved = 0
        worst = 0.0
        loops = 0
        for fc in fcurves:
            res = _inertia_fit_fcurve(fc, self)
            if res is None:
                continue
            done += 1
            moved += res[0]
            worst = max(worst, res[1])
        if not done:
            self.report({'WARNING'}, "Curves need at least 3 keys.")
            return {'CANCELLED'}
        self.report({'INFO'}, f"Inertia on {done} curve(s), {moved} keys adjusted "
                              f"(max deviation from sim {worst:.4f}).")
        return {'FINISHED'}


classes = [ARANTOOLS_OT_InertiaFix]


def register():
    for c in classes:
        bpy.utils.register_class(c)


def unregister():
    for c in reversed(classes):
        bpy.utils.unregister_class(c)
