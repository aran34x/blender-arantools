bl_info = {
    "name": "Aran Tools",
    "author": "Aran",
    "version": (0, 2, 0),
    "blender": (3, 0, 0),
    "location": "View3D > Sidebar > Aran Tools",
    "description": "A collection of rigging, weight, organization, animation, and naming tools.",
    "category": "Rigging",
}

import bpy
from bpy.types import Panel

from . import rigging
from . import animation
from . import naming
from . import weight_tools
from . import organization
from . import export


_PANEL_SPACE = 'VIEW_3D'
_PANEL_REGION = 'UI'
_PANEL_CATEGORY = 'Aran Tools'


# ============================================================================
# Root panel
# ============================================================================

class ARANTOOLS_PT_main(Panel):
    bl_label = "Aran Tools"
    bl_idname = "ARANTOOLS_PT_main"
    bl_space_type = _PANEL_SPACE
    bl_region_type = _PANEL_REGION
    bl_category = _PANEL_CATEGORY

    def draw(self, context):
        pass


# ============================================================================
# Rigging sub-panel
# ============================================================================

class ARANTOOLS_PT_rigging(Panel):
    bl_label = "Rigging"
    bl_idname = "ARANTOOLS_PT_rigging"
    bl_space_type = _PANEL_SPACE
    bl_region_type = _PANEL_REGION
    bl_category = _PANEL_CATEGORY
    bl_parent_id = "ARANTOOLS_PT_main"
    bl_options = {'DEFAULT_CLOSED'}

    def draw_header(self, context):
        self.layout.label(text="", icon='COLORSET_01_VEC')  # red

    def draw(self, context):
        layout = self.layout
        props = context.scene.arantools_adv_rigging

        box = layout.box()
        box.label(text="Selection", icon='FILTER')
        box.operator("arantools.select_deform_bones", icon='BONE_DATA')
        row = box.row(align=True)
        row.operator("arantools.select_bone_type", text="Control Bones").bone_type = 'CONTROL'
        row.operator("arantools.select_bone_type", text="Mech Bones").bone_type = 'MECH'

        box = layout.box()
        box.label(text="Mirror", icon='MOD_MIRROR')
        box.operator("arantools.mirror_bones", icon='MOD_MIRROR')

        box = layout.box()
        box.label(text="Feather Rigger", icon='OUTLINER_OB_CURVES')
        fr = context.scene.arantools_feather_rig
        col = box.column(align=True)
        col.prop(fr, "target_armature")
        col.separator()
        col.label(text="Matching:", icon='CON_TRACKTO')
        col.prop(fr, "pointer_method", text="Island Pointer")
        col.prop(fr, "bone_target", text="Measure to")
        col.separator()
        col.label(text="Weighting:", icon='MOD_VERTEX_WEIGHT')
        col.prop(fr, "weight_method", text="Method")
        col.prop(fr, "chain_length")
        col.prop(fr, "exclusive_chains")
        col.separator()
        col.label(text="Bone Filters:", icon='FILTER')
        col.prop(fr, "use_selected_only")
        col.prop(fr, "filter_include", text="Include")
        col.prop(fr, "filter_exclude", text="Exclude")
        col.separator()
        run_row = col.row()
        run_row.enabled = fr.target_armature is not None
        run_row.scale_y = 1.4
        run_row.operator("arantools.rig_feathers", icon='AUTO')
        if fr.weight_method == 'ARP':
            col.label(text="Requires Auto-Rig Pro", icon='ERROR')

        box = layout.box()
        box.label(text="Join & Bind", icon='MOD_DATA_TRANSFER')
        col = box.column(align=True)
        col.prop(props, "target_collection")
        col.operator("arantools.join_targets", icon='OBJECT_DATAMODE')
        col.separator()
        col.prop(props, "source_mesh")
        col.prop(props, "mapping_method", text="")
        col.operator("arantools.bind_and_transfer", icon='ARMATURE_DATA')

        box = layout.box()
        box.label(text="Weight from Pointer  (ARP)", icon='BONE_DATA')
        col = box.column(align=True)
        col.prop(props, "sharp_edge_pointer_method", text="Pointer By")
        col.prop(props, "chain_length")
        col.operator("arantools.weight_from_pointer", icon='CURVE_PATH')
        col.operator("arantools.direct_arp_bind", icon='LINKED')
        col.label(text="Requires Auto-Rig Pro", icon='ERROR')


# ============================================================================
# Weight Tools sub-panel
# ============================================================================

class ARANTOOLS_PT_weight_tools(Panel):
    bl_label = "Weight Tools"
    bl_idname = "ARANTOOLS_PT_weight_tools"
    bl_space_type = _PANEL_SPACE
    bl_region_type = _PANEL_REGION
    bl_category = _PANEL_CATEGORY
    bl_parent_id = "ARANTOOLS_PT_main"
    bl_options = {'DEFAULT_CLOSED'}

    def draw_header(self, context):
        self.layout.label(text="", icon='COLORSET_03_VEC')  # orange

    def draw(self, context):
        layout = self.layout

        box = layout.box()
        box.label(text="Smart Weight Transfer", icon='MOD_DATA_TRANSFER')
        props = context.scene.arantools_smart_transfer
        col = box.column(align=True)
        col.prop(props, "source_mesh")
        col.prop(props, "interp_mode", text="")
        col.prop(props, "clean_empty")
        col.operator("arantools.smart_weight_transfer", icon='ARMATURE_DATA')

        box = layout.box()
        box.label(text="Sync Vertex Groups", icon='GROUP_VERTEX')
        box.label(text="Active mesh must have an armature.", icon='INFO')
        box.operator("arantools.sync_vertex_groups", icon='FILE_REFRESH')

        box = layout.box()
        box.label(text="Unify Island Weights", icon='SNAP_FACE')
        props = context.scene.arantools_unify_weights
        col = box.column(align=True)
        col.prop(props, "blend", slider=True)
        col.prop(props, "only_selected")
        col.operator("arantools.unify_island_weights", icon='FULLSCREEN_ENTER')

        box = layout.box()
        box.label(text="Island Weight Copy", icon='COPY_ID')
        props = context.scene.arantools_island_copy
        col = box.column(align=True)
        col.prop(props, "base_vertex_method", text="Base By")
        if props.base_vertex_method == 'ATTRIBUTE':
            col.prop(props, "base_attribute_name")
        col.prop(props, "blend_factor", slider=True)
        col.prop(props, "only_selected")
        col.operator("arantools.island_weight_copy", icon='FORWARD')

        box = layout.box()
        box.label(text="Contact Weight Flooder", icon='PARTICLE_POINT')
        props = context.scene.arantools_contact_flood
        col = box.column(align=True)
        col.prop(props, "source")
        col.prop(props, "blend", slider=True)
        col.prop(props, "use_selection")
        col.prop(props, "use_uv")
        if props.use_uv:
            sub = col.box()
            sub.prop(props, "uv_name")
            row = sub.row(align=True)
            row.prop(props, "uv_axis", expand=True)
            row.prop(props, "uv_direction", expand=True)
        col.operator("arantools.contact_flood", icon='DRIVER_DISTANCE')


# ============================================================================
# Organization sub-panel
# ============================================================================

class ARANTOOLS_PT_organization(Panel):
    bl_label = "Organization"
    bl_idname = "ARANTOOLS_PT_organization"
    bl_space_type = _PANEL_SPACE
    bl_region_type = _PANEL_REGION
    bl_category = _PANEL_CATEGORY
    bl_parent_id = "ARANTOOLS_PT_main"
    bl_options = {'DEFAULT_CLOSED'}

    def draw_header(self, context):
        self.layout.label(text="", icon='COLORSET_09_VEC')  # blue

    def draw(self, context):
        layout = self.layout

        box = layout.box()
        box.label(text="Batch Rig Transfer", icon='ARMATURE_DATA')
        props = context.scene.arantools_rt_props
        col = box.column(align=True)
        col.prop(props, "source_collection")
        col.prop(props, "gt_collection")
        col.prop(props, "target_collection")
        col.separator()
        col.prop(props, "apply_modifiers")
        col.prop(props, "transfer_method", text="")
        col.separator()
        col.operator("arantools.rt_populate_list", icon='FILE_REFRESH')
        if props.binding_list:
            box.label(text="Source -> Ground Truth:")
            box.template_list(
                "ARANTOOLS_UL_RT_BindingList", "",
                props, "binding_list",
                props, "binding_index",
                rows=4,
            )
            row = box.row()
            row.enabled = bool(props.target_collection)
            row.operator("arantools.rt_execute_rigging", icon='PLAY')
            if not props.target_collection:
                box.label(text="Select a Target Collection!", icon='ERROR')

        box = layout.box()
        box.label(text="Collection Baker", icon='RENDER_STILL')
        bake_props = context.scene.arantools_bake_scene
        col = box.column(align=True)
        col.prop(bake_props, "source_collection")
        col.prop(bake_props, "target_collection")
        if bake_props.source_collection:
            box.separator()
            mesh_box = box.box()
            mesh_box.label(text="Set Target Names:", icon='OUTLINER_OB_MESH')
            for obj in bake_props.source_collection.objects:
                if obj.type != 'MESH':
                    continue
                row = mesh_box.row(align=True)
                row.label(text=obj.name, icon='MESH_DATA')
                row.prop(obj.arantools_bake_props, "target_name", text="")
            row = box.row()
            row.scale_y = 1.4
            row.operator("arantools.collection_bake", icon='RENDER_STILL')
        else:
            box.label(text="Select a Source Collection.", icon='INFO')


# ============================================================================
# Export sub-panel
# ============================================================================

class ARANTOOLS_PT_export(Panel):
    bl_label = "Export"
    bl_idname = "ARANTOOLS_PT_export"
    bl_space_type = _PANEL_SPACE
    bl_region_type = _PANEL_REGION
    bl_category = _PANEL_CATEGORY
    bl_parent_id = "ARANTOOLS_PT_main"
    bl_options = {'DEFAULT_CLOSED'}

    def draw_header(self, context):
        self.layout.label(text="", icon='COLORSET_05_VEC')  # yellow

    def draw(self, context):
        layout = self.layout
        props = context.scene.arantools_arp_export

        box = layout.box()
        box.label(text="ARP Batch Export", icon='EXPORT')
        col = box.column(align=True)
        col.prop(props, "target_armature")
        col.prop(props, "export_folder")
        col.separator()
        col.label(text="Naming Rules:")
        col.prop(props, "remove_str", text="Remove")
        row = col.row(align=True)
        row.prop(props, "prefix_str", text="Prefix")
        row.prop(props, "suffix_str", text="Suffix")
        col.separator()

        from . import export as _export
        selected_meshes = [
            obj for obj in context.selected_objects
            if obj != props.target_armature and obj.type == 'MESH'
        ]
        if selected_meshes:
            preview_box = box.box()
            obj = selected_meshes[0]
            final_name = _export._get_final_filename(obj.name, props)
            preview_box.label(text="Org:  " + obj.name, icon='OBJECT_DATAMODE')
            if not final_name:
                preview_box.label(text="New:  [EMPTY NAME]", icon='ERROR')
            else:
                preview_box.label(text="New:  " + final_name + ".fbx", icon='FORWARD')
            if len(selected_meshes) > 1:
                preview_box.label(text="+ " + str(len(selected_meshes) - 1) + " more selected")
        else:
            box.label(text="Select meshes to preview.", icon='INFO')

        row = box.row()
        row.scale_y = 1.4
        row.enabled = bool(props.target_armature)
        row.operator("arantools.arp_batch_export", icon='EXPORT')
        box.label(text="Requires Auto-Rig Pro", icon='ERROR')


# ============================================================================
# Naming sub-panel
# ============================================================================

class ARANTOOLS_PT_naming(Panel):
    bl_label = "Naming"
    bl_idname = "ARANTOOLS_PT_naming"
    bl_space_type = _PANEL_SPACE
    bl_region_type = _PANEL_REGION
    bl_category = _PANEL_CATEGORY
    bl_parent_id = "ARANTOOLS_PT_main"
    bl_options = {'DEFAULT_CLOSED'}

    def draw_header(self, context):
        self.layout.label(text="", icon='COLORSET_07_VEC')  # teal

    def draw(self, context):
        layout = self.layout
        scene = context.scene

        box = layout.box()
        box.label(text="Bone Renamer", icon='SORTALPHA')
        col = box.column(align=True)
        col.prop(scene, 'arantools_format', text='Format')
        col.separator()

        row = col.row(align=True)
        row.prop(scene, 'arantools_t1', text='T1')
        row.prop(scene, 'arantools_t2', text='T2')
        row = col.row(align=True)
        row.prop(scene, 'arantools_t3', text='T3')
        row.prop(scene, 'arantools_t4', text='T4')
        col.separator()

        row = col.row(align=True)
        row.prop(scene, 'arantools_n1', text='N1')
        row.prop(scene, 'arantools_n2', text='N2')
        row.prop(scene, 'arantools_n3', text='N3')
        col.prop(scene, 'arantools_inc', text='Counter (INC)')
        col.separator()

        preview = scene.arantools_format
        preview = preview.replace('N1', '0' + str(scene.arantools_n1))
        preview = preview.replace('N2', '0' + str(scene.arantools_n2))
        preview = preview.replace('N3', '0' + str(scene.arantools_n3))
        preview = preview.replace('T1', scene.arantools_t1)
        preview = preview.replace('T2', scene.arantools_t2)
        preview = preview.replace('T3', scene.arantools_t3)
        preview = preview.replace('T4', scene.arantools_t4)
        preview = preview.replace('INC', '0' + str(scene.arantools_inc))
        preview_box = col.box()
        preview_box.label(text=preview if preview else "(empty)", icon='BONE_DATA')
        col.separator()

        row = col.row(align=True)
        row.operator("arantools.rename_bone", text='Rename  (Alt+Shift+R)', icon='GREASEPENCIL')
        col.operator("arantools.reset_counter", text='Reset Counter  (Shift+Y)', icon='LOOP_BACK')


# ============================================================================
# Animation sub-panel
# ============================================================================

class ARANTOOLS_PT_animation(Panel):
    bl_label = "Animation"
    bl_idname = "ARANTOOLS_PT_animation"
    bl_space_type = _PANEL_SPACE
    bl_region_type = _PANEL_REGION
    bl_category = _PANEL_CATEGORY
    bl_parent_id = "ARANTOOLS_PT_main"
    bl_options = {'DEFAULT_CLOSED'}

    def draw_header(self, context):
        self.layout.label(text="", icon='COLORSET_11_VEC')  # purple

    def draw(self, context):
        layout = self.layout
        scene = context.scene

        box = layout.box()
        box.label(text="Noise on Bones", icon='FORCE_TURBULENCE')
        col = box.column(align=True)
        col.prop(scene, 'arantools_rotation_strength', text='Rot Strength', slider=True)
        col.prop(scene, 'arantools_rotation_scale', text='Rot Speed', slider=True)
        col.prop(scene, 'arantools_location_strenght', text='Loc Strength', slider=True)
        col.prop(scene, 'arantools_location_scale', text='Loc Speed', slider=True)
        col.separator()
        col.prop(scene, 'arantools_frame_length', text='Frame Length')
        col.prop(scene, 'arantools_blend_duration', text='Blend Duration')
        col.separator()
        row = col.row(align=True)
        row.operator("arantools.apply_noise_rotation", text='Rotation', icon='FORCE_FORCE')
        row.operator("arantools.apply_noise_location", text='Location', icon='FORCE_FORCE')


# ============================================================================
# Registration
# ============================================================================

classes = [
    ARANTOOLS_PT_main,
    ARANTOOLS_PT_rigging,
    ARANTOOLS_PT_weight_tools,
    ARANTOOLS_PT_organization,
    ARANTOOLS_PT_export,
    ARANTOOLS_PT_naming,
    ARANTOOLS_PT_animation,
]


def register():
    rigging.register()
    animation.register()
    naming.register()
    weight_tools.register()
    organization.register()
    export.register()
    for cls in classes:
        bpy.utils.register_class(cls)


def unregister():
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
    export.unregister()
    organization.unregister()
    weight_tools.unregister()
    naming.unregister()
    animation.unregister()
    rigging.unregister()
