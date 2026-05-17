import bpy
import os
from bpy.types import Operator


def _format_name(original, remove_str, prefix, suffix):
    name = original
    if remove_str:
        for token in [t.strip() for t in remove_str.split(',')]:
            if token:
                name = name.replace(token, "")
    return bpy.path.clean_name(f"{prefix}{name}{suffix}")


def _get_final_filename(original_name, props):
    return _format_name(original_name, props.remove_str,
                        props.prefix_str, props.suffix_str)


def _get_anim_filename(action_name, props):
    return _format_name(action_name, props.anim_remove_str,
                        props.anim_prefix_str, props.anim_suffix_str)


def _iter_armature_actions(armature):
    """Yield every action in the file whose F-curves animate pose bones —
    a heuristic for 'actions that belong to an armature' since Blender
    doesn't track which armature an action was created for."""
    if armature is None:
        return
    for action in bpy.data.actions:
        for fc in action.fcurves:
            if fc.data_path.startswith('pose.bones['):
                yield action
                break


def _arp_set(scene, prop, value):
    """Set an ARP scene property if it exists."""
    if hasattr(scene, prop):
        setattr(scene, prop, value)


def _arp_get(scene, prop, default=None):
    """Get an ARP scene property, returning default if it doesn't exist."""
    return getattr(scene, prop, default)


# ============================================================================
# Export Sets — group meshes into named bundles, one FBX per set
# ============================================================================

def _clean_relpath(raw):
    """Sanitize a user-entered filename, allowing forward-slash subfolders.
    'Char/Body' → 'Char/Body' (each segment passed through clean_name)."""
    raw = raw.strip().replace('\\', '/').strip('/')
    parts = [bpy.path.clean_name(p.strip()) for p in raw.split('/') if p.strip()]
    return '/'.join(parts)


class ARANTOOLS_PG_ExportSetMesh(bpy.types.PropertyGroup):
    obj: bpy.props.PointerProperty(
        name="Mesh",
        type=bpy.types.Object,
        poll=lambda self, obj: obj.type == 'MESH',
    )


class ARANTOOLS_PG_ExportSet(bpy.types.PropertyGroup):
    filename: bpy.props.StringProperty(
        name="Filename",
        description="Output filename without the .fbx extension. Forward slashes "
                    "make subfolders inside the export folder (e.g. 'Char/Body')",
        default="ExportSet",
    )
    meshes: bpy.props.CollectionProperty(type=ARANTOOLS_PG_ExportSetMesh)
    expanded: bpy.props.BoolProperty(default=True)


def _do_export_set(context, eset, folder, armature):
    """Select the set's meshes + armature and run ARP export to the set's
    filepath. Overwrites any existing file. Returns True on success."""
    meshes = [e.obj for e in eset.meshes if e.obj is not None]
    if not meshes:
        print(f"Skipping set '{eset.filename}': no meshes.")
        return False
    relpath = _clean_relpath(eset.filename)
    if not relpath:
        print(f"Skipping set '{eset.filename}': empty filename.")
        return False

    filepath = os.path.join(folder, f"{relpath}.fbx")
    os.makedirs(os.path.dirname(filepath), exist_ok=True)

    bpy.ops.object.select_all(action='DESELECT')
    for m in meshes:
        m.select_set(True)
    armature.select_set(True)
    context.view_layer.objects.active = armature

    print(f"Exporting set → {relpath}.fbx  ({len(meshes)} mesh(es))")
    bpy.ops.arp.arp_export_fbx_panel(filepath=filepath)
    return True


class ARANTOOLS_PG_ARPExport(bpy.types.PropertyGroup):
    target_armature: bpy.props.PointerProperty(
        type=bpy.types.Object,
        name="Armature",
        description="The Auto-Rig Pro armature to export with",
        poll=lambda self, obj: obj.type == 'ARMATURE'
    )
    export_folder: bpy.props.StringProperty(
        name="Export Folder",
        description="Folder where FBX files will be saved",
        default="//",
        subtype='DIR_PATH'
    )
    remove_str: bpy.props.StringProperty(
        name="Remove Text",
        description="Text to strip from filenames. Separate multiple with commas.",
        default=""
    )
    prefix_str: bpy.props.StringProperty(name="Prefix", default="")
    suffix_str: bpy.props.StringProperty(name="Suffix", default="")

    # ── Animation-specific naming (independent of mesh naming) ──────────
    anim_remove_str: bpy.props.StringProperty(
        name="Remove Text",
        description="Text to strip from action names before exporting. "
                    "Separate multiple tokens with commas",
        default="",
    )
    anim_prefix_str: bpy.props.StringProperty(
        name="Prefix",
        description="Text prepended to each exported animation FBX filename",
        default="A_",
    )
    anim_suffix_str: bpy.props.StringProperty(
        name="Suffix",
        description="Text appended to each exported animation FBX filename",
        default="",
    )
    anim_only_active: bpy.props.BoolProperty(
        name="Only Active Action",
        description="Export only the armature's currently active action instead "
                    "of every action whose F-curves animate pose bones",
        default=False,
    )
    export_sets: bpy.props.CollectionProperty(type=ARANTOOLS_PG_ExportSet)


class ARANTOOLS_OT_ARPBatchExport(Operator):
    """Batch export all selected meshes as FBX using Auto-Rig Pro.
Sets 'Selected Objects Only' and disables 'Bake Animations' automatically."""
    bl_idname = "arantools.arp_batch_export"
    bl_label = "Export Meshes"

    def execute(self, context):
        props = context.scene.arantools_arp_export
        scene = context.scene
        armature = props.target_armature
        folder_path = bpy.path.abspath(props.export_folder)

        if not armature:
            self.report({'ERROR'}, "Select the Armature in the panel first.")
            return {'CANCELLED'}
        if not os.path.exists(folder_path):
            self.report({'ERROR'}, f"Export folder does not exist: {folder_path}")
            return {'CANCELLED'}

        objects_to_process = [
            obj for obj in context.selected_objects
            if obj != armature and obj.type == 'MESH'
        ]
        if not objects_to_process:
            self.report({'WARNING'}, "No mesh objects selected.")
            return {'CANCELLED'}

        # ── Store and apply required ARP settings ─────────────────────────
        prev_sel_only   = _arp_get(scene, 'arp_ge_sel_only',   None)
        prev_bake_anim  = _arp_get(scene, 'arp_bake_anim',     None)

        _arp_set(scene, 'arp_ge_sel_only',  True)   # export selected objects only
        _arp_set(scene, 'arp_bake_anim',    False)  # no animation baking for mesh export

        count = 0
        try:
            for obj in objects_to_process:
                try:
                    bpy.ops.object.select_all(action='DESELECT')
                    obj.select_set(True)
                    armature.select_set(True)
                    context.view_layer.objects.active = armature

                    filename = _get_final_filename(obj.name, props)
                    if not filename.strip():
                        print(f"Skipping {obj.name}: resulting filename is empty.")
                        continue

                    filepath = os.path.join(folder_path, f"{filename}.fbx")
                    print(f"Exporting: {obj.name} → {filename}.fbx")
                    bpy.ops.arp.arp_export_fbx_panel(filepath=filepath)
                    count += 1
                except Exception as e:
                    self.report({'ERROR'}, f"Failed on '{obj.name}': {e}")
                    print(f"Error on {obj.name}: {e}")
        finally:
            # ── Restore ARP settings ───────────────────────────────────────
            if prev_sel_only  is not None: _arp_set(scene, 'arp_ge_sel_only',  prev_sel_only)
            if prev_bake_anim is not None: _arp_set(scene, 'arp_bake_anim',    prev_bake_anim)

            bpy.ops.object.select_all(action='DESELECT')
            armature.select_set(True)
            context.view_layer.objects.active = armature

        self.report({'INFO'}, f"Exported {count} mesh file(s) successfully.")
        return {'FINISHED'}


class ARANTOOLS_OT_ARPAnimExport(Operator):
    """Export each of the armature's actions as its own FBX using Auto-Rig Pro.
The action is set active and ARP is told to bake only the active action, so
each call writes one file with the name produced by the Anim Naming rules.
Files go to an 'Animations' subfolder of the export folder."""
    bl_idname = "arantools.arp_anim_export"
    bl_label = "Export Animations"

    def execute(self, context):
        props = context.scene.arantools_arp_export
        scene = context.scene
        armature = props.target_armature
        folder_path = bpy.path.abspath(props.export_folder)

        if not armature:
            self.report({'ERROR'}, "Select the Armature in the panel first.")
            return {'CANCELLED'}
        if not os.path.exists(folder_path):
            self.report({'ERROR'}, f"Export folder does not exist: {folder_path}")
            return {'CANCELLED'}

        # ── Decide which actions to export ────────────────────────────────
        if props.anim_only_active:
            if (armature.animation_data is None
                    or armature.animation_data.action is None):
                self.report({'ERROR'}, "Armature has no active action.")
                return {'CANCELLED'}
            actions_to_export = [armature.animation_data.action]
        else:
            actions_to_export = list(_iter_armature_actions(armature))

        if not actions_to_export:
            self.report({'WARNING'}, "No armature actions found to export.")
            return {'CANCELLED'}

        anim_folder = os.path.join(folder_path, "Animations")
        os.makedirs(anim_folder, exist_ok=True)

        # ── Store ARP state + the currently-active action ─────────────────
        prev_sel_only     = _arp_get(scene, 'arp_ge_sel_only',         None)
        prev_bake_anim    = _arp_get(scene, 'arp_bake_anim',           None)
        prev_separate_fbx = _arp_get(scene, 'arp_export_separate_fbx', None)
        prev_only_active  = _arp_get(scene, 'arp_bake_only_active',    None)
        prev_action       = (armature.animation_data.action
                             if armature.animation_data else None)

        _arp_set(scene, 'arp_ge_sel_only',         True)
        _arp_set(scene, 'arp_bake_anim',           True)
        _arp_set(scene, 'arp_export_separate_fbx', False)  # one FBX per ARP call
        _arp_set(scene, 'arp_bake_only_active',    True)   # we cycle the active action

        count = 0
        try:
            for action in actions_to_export:
                try:
                    bpy.ops.object.select_all(action='DESELECT')
                    armature.select_set(True)
                    context.view_layer.objects.active = armature

                    if armature.animation_data is None:
                        armature.animation_data_create()
                    armature.animation_data.action = action

                    filename = _get_anim_filename(action.name, props)
                    if not filename.strip():
                        print(f"Skipping action '{action.name}': empty filename.")
                        continue

                    filepath = os.path.join(anim_folder, f"{filename}.fbx")
                    print(f"Exporting animation: {action.name} → {filename}.fbx")
                    bpy.ops.arp.arp_export_fbx_panel(filepath=filepath)
                    count += 1
                except Exception as e:
                    self.report({'ERROR'}, f"Failed on action '{action.name}': {e}")
                    print(f"Error on action {action.name}: {e}")
        finally:
            # ── Restore ARP state + previously-active action ──────────────
            if prev_sel_only     is not None: _arp_set(scene, 'arp_ge_sel_only',         prev_sel_only)
            if prev_bake_anim    is not None: _arp_set(scene, 'arp_bake_anim',           prev_bake_anim)
            if prev_separate_fbx is not None: _arp_set(scene, 'arp_export_separate_fbx', prev_separate_fbx)
            if prev_only_active  is not None: _arp_set(scene, 'arp_bake_only_active',    prev_only_active)
            if armature.animation_data is not None:
                armature.animation_data.action = prev_action

            bpy.ops.object.select_all(action='DESELECT')
            armature.select_set(True)
            context.view_layer.objects.active = armature

        self.report({'INFO'},
                    f"Exported {count} animation file(s) → {anim_folder}")
        return {'FINISHED'}


# ============================================================================
# Export Set operators
# ============================================================================

def _selected_meshes(context, armature):
    return [o for o in context.selected_objects
            if o.type == 'MESH' and o != armature]


class ARANTOOLS_OT_ExpSet_AddFromSelection(Operator):
    """Create a new export set populated with the currently selected meshes.
The filename defaults to the first mesh's name."""
    bl_idname  = "arantools.expset_add_from_selection"
    bl_label   = "Add Set from Selection"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        props = context.scene.arantools_arp_export
        meshes = _selected_meshes(context, props.target_armature)
        if not meshes:
            self.report({'WARNING'}, "Select one or more meshes first.")
            return {'CANCELLED'}
        new_set = props.export_sets.add()
        new_set.filename = meshes[0].name
        for m in meshes:
            entry = new_set.meshes.add()
            entry.obj = m
        return {'FINISHED'}


class ARANTOOLS_OT_ExpSet_Add(Operator):
    """Add a new empty export set."""
    bl_idname  = "arantools.expset_add"
    bl_label   = "Add Empty Set"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        props = context.scene.arantools_arp_export
        new_set = props.export_sets.add()
        new_set.filename = f"ExportSet_{len(props.export_sets):02d}"
        return {'FINISHED'}


class ARANTOOLS_OT_ExpSet_Remove(Operator):
    """Delete this export set."""
    bl_idname  = "arantools.expset_remove"
    bl_label   = "Delete Set"
    bl_options = {'REGISTER', 'UNDO'}

    index: bpy.props.IntProperty()

    def execute(self, context):
        props = context.scene.arantools_arp_export
        if 0 <= self.index < len(props.export_sets):
            props.export_sets.remove(self.index)
        return {'FINISHED'}


class ARANTOOLS_OT_ExpSet_SetMeshes(Operator):
    """Replace this set's meshes with the current viewport selection."""
    bl_idname  = "arantools.expset_set_meshes"
    bl_label   = "Set from Selection"
    bl_options = {'REGISTER', 'UNDO'}

    index: bpy.props.IntProperty()

    def execute(self, context):
        props = context.scene.arantools_arp_export
        if not (0 <= self.index < len(props.export_sets)):
            return {'CANCELLED'}
        eset = props.export_sets[self.index]
        meshes = _selected_meshes(context, props.target_armature)
        eset.meshes.clear()
        for m in meshes:
            entry = eset.meshes.add()
            entry.obj = m
        return {'FINISHED'}


class ARANTOOLS_OT_ExpSet_AddMeshes(Operator):
    """Append the current viewport selection to this set (no duplicates)."""
    bl_idname  = "arantools.expset_add_meshes"
    bl_label   = "Add from Selection"
    bl_options = {'REGISTER', 'UNDO'}

    index: bpy.props.IntProperty()

    def execute(self, context):
        props = context.scene.arantools_arp_export
        if not (0 <= self.index < len(props.export_sets)):
            return {'CANCELLED'}
        eset = props.export_sets[self.index]
        existing = {e.obj.name for e in eset.meshes if e.obj is not None}
        added = 0
        for m in _selected_meshes(context, props.target_armature):
            if m.name in existing:
                continue
            entry = eset.meshes.add()
            entry.obj = m
            added += 1
        self.report({'INFO'}, f"Added {added} mesh(es) to set.")
        return {'FINISHED'}


class ARANTOOLS_OT_ExpSet_Clear(Operator):
    """Remove all meshes from this set."""
    bl_idname  = "arantools.expset_clear"
    bl_label   = "Clear Meshes"
    bl_options = {'REGISTER', 'UNDO'}

    index: bpy.props.IntProperty()

    def execute(self, context):
        props = context.scene.arantools_arp_export
        if 0 <= self.index < len(props.export_sets):
            props.export_sets[self.index].meshes.clear()
        return {'FINISHED'}


class ARANTOOLS_OT_ExpSet_Select(Operator):
    """Select this set's meshes in the viewport (deselects everything else)."""
    bl_idname  = "arantools.expset_select"
    bl_label   = "Select Meshes"
    bl_options = {'REGISTER', 'UNDO'}

    index: bpy.props.IntProperty()

    def execute(self, context):
        props = context.scene.arantools_arp_export
        if not (0 <= self.index < len(props.export_sets)):
            return {'CANCELLED'}
        eset = props.export_sets[self.index]
        bpy.ops.object.select_all(action='DESELECT')
        last = None
        for entry in eset.meshes:
            if entry.obj is not None:
                entry.obj.select_set(True)
                last = entry.obj
        if last is not None:
            context.view_layer.objects.active = last
        return {'FINISHED'}


class ARANTOOLS_OT_ExpSet_ExportOne(Operator):
    """Export only this set to its FBX via Auto-Rig Pro. Overwrites existing file."""
    bl_idname  = "arantools.expset_export"
    bl_label   = "Export Set"

    index: bpy.props.IntProperty()

    def execute(self, context):
        props = context.scene.arantools_arp_export
        scene = context.scene
        armature = props.target_armature
        folder = bpy.path.abspath(props.export_folder)

        if not armature:
            self.report({'ERROR'}, "Select the Armature in the panel first.")
            return {'CANCELLED'}
        if not os.path.exists(folder):
            self.report({'ERROR'}, f"Export folder does not exist: {folder}")
            return {'CANCELLED'}
        if not (0 <= self.index < len(props.export_sets)):
            return {'CANCELLED'}

        prev_sel_only  = _arp_get(scene, 'arp_ge_sel_only', None)
        prev_bake_anim = _arp_get(scene, 'arp_bake_anim',   None)
        _arp_set(scene, 'arp_ge_sel_only', True)
        _arp_set(scene, 'arp_bake_anim',   False)

        ok = False
        try:
            ok = _do_export_set(context, props.export_sets[self.index],
                                folder, armature)
        except Exception as e:
            self.report({'ERROR'}, f"Export failed: {e}")
        finally:
            if prev_sel_only  is not None: _arp_set(scene, 'arp_ge_sel_only', prev_sel_only)
            if prev_bake_anim is not None: _arp_set(scene, 'arp_bake_anim',   prev_bake_anim)
            bpy.ops.object.select_all(action='DESELECT')
            armature.select_set(True)
            context.view_layer.objects.active = armature

        if ok:
            self.report({'INFO'}, "Exported set.")
            return {'FINISHED'}
        self.report({'WARNING'}, "Set skipped — empty meshes or empty filename.")
        return {'CANCELLED'}


class ARANTOOLS_OT_ExpSet_ExportAll(Operator):
    """Batch-export every set to its own FBX via Auto-Rig Pro.
Overwrites any existing files at the target paths."""
    bl_idname = "arantools.expset_export_all"
    bl_label  = "Export All Sets"

    def execute(self, context):
        props = context.scene.arantools_arp_export
        scene = context.scene
        armature = props.target_armature
        folder = bpy.path.abspath(props.export_folder)

        if not armature:
            self.report({'ERROR'}, "Select the Armature in the panel first.")
            return {'CANCELLED'}
        if not os.path.exists(folder):
            self.report({'ERROR'}, f"Export folder does not exist: {folder}")
            return {'CANCELLED'}
        if len(props.export_sets) == 0:
            self.report({'WARNING'}, "No export sets defined.")
            return {'CANCELLED'}

        prev_sel_only  = _arp_get(scene, 'arp_ge_sel_only', None)
        prev_bake_anim = _arp_get(scene, 'arp_bake_anim',   None)
        _arp_set(scene, 'arp_ge_sel_only', True)
        _arp_set(scene, 'arp_bake_anim',   False)

        count = 0
        try:
            for eset in props.export_sets:
                try:
                    if _do_export_set(context, eset, folder, armature):
                        count += 1
                except Exception as e:
                    self.report({'ERROR'},
                                f"Failed on set '{eset.filename}': {e}")
                    print(f"Error on set '{eset.filename}': {e}")
        finally:
            if prev_sel_only  is not None: _arp_set(scene, 'arp_ge_sel_only', prev_sel_only)
            if prev_bake_anim is not None: _arp_set(scene, 'arp_bake_anim',   prev_bake_anim)
            bpy.ops.object.select_all(action='DESELECT')
            armature.select_set(True)
            context.view_layer.objects.active = armature

        self.report({'INFO'},
                    f"Exported {count}/{len(props.export_sets)} set(s).")
        return {'FINISHED'}


# ============================================================================
# Registration
# ============================================================================

classes = [
    ARANTOOLS_PG_ExportSetMesh,
    ARANTOOLS_PG_ExportSet,
    ARANTOOLS_PG_ARPExport,
    ARANTOOLS_OT_ARPBatchExport,
    ARANTOOLS_OT_ARPAnimExport,
    ARANTOOLS_OT_ExpSet_AddFromSelection,
    ARANTOOLS_OT_ExpSet_Add,
    ARANTOOLS_OT_ExpSet_Remove,
    ARANTOOLS_OT_ExpSet_SetMeshes,
    ARANTOOLS_OT_ExpSet_AddMeshes,
    ARANTOOLS_OT_ExpSet_Clear,
    ARANTOOLS_OT_ExpSet_Select,
    ARANTOOLS_OT_ExpSet_ExportOne,
    ARANTOOLS_OT_ExpSet_ExportAll,
]


def register():
    for cls in classes:
        bpy.utils.register_class(cls)
    bpy.types.Scene.arantools_arp_export = bpy.props.PointerProperty(type=ARANTOOLS_PG_ARPExport)


def unregister():
    del bpy.types.Scene.arantools_arp_export
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
