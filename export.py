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
    """Batch export all selected meshes as FBX using Auto-Rig Pro"""
    bl_idname = "arantools.arp_batch_export"
    bl_label = "Batch Export Objects"

    def execute(self, context):
        props = context.scene.arantools_arp_export
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

        count = 0
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

        bpy.ops.object.select_all(action='DESELECT')
        armature.select_set(True)
        context.view_layer.objects.active = armature

        self.report({'INFO'}, f"Exported {count} file(s) successfully.")
        return {'FINISHED'}


# ============================================================================
# Registration
# ============================================================================

classes = [
    ARANTOOLS_PG_ARPExport,
    ARANTOOLS_OT_ARPBatchExport,
]


def register():
    for cls in classes:
        bpy.utils.register_class(cls)
    bpy.types.Scene.arantools_arp_export = bpy.props.PointerProperty(type=ARANTOOLS_PG_ARPExport)


def unregister():
    del bpy.types.Scene.arantools_arp_export
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
