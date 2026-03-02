bl_info = {
    "name": "Aran Tools",
    "author": "Aran",
    "version": (0, 1, 0),
    "blender": (3, 0, 0),
    "location": "View3D > Sidebar > Aran Tools",
    "description": "A collection of rigging, organization, animation, and naming tools.",
    "category": "Rigging",
}

import bpy
from bpy.types import Panel

# --- Sub-modules will be imported here as tools are added ---
from . import rigging
from . import animation
from . import naming
# from . import organization


# ============================================================================
# Main Panel
# ============================================================================

class ARANTOOLS_PT_main(Panel):
    """Main Aran Tools Panel"""
    bl_label = "Aran Tools"
    bl_idname = "ARANTOOLS_PT_main"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = 'Aran Tools'

    def draw(self, context):
        layout = self.layout

        # ========== RIGGING SECTION ==========
        box = layout.box()
        box.label(text="Rigging", icon='BONE_DATA')

        # Selection Tools
        subbox = box.box()
        subbox.label(text="Selection", icon='FILTER')
        subbox.operator("arantools.select_deform_bones", icon='BONE_DATA')
        row = subbox.row()
        row.operator("arantools.select_bone_type", icon='FILTER').bone_type = 'CONTROL'
        row.label(text="Control Bones")

        # Mirror Tools
        subbox = box.box()
        subbox.label(text="Mirror", icon='MOD_MIRROR')
        subbox.operator("arantools.mirror_bones", icon='MOD_MIRROR')

        # Feather Rigger
        subbox = box.box()
        subbox.label(text="Feather Rigger", icon='FEATHER')
        subbox.operator("arantools.rig_feathers", icon='AUTO')

        # ========== NAMING SECTION ==========
        box = layout.box()
        box.label(text="Naming", icon='SORTALPHA')
        subbox = box.box()
        subbox.prop(context.scene, 'arantools_format', text='Format')
        subbox.prop(context.scene, 'arantools_inc', text='Counter')
        subbox.operator("arantools.rename_bone", text='Rename', icon='GREASEPENCIL')
        subbox.operator("arantools.reset_counter", icon='PANEL_CLOSE')

        # ========== ANIMATION SECTION ==========
        box = layout.box()
        box.label(text="Animation", icon='PLAY')
        subbox = box.box()
        subbox.label(text="Noise", icon='PARTICLES')
        col = subbox.column()
        col.prop(context.scene, 'arantools_rotation_strength', text='Rotation Strength', slider=True)
        col.prop(context.scene, 'arantools_rotation_scale', text='Rotation Speed', slider=True)
        col.prop(context.scene, 'arantools_location_strenght', text='Location Strength', slider=True)
        col.prop(context.scene, 'arantools_location_scale', text='Location Speed', slider=True)
        col.prop(context.scene, 'arantools_frame_length', text='Frame Length')
        col.prop(context.scene, 'arantools_blend_duration', text='Blend Duration')
        row = subbox.row()
        row.operator("arantools.apply_noise_rotation", text='Apply Rotation', icon='FORCE_FORCE')
        row.operator("arantools.apply_noise_location", text='Apply Location', icon='FORCE_FORCE')

        # ========== ORGANIZATION SECTION (PLACEHOLDER) ==========
        box = layout.box()
        box.label(text="Organization", icon='FOLDER_REDIRECT')
        box.label(text="Coming soon...", icon='INFO')


classes = [ARANTOOLS_PT_main]


def register():
    for cls in classes:
        bpy.utils.register_class(cls)
    rigging.register()
    animation.register()
    naming.register()
    # organization.register()


def unregister():
    # organization.unregister()
    naming.unregister()
    animation.unregister()
    rigging.unregister()
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
