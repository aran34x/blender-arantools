import bpy
import os
from bpy.types import Operator


def _get_final_filename(original_name, props):
    name = original_name
    if props.remove_str:
        for token in [t.strip() for t in props.remove_str.split(',')]:
            if token:
                name = name.replace(token, "")
    return bpy.path.clean_name(f"{props.prefix_str}{name}{props.suffix_str}")


def _arp_set(scene, prop, value):
    """Set an ARP scene property if it exists."""
    if hasattr(scene, prop):
        setattr(scene, prop, value)


def _arp_get(scene, prop, default=None):
    """Get an ARP scene property, returning default if it doesn't exist."""
    return getattr(scene, prop, default)


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
    """Export all actions for the armature as separate FBX files using Auto-Rig Pro.
Exports to an 'Animations' subfolder inside the export folder.
Sets 'Selected Objects Only', 'Bake Animations', 'As Multiple FBX Files',
and disables 'Only Active Action' automatically."""
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

        # ── Create Animations subfolder ────────────────────────────────────
        anim_folder = os.path.join(folder_path, "Animations")
        os.makedirs(anim_folder, exist_ok=True)

        # ── Store required ARP settings ────────────────────────────────────
        prev_sel_only       = _arp_get(scene, 'arp_ge_sel_only',          None)
        prev_bake_anim      = _arp_get(scene, 'arp_bake_anim',            None)
        prev_separate_fbx   = _arp_get(scene, 'arp_export_separate_fbx',  None)
        prev_only_active    = _arp_get(scene, 'arp_bake_only_active',      None)

        _arp_set(scene, 'arp_ge_sel_only',         True)   # only selected (armature)
        _arp_set(scene, 'arp_bake_anim',           True)   # bake animations
        _arp_set(scene, 'arp_export_separate_fbx', True)   # one FBX per action
        _arp_set(scene, 'arp_bake_only_active',    False)  # export all actions

        try:
            # Select only the armature
            bpy.ops.object.select_all(action='DESELECT')
            armature.select_set(True)
            context.view_layer.objects.active = armature

            # ARP uses this as the base filepath; with separate FBX enabled
            # it names each file after the action in the same directory
            filepath = os.path.join(anim_folder, "animations.fbx")
            print(f"Exporting animations → {anim_folder}")
            bpy.ops.arp.arp_export_fbx_panel(filepath=filepath)
        except Exception as e:
            self.report({'ERROR'}, f"Animation export failed: {e}")
            print(f"Animation export error: {e}")
            return {'CANCELLED'}
        finally:
            # ── Restore ARP settings ───────────────────────────────────────
            if prev_sel_only     is not None: _arp_set(scene, 'arp_ge_sel_only',         prev_sel_only)
            if prev_bake_anim    is not None: _arp_set(scene, 'arp_bake_anim',           prev_bake_anim)
            if prev_separate_fbx is not None: _arp_set(scene, 'arp_export_separate_fbx', prev_separate_fbx)
            if prev_only_active  is not None: _arp_set(scene, 'arp_bake_only_active',    prev_only_active)

            bpy.ops.object.select_all(action='DESELECT')
            armature.select_set(True)
            context.view_layer.objects.active = armature

        self.report({'INFO'}, f"Animation export complete → {anim_folder}")
        return {'FINISHED'}


# ============================================================================
# Registration
# ============================================================================

classes = [
    ARANTOOLS_PG_ARPExport,
    ARANTOOLS_OT_ARPBatchExport,
    ARANTOOLS_OT_ARPAnimExport,
]


def register():
    for cls in classes:
        bpy.utils.register_class(cls)
    bpy.types.Scene.arantools_arp_export = bpy.props.PointerProperty(type=ARANTOOLS_PG_ARPExport)


def unregister():
    del bpy.types.Scene.arantools_arp_export
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
