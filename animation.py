import bpy
import blf
import math
import random
import re
from bpy.types import Operator


_NOISE_NAME = "Aran_Noise"

# ============================================================================
# Animation Organization — helpers, properties, operators, list, timer
# ============================================================================

_DURATION_PATTERN = re.compile(r'_(\d+)$')

# Matches the bone name in an fcurve data_path like pose.bones["Spine"].location
_BONE_NAME_PATTERN = re.compile(r'pose\.bones\["([^"]+)"\]')

# Per-armature memory of the last seen active-action name so the background
# timer only re-applies the timeline length when it actually changes.
_animorg_last_action = {}


def _parse_duration(action_name):
    """Return integer duration parsed from trailing '_NNN', or None."""
    if not action_name:
        return None
    m = _DURATION_PATTERN.search(action_name)
    return int(m.group(1)) if m else None


def _iter_action_fcurves(action):
    """Yield every F-curve of an action, for both legacy and slotted
    (Blender 4.4+ layered) actions.

    Legacy actions expose `action.fcurves` directly. Layered actions removed
    that attribute — their curves live in
    action.layers[].strips[].channelbags[].fcurves."""
    fcurves = getattr(action, 'fcurves', None)
    if fcurves is not None:
        try:
            for fc in fcurves:
                yield fc
            return
        except (TypeError, AttributeError):
            pass
    for layer in getattr(action, 'layers', []):
        for strip in getattr(layer, 'strips', []):
            for cbag in getattr(strip, 'channelbags', []):
                for fc in getattr(cbag, 'fcurves', []):
                    yield fc


def _find_action_fcurve(action, data_path, index=0):
    fcurves = getattr(action, 'fcurves', None)
    if fcurves is not None:
        try:
            return fcurves.find(data_path, index=index)
        except AttributeError:
            pass
    for fc in _iter_action_fcurves(action):
        if fc.data_path == data_path and fc.array_index == index:
            return fc
    return None


def _is_armature_action(action):
    """True if any fcurve targets a pose bone."""
    for fc in _iter_action_fcurves(action):
        if fc.data_path.startswith('pose.bones['):
            return True
    return False


def _armature_used_actions(arm):
    """Set of actions the armature currently references (active + NLA strips)."""
    used = set()
    ad = arm.animation_data
    if ad is not None:
        if ad.action is not None:
            used.add(ad.action)
        for track in ad.nla_tracks:
            for strip in track.strips:
                if strip.action is not None:
                    used.add(strip.action)
    return used


def _action_belongs_to_armature(action, bone_names, used):
    """Heuristic ownership test used by the 'purge foreign actions' button.

    An action is considered to belong to the selected armature when:
      • it's currently used by the armature (active action or an NLA strip), OR
      • it's an empty placeholder (no F-curves — e.g. just created here), OR
      • every pose-bone it animates exists in this armature.

    Actions that animate a bone NOT present in the armature, or that animate
    no pose bones at all (object/mesh/etc. actions), are treated as foreign.
    Two rigs that share identical bone names cannot be told apart.
    """
    if action in used:
        return True
    bone_refs = []
    has_any_fcurve = False
    for fc in _iter_action_fcurves(action):
        has_any_fcurve = True
        m = _BONE_NAME_PATTERN.match(fc.data_path)
        if m:
            bone_refs.append(m.group(1))
    if not has_any_fcurve:
        return True
    if not bone_refs:
        return False
    return all(n in bone_names for n in bone_refs)


def _apply_duration_to_timeline(scene, duration):
    if duration is None or duration < 1:
        return
    props = getattr(scene, 'arantools_anim_org', None)
    start = props.start_frame if props is not None else 0
    scene.frame_start = start
    scene.frame_end = max(start, duration)


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
        scale             = scene.arantools_location_scale * 10.0,
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
                fc = _find_action_fcurve(
                    armature.animation_data.action,
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
            fc = _find_action_fcurve(
                armature.animation_data.action,
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

class ARANTOOLS_ImportActionItem(bpy.types.PropertyGroup):
    action_name: bpy.props.StringProperty(name="Action Name")
    status: bpy.props.EnumProperty(
        name="Status",
        items=[
            ('NEW', "New", "Action does not exist in current file", 'ADD', 1),
            ('MODIFIED', "Modified", "Action exists but differs", 'MODIFIER', 2),
            ('UNCHANGED', "Unchanged", "Action is identical", 'CHECKMARK', 3),
        ]
    )
    diff_info: bpy.props.StringProperty(name="Diff Info")
    do_import: bpy.props.BoolProperty(default=True, name="", description="Import this action")
    temp_action: bpy.props.PointerProperty(type=bpy.types.Action)


class ARANTOOLS_AnimOrg_Props(bpy.types.PropertyGroup):
    armature: bpy.props.PointerProperty(
        name="Armature",
        description="Armature whose actions you want to organize",
        type=bpy.types.Object,
        poll=_poll_armature,
    )
    new_action_basename: bpy.props.StringProperty(
        name="Name",
        description="Base name for the new action. The duration is appended "
                    "automatically as '_NNN' when you click Create & Activate",
        default="NewAction",
    )
    new_action_duration: bpy.props.IntProperty(
        name="Duration",
        description="Length of the new action in frames. Appended to the name "
                    "as '_NNN' and used as the timeline end frame",
        default=100, min=1, max=100000,
    )
    start_frame: bpy.props.IntProperty(
        name="Start Frame",
        description="Frame the timeline starts on when a duration is applied "
                    "(on create, activate, or auto-sync). The '_NNN' suffix is "
                    "the end frame",
        default=0, min=0, max=100000,
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

    # ── Import Animations ──
    import_filepath: bpy.props.StringProperty(
        name="Import File",
        description="Path to the .blend file containing animations",
        subtype='FILE_PATH',
        default="//"
    )
    imported_actions: bpy.props.CollectionProperty(type=ARANTOOLS_ImportActionItem)
    import_active_index: bpy.props.IntProperty(default=0)
    show_import_panel: bpy.props.BoolProperty(
        name="Import Animations",
        description="Show the animation import panel",
        default=False,
    )

    # ── Export Settings ──
    export_folder: bpy.props.StringProperty(
        name="Export Folder",
        description="Destination directory for exported actions",
        subtype='DIR_PATH',
        default="//"
    )
    export_prefix_str: bpy.props.StringProperty(
        name="Prefix",
        description="String to prepend to the exported action filename",
        default=""
    )
    export_suffix_str: bpy.props.StringProperty(
        name="Suffix",
        description="String to append to the exported action filename",
        default=""
    )
    export_remove_str: bpy.props.StringProperty(
        name="Remove",
        description="Comma-separated list of strings to remove from the filename",
        default=""
    )
    show_export_settings: bpy.props.BoolProperty(
        name="Export Settings",
        description="Show Auto-Rig Pro individual action export settings",
        default=False,
    )

    # ── Viewport overlay: show the active action name big in the 3D view ──
    show_visualization: bpy.props.BoolProperty(
        name="Visualization",
        description="Show the viewport-overlay visualization settings",
        default=False,
    )
    show_action_overlay: bpy.props.BoolProperty(
        name="Show Action in Viewport",
        description="Draw the armature's active action name as a large overlay "
                    "across the top of the 3D viewport",
        default=True,
        update=lambda self, ctx: _animorg_tag_redraw(ctx),
    )
    overlay_text_size: bpy.props.IntProperty(
        name="Overlay Size",
        description="Font size of the action-name overlay (pixels)",
        default=36, min=10, max=200,
        update=lambda self, ctx: _animorg_tag_redraw(ctx),
    )
    overlay_color: bpy.props.FloatVectorProperty(
        name="Overlay Color",
        description="Color of the action-name overlay text",
        subtype='COLOR', size=4,
        default=(1.0, 1.0, 1.0, 0.9), min=0.0, max=1.0,
        update=lambda self, ctx: _animorg_tag_redraw(ctx),
    )


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
        op = row.operator("arantools.animorg_export_action_arp", text="", icon='EXPORT')
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
            arm = props.armature
            used = _armature_used_actions(arm) if arm is not None else set()
            for i, action in enumerate(actions):
                # Keep: real armature actions, this armature's active/NLA
                # actions, and empty placeholders (freshly created, not yet
                # keyed — these have no pose-bone F-curves to detect).
                keep = (_is_armature_action(action)
                        or action in used
                        or not any(True for _ in _iter_action_fcurves(action)))
                if not keep:
                    flt_flags[i] = 0

        flt_neworder = helper.sort_items_by_name(actions, "name")
        return flt_flags, flt_neworder


class ARANTOOLS_UL_AnimOrg_ImportedActions(bpy.types.UIList):
    def draw_item(self, context, layout, data, item, icon, active_data, active_propname, index):
        if self.layout_type in {'DEFAULT', 'COMPACT'}:
            row = layout.row(align=True)
            
            # Use smaller font/spacing if needed, but row is fine
            row.prop(item, "do_import", text="")
            
            icon_str = 'NONE'
            if item.status == 'NEW':
                icon_str = 'ADD'
            elif item.status == 'MODIFIED':
                icon_str = 'MODIFIER'
            elif item.status == 'UNCHANGED':
                icon_str = 'CHECKMARK'
                
            # Split the row to align columns nicely
            split = row.split(factor=0.6)
            split.label(text=item.action_name, icon=icon_str)
            
            # Right side status text
            split.label(text=item.status.capitalize())
        elif self.layout_type in {'GRID'}:
            layout.alignment = 'CENTER'
            layout.label(text="", icon_value=icon)


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

        base = props.new_action_basename.strip().rstrip('_')
        if not base:
            self.report({'ERROR'}, "Action name cannot be empty.")
            return {'CANCELLED'}
        name = f"{base}_{props.new_action_duration}"

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


class ARANTOOLS_OT_AnimOrg_ExportAction_ARP(Operator):
    """Export this action as a single FBX using Auto-Rig Pro"""
    bl_idname = "arantools.animorg_export_action_arp"
    bl_label = "Export Action (ARP)"
    bl_options = {'REGISTER'}

    action_name: bpy.props.StringProperty()

    @classmethod
    def description(cls, context, properties):
        return f"Export action '{properties.action_name}' via Auto-Rig Pro"

    @classmethod
    def poll(cls, context):
        has_arp = hasattr(bpy.types, "ARP_OT_export_fbx_panel") or \
                  "arp_export_fbx_panel" in dir(getattr(bpy.ops, "arp", object()))
        return has_arp and context.scene.arantools_anim_org.armature is not None

    def execute(self, context):
        props = context.scene.arantools_anim_org
        arm = props.armature
        action = bpy.data.actions.get(self.action_name)
        
        if not action or not arm:
            return {'CANCELLED'}

        import os
        from .export import _arp_set, _arp_get, _format_name
        
        # Ensure directory
        folder = bpy.path.abspath(props.export_folder)
        if not os.path.exists(folder):
            try:
                os.makedirs(folder, exist_ok=True)
            except Exception as e:
                self.report({'ERROR'}, f"Could not create folder: {e}")
                return {'CANCELLED'}
        
        # Determine filename
        filename = _format_name(self.action_name, props.export_remove_str, props.export_prefix_str, props.export_suffix_str)
        if not filename.strip():
            self.report({'ERROR'}, "Resulting filename is empty. Check prefix/suffix/remove settings.")
            return {'CANCELLED'}
            
        filepath = os.path.join(folder, f"{filename}.fbx")

        # Save current state
        prev_action = arm.animation_data.action if arm.animation_data else None
        
        # Set to target action
        _assign_action(arm, action)
        
        # Ensure arm is active and selected
        prev_active = context.view_layer.objects.active
        prev_selected = context.selected_objects.copy()
        
        bpy.ops.object.select_all(action='DESELECT')
        arm.select_set(True)
        context.view_layer.objects.active = arm

        # Save ARP settings
        scene = context.scene
        prev_sel_only     = _arp_get(scene, 'arp_ge_sel_only', None)
        prev_bake_anim    = _arp_get(scene, 'arp_bake_anim',   None)
        prev_separate_fbx = _arp_get(scene, 'arp_export_separate_fbx', None)
        prev_only_active  = _arp_get(scene, 'arp_bake_only_active', None)

        # Apply temporary ARP settings
        _arp_set(scene, 'arp_ge_sel_only', True)
        _arp_set(scene, 'arp_bake_anim', True)
        _arp_set(scene, 'arp_export_separate_fbx', False)
        _arp_set(scene, 'arp_bake_only_active', True)

        # Export
        try:
            bpy.ops.arp.arp_export_fbx_panel(filepath=filepath)
            self.report({'INFO'}, f"Exported: {filename}.fbx")
        except Exception as e:
            self.report({'ERROR'}, f"Failed to export: {e}")
            print(f"ARP Export Error: {e}")
        finally:
            # Restore ARP
            if prev_sel_only     is not None: _arp_set(scene, 'arp_ge_sel_only', prev_sel_only)
            if prev_bake_anim    is not None: _arp_set(scene, 'arp_bake_anim', prev_bake_anim)
            if prev_separate_fbx is not None: _arp_set(scene, 'arp_export_separate_fbx', prev_separate_fbx)
            if prev_only_active  is not None: _arp_set(scene, 'arp_bake_only_active', prev_only_active)
            
            # Restore selection
            bpy.ops.object.select_all(action='DESELECT')
            for obj in prev_selected:
                try:
                    obj.select_set(True)
                except ReferenceError:
                    pass
            if prev_active:
                try:
                    context.view_layer.objects.active = prev_active
                except ReferenceError:
                    pass
            
            # Restore action
            if prev_action:
                _assign_action(arm, prev_action)
            elif arm.animation_data:
                arm.animation_data.action = None

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


class ARANTOOLS_OT_AnimOrg_PurgeForeign(Operator):
    """Delete every action in this .blend file that does NOT belong to the
selected armature. Keeps the armature's own actions (and empty placeholders);
removes actions that animate bones absent from this armature or that don't
animate any bone. This is permanent."""
    bl_idname = "arantools.animorg_purge_foreign_actions"
    bl_label = "Remove Foreign Actions"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return context.scene.arantools_anim_org.armature is not None

    def _foreign(self, context):
        """Return (keep, remove) lists of actions for the selected armature."""
        props = context.scene.arantools_anim_org
        arm = props.armature
        bone_names = {b.name for b in arm.data.bones}
        used = _armature_used_actions(arm)
        keep, remove = [], []
        for action in bpy.data.actions:
            if _action_belongs_to_armature(action, bone_names, used):
                keep.append(action)
            else:
                remove.append(action)
        return keep, remove

    def invoke(self, context, event):
        _, remove = self._foreign(context)
        if not remove:
            self.report({'INFO'}, "No foreign actions to remove.")
            return {'CANCELLED'}
        return context.window_manager.invoke_props_dialog(self, width=320)

    def draw(self, context):
        layout = self.layout
        arm = context.scene.arantools_anim_org.armature
        _, remove = self._foreign(context)
        layout.label(text=f"Delete {len(remove)} action(s) not from "
                          f"'{arm.name}'?", icon='TRASH')
        col = layout.column(align=True)
        for action in remove[:12]:
            col.label(text=action.name, icon='ACTION')
        if len(remove) > 12:
            col.label(text=f"… and {len(remove) - 12} more")
        layout.label(text="This permanently removes them from the file.",
                     icon='ERROR')

    def execute(self, context):
        _, remove = self._foreign(context)
        if not remove:
            self.report({'INFO'}, "No foreign actions to remove.")
            return {'CANCELLED'}
        names = [a.name for a in remove]
        for action in names:
            a = bpy.data.actions.get(action)
            if a is not None:
                bpy.data.actions.remove(a)
        self.report({'INFO'}, f"Removed {len(names)} foreign action(s).")
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
        self.report({'INFO'},
                    f"Timeline set to {props.start_frame}–{duration}.")
        return {'FINISHED'}


def _compare_actions(action_a, action_b):
    if action_a is None or action_b is None:
        return False
    
    fcurves_a = list(_iter_action_fcurves(action_a))
    fcurves_b = list(_iter_action_fcurves(action_b))
    
    if len(fcurves_a) != len(fcurves_b):
        return False
    
    # Sort fcurves to ensure matching pairs
    fcurves_a.sort(key=lambda f: (f.data_path, f.array_index))
    fcurves_b.sort(key=lambda f: (f.data_path, f.array_index))
    
    for fc_a, fc_b in zip(fcurves_a, fcurves_b):
        if fc_a.data_path != fc_b.data_path or fc_a.array_index != fc_b.array_index:
            return False
        if len(fc_a.keyframe_points) != len(fc_b.keyframe_points):
            return False
        for kp_a, kp_b in zip(fc_a.keyframe_points, fc_b.keyframe_points):
            # Precision tolerance for floating point comparison
            if abs(kp_a.co.x - kp_b.co.x) > 1e-4 or abs(kp_a.co.y - kp_b.co.y) > 1e-4:
                return False
    return True


class ARANTOOLS_OT_AnimOrg_LoadExternal(bpy.types.Operator):
    bl_idname = "arantools.animorg_load_external"
    bl_label = "Load & Compare Actions"
    bl_description = "Loads actions from the selected file and compares them to local actions"

    @classmethod
    def poll(cls, context):
        return True

    def execute(self, context):
        props = context.scene.arantools_anim_org
        if not props.import_filepath:
            self.report({'ERROR'}, "No file selected.")
            return {'CANCELLED'}
        
        filepath = bpy.path.abspath(props.import_filepath)
        import os
        if not os.path.exists(filepath):
            self.report({'ERROR'}, "File not found.")
            return {'CANCELLED'}

        # Cancel any previous import to clean up temp actions
        bpy.ops.arantools.animorg_cancel_import()

        try:
            with bpy.data.libraries.load(filepath, link=False) as (data_from, data_to):
                original_action_names = list(data_from.actions)
                data_to.actions = data_from.actions
        except OSError:
            self.report({'ERROR'}, "Failed to read blend file.")
            return {'CANCELLED'}

        for i, appended_act in enumerate(data_to.actions):
            if appended_act is None:
                continue
            
            original_name = original_action_names[i]
            # prefix with a dot so Blender hides it from the UI lists!
            appended_act.name = "._temp_import_" + original_name
            appended_act.use_fake_user = True 
            
            item = props.imported_actions.add()
            item.action_name = original_name
            item.temp_action = appended_act
            
            local_act = bpy.data.actions.get(original_name)
            if local_act is None:
                item.status = 'NEW'
                item.diff_info = "Action is new"
                item.do_import = True
            else:
                if _compare_actions(local_act, appended_act):
                    item.status = 'UNCHANGED'
                    item.diff_info = "Identical to local"
                    item.do_import = False
                else:
                    item.status = 'MODIFIED'
                    item.diff_info = "Keyframes differ"
                    item.do_import = True
                    
        self.report({'INFO'}, f"Loaded {len(data_to.actions)} actions for review.")
        return {'FINISHED'}


from bpy_extras.io_utils import ImportHelper
from bpy.props import StringProperty

class ARANTOOLS_OT_AnimOrg_ImportDialog(bpy.types.Operator, ImportHelper):
    """Select a .blend file to import and review animations from"""
    bl_idname = "arantools.animorg_import_dialog"
    bl_label = "Import Animations"
    
    filter_glob: StringProperty(
        default="*.blend",
        options={'HIDDEN'},
    )

    def execute(self, context):
        props = context.scene.arantools_anim_org
        props.import_filepath = self.filepath
        props.show_import_panel = True
        
        bpy.ops.arantools.animorg_load_external()
        return {'FINISHED'}




class ARANTOOLS_OT_AnimOrg_ApplyImport(bpy.types.Operator):
    bl_idname = "arantools.animorg_apply_import"
    bl_label = "Apply Import"
    bl_description = "Apply the selected actions and clean up"

    @classmethod
    def poll(cls, context):
        return len(context.scene.arantools_anim_org.imported_actions) > 0

    def execute(self, context):
        props = context.scene.arantools_anim_org
        
        imported = 0
        for item in list(props.imported_actions):
            if not item.temp_action:
                continue
            
            if item.do_import and item.status in {'NEW', 'MODIFIED'}:
                local_act = bpy.data.actions.get(item.action_name)
                temp_act = item.temp_action
                
                if local_act:
                    # Remove the local one completely
                    local_act.user_clear()
                    bpy.data.actions.remove(local_act)
                
                # Now rename temp to the original name
                temp_act.name = item.action_name
                temp_act.use_fake_user = True
                imported += 1
            else:
                # Discard temp action
                item.temp_action.user_clear()
                bpy.data.actions.remove(item.temp_action)
                
        props.imported_actions.clear()
        self.report({'INFO'}, f"Successfully imported {imported} actions.")
        
        # Trigger an update of the viewport or dependencies
        context.view_layer.update()
        return {'FINISHED'}


class ARANTOOLS_OT_AnimOrg_CancelImport(bpy.types.Operator):
    bl_idname = "arantools.animorg_cancel_import"
    bl_label = "Cancel / Clear"
    bl_description = "Cancel import and remove temporary actions"

    def execute(self, context):
        props = context.scene.arantools_anim_org
        
        for item in props.imported_actions:
            if item.temp_action:
                item.temp_action.user_clear()
                bpy.data.actions.remove(item.temp_action)
                
        # Also clean up any orphan ._temp_import_ actions
        for act in list(bpy.data.actions):
            if act.name.startswith("._temp_import_"):
                act.user_clear()
                bpy.data.actions.remove(act)
                
        props.imported_actions.clear()
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


# ── Viewport overlay: draw the active action name big in the 3D view ───────
# The handle is stashed in driver_namespace (not a module global) so the
# Reload Addon button — which re-executes this module before unregister()
# runs — can still find and remove a stale handler instead of leaking it.
_OVERLAY_NS_KEY = "arantools_animorg_overlay_handle"


def _animorg_tag_redraw(context):
    """Force every 3D viewport to redraw (so the overlay updates instantly)."""
    wm = getattr(context, 'window_manager', None) or bpy.context.window_manager
    if wm is None:
        return
    for window in wm.windows:
        for area in window.screen.areas:
            if area.type == 'VIEW_3D':
                area.tag_redraw()


def _animorg_draw_overlay():
    """POST_PIXEL draw callback for the 3D viewport action-name overlay."""
    try:
        context = bpy.context
        scene = context.scene
        props = getattr(scene, 'arantools_anim_org', None)
        if props is None or not props.show_action_overlay:
            return

        arm = props.armature
        # Fall back to the active object if no armature is pinned in the panel.
        if arm is None:
            obj = context.active_object
            if obj is not None and obj.animation_data is not None:
                arm = obj
        if (arm is None or arm.animation_data is None
                or arm.animation_data.action is None):
            return

        action = arm.animation_data.action
        name = action.name
        sub = f"frame {scene.frame_current}  /  {scene.frame_end}"

        region = context.region
        if region is None:
            return

        font_id = 0
        size = props.overlay_text_size

        # blf.size dropped its DPI arg in Blender 4.0 — support both.
        try:
            blf.size(font_id, size)
        except TypeError:
            blf.size(font_id, size, 72)

        blf.enable(font_id, blf.SHADOW)
        blf.shadow(font_id, 5, 0.0, 0.0, 0.0, 1.0)
        blf.shadow_offset(font_id, 2, -2)

        # ── Title (action name), centered near the top ──
        blf.color(font_id, *props.overlay_color)
        tw, th = blf.dimensions(font_id, name)
        x = max(10.0, (region.width - tw) / 2.0)
        y = region.height - th - 40.0
        blf.position(font_id, x, y, 0.0)
        blf.draw(font_id, name)

        # ── Subtitle (frame range), smaller, just below ──
        sub_size = max(10, int(size * 0.45))
        try:
            blf.size(font_id, sub_size)
        except TypeError:
            blf.size(font_id, sub_size, 72)
        r, g, b, a = props.overlay_color
        blf.color(font_id, r, g, b, a * 0.8)
        sw, sh = blf.dimensions(font_id, sub)
        blf.position(font_id, max(10.0, (region.width - sw) / 2.0),
                     y - sh - 6.0, 0.0)
        blf.draw(font_id, sub)

        blf.disable(font_id, blf.SHADOW)
    except Exception as e:
        print(f"[AranTools animorg overlay] {e}")


def _animorg_overlay_unregister():
    ns = bpy.app.driver_namespace
    old = ns.get(_OVERLAY_NS_KEY)
    if old is not None:
        try:
            bpy.types.SpaceView3D.draw_handler_remove(old, 'WINDOW')
        except Exception:
            pass
        ns[_OVERLAY_NS_KEY] = None


def _animorg_overlay_register():
    # Always clear a possibly-stale handler first (survives module reload).
    _animorg_overlay_unregister()
    ns = bpy.app.driver_namespace
    ns[_OVERLAY_NS_KEY] = bpy.types.SpaceView3D.draw_handler_add(
        _animorg_draw_overlay, (), 'WINDOW', 'POST_PIXEL')


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
    for fc in _iter_action_fcurves(obj.animation_data.action):
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
    for fc in _iter_action_fcurves(action):
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
            for fc in _iter_action_fcurves(action):
                if not fc.data_path.startswith(prefix):
                    continue
                for mod in list(fc.modifiers):
                    if mod.name == _NOISE_NAME:
                        fc.modifiers.remove(mod)
                        removed += 1
        self.report({'INFO'}, f"Removed {removed} noise modifier(s).")
        return {'FINISHED'}


class ARANTOOLS_OT_Apply_Cycles(Operator):
    """Add a Cycles modifier at the top of the modifier stack for every F-curve belonging
to the selected pose bones."""
    bl_idname  = "arantools.apply_cycles"
    bl_label   = "Add Cycles Before Noise"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return (context.mode == 'POSE'
                and context.active_object
                and context.active_object.animation_data
                and context.active_object.animation_data.action)

    def execute(self, context):
        action = context.active_object.animation_data.action
        added = 0
        for bone in context.selected_pose_bones:
            prefix = f'pose.bones["{bone.name}"]'
            for fc in _iter_action_fcurves(action):
                if not fc.data_path.startswith(prefix):
                    continue
                
                if fc.modifiers and fc.modifiers[0].type == 'CYCLES':
                    continue
                
                mod_cache = []
                for mod in list(fc.modifiers):
                    if mod.type == 'CYCLES': 
                        fc.modifiers.remove(mod)
                        continue
                    
                    props = {}
                    for k in dir(mod):
                        if k.startswith('_') or k in ('rna_type', 'type', 'is_valid', 'bl_rna'): continue
                        try:
                            props[k] = getattr(mod, k)
                        except Exception:
                            pass
                    
                    mod_type = mod.type
                    fc.modifiers.remove(mod)
                    mod_cache.append((mod_type, props))
                
                fc.modifiers.new(type='CYCLES')
                added += 1
                
                for m_type, props in mod_cache:
                    new_m = fc.modifiers.new(type=m_type)
                    for k, v in props.items():
                        try:
                            if k == 'coefficients' and m_type == 'GENERATOR':
                                for i in range(min(len(new_m.coefficients), len(v))):
                                    new_m.coefficients[i] = v[i]
                            else:
                                setattr(new_m, k, v)
                        except Exception:
                            pass
                            
        self.report({'INFO'}, f"Added {added} Cycles modifiers at the top.")
        return {'FINISHED'}


class ARANTOOLS_OT_FixLoop(Operator):
    """Make the last keyframe of all channels match the first keyframe's value,
and move the last keyframe to (Last Frame + 1)."""
    bl_idname = "arantools.fix_loop"
    bl_label = "Fix Loop"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return (context.mode == 'POSE'
                and context.active_object
                and context.active_object.animation_data
                and context.active_object.animation_data.action)

    def execute(self, context):
        action = context.active_object.animation_data.action
        scene = context.scene
        
        duration = scene.arantools_fix_loop_duration
        
        fixed_count = 0
        
        selected_prefixes = [f'pose.bones["{b.name}"]' for b in context.selected_pose_bones]
        
        for fc in _iter_action_fcurves(action):
            if not any(fc.data_path.startswith(p) for p in selected_prefixes):
                continue
            
            kpts = fc.keyframe_points
            if len(kpts) < 2:
                continue
            
            first_key = kpts[0]
            last_key = kpts[-1]
            
            val = first_key.co.y
            
            target_frame = first_key.co.x + duration
            dx = target_frame - last_key.co.x
            dy = val - last_key.co.y
            
            last_key.co.x += dx
            last_key.co.y += dy
            last_key.handle_left.x += dx
            last_key.handle_left.y += dy
            last_key.handle_right.x += dx
            last_key.handle_right.y += dy
            
            fixed_count += 1
            
        action.update_tag()
        self.report({'INFO'}, f"Fixed loop on {fixed_count} F-Curves.")
        return {'FINISHED'}


# ============================================================================
# Registration
# ============================================================================

classes = [
    ARANTOOLS_ImportActionItem,
    ARANTOOLS_AnimOrg_Props,
    ARANTOOLS_UL_AnimOrg_Actions,
    ARANTOOLS_UL_AnimOrg_ImportedActions,
    ARANTOOLS_OT_AnimOrg_NewAction,
    ARANTOOLS_OT_AnimOrg_SetActive,
    ARANTOOLS_OT_AnimOrg_Delete,
    ARANTOOLS_OT_AnimOrg_ExportAction_ARP,
    ARANTOOLS_OT_AnimOrg_PurgeForeign,
    ARANTOOLS_OT_AnimOrg_SyncTimeline,
    ARANTOOLS_OT_AnimOrg_LoadExternal,
    ARANTOOLS_OT_AnimOrg_ImportDialog,
    ARANTOOLS_OT_AnimOrg_ApplyImport,
    ARANTOOLS_OT_AnimOrg_CancelImport,
    ARANTOOLS_CurveSmooth_Props,
    ARANTOOLS_OT_SpringSmoothCurves,
    ARANTOOLS_OT_DecimateCurves,
    ARANTOOLS_OT_Apply_Noise_Rotation,
    ARANTOOLS_OT_Apply_Noise_Location,
    ARANTOOLS_OT_Apply_Noise_Both,
    ARANTOOLS_OT_Remove_Noise,
    ARANTOOLS_OT_Apply_Cycles,
    ARANTOOLS_OT_FixLoop,
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

    _animorg_overlay_register()

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

    bpy.types.Scene.arantools_fix_loop_duration = bpy.props.FloatProperty(
        name='Loop Duration',
        description='Duration of the loop (distance between first and last keyframe)',
        default=24.0)


def unregister():
    _animorg_overlay_unregister()
    if bpy.app.timers.is_registered(_animorg_timer):
        bpy.app.timers.unregister(_animorg_timer)
    del bpy.types.Scene.arantools_curve_smooth
    del bpy.types.Scene.arantools_anim_org

    del bpy.types.Scene.arantools_advanced_options
    del bpy.types.Scene.arantools_fix_loop_duration
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
