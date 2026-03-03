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

# Colored ball icons (COLORSET_*_VEC) — available since Blender 2.8
_SECTION_COLORS = {
    'rigging':      'COLORSET_03_VEC',  # orange-red
    'weight_tools': 'COLORSET_06_VEC',  # green
    'organization': 'COLORSET_09_VEC',  # blue
    'export_panel': 'COLORSET_05_VEC',  # yellow
    'naming':       'COLORSET_07_VEC',  # teal
    'animation':    'COLORSET_11_VEC',  # purple
}


# ============================================================================
# Panel state
# ============================================================================

class ARANTOOLS_PG_PanelState(bpy.types.PropertyGroup):
    rigging:      bpy.props.BoolProperty(default=False)
    weight_tools: bpy.props.BoolProperty(default=False)
    organization: bpy.props.BoolProperty(default=False)
    export_panel: bpy.props.BoolProperty(default=False)
    naming:       bpy.props.BoolProperty(default=False)
    animation:    bpy.props.BoolProperty(default=False)


class ARANTOOLS_OT_ToggleSection(bpy.types.Operator):
    bl_idname = "arantools.toggle_section"
    bl_label = ""
    bl_description = "Toggle section"
    bl_options = {'INTERNAL', 'REGISTER'}

    prop: bpy.props.StringProperty()

    def execute(self, context):
        state = context.scene.arantools_panel_state
        setattr(state, self.prop, not getattr(state, self.prop))
        return {'FINISHED'}


def _section(layout, state, key, label, icon):
    """Draw a ZenUV-style section header. Returns True when the section is open."""
    expanded = getattr(state, key)
    color_icon = _SECTION_COLORS.get(key, 'BLANK1')
    arrow = 'TRIA_DOWN' if expanded else 'TRIA_RIGHT'

    row = layout.row(align=True)
    row.scale_y = 1.2

    # Small colored dot button (icon-only)
    op = row.operator(
        "arantools.toggle_section",
        text="", icon=color_icon,
        emboss=True, depress=expanded,
    )
    op.prop = key

    # Wide label button
    op2 = row.operator(
        "arantools.toggle_section",
        text=label, icon=arrow,
        emboss=True, depress=expanded,
    )
    op2.prop = key

    return expanded


# ============================================================================
# Main panel
# ============================================================================

class ARANTOOLS_PT_main(Panel):
    bl_label = "Aran Tools"
    bl_idname = "ARANTOOLS_PT_main"
    bl_space_type = _PANEL_SPACE
    bl_region_type = _PANEL_REGION
    bl_category = _PANEL_CATEGORY

    def draw(self, context):
        layout = self.layout
        state = context.scene.arantools_panel_state

        if _section(layout, state, 'rigging', "Rigging", 'ARMATURE_DATA'):
            self._draw_rigging(layout, context)

        if _section(layout, state, 'weight_tools', "Weight Tools", 'MOD_VERTEX_WEIGHT'):
            self._draw_weight_tools(layout, context)

        if _section(layout, state, 'organization', "Organization", 'OUTLINER'):
            self._draw_organization(layout, context)

        if _section(layout, state, 'export_panel', "Export", 'EXPORT'):
            self._draw_export(layout, context)

        if _section(layout, state, 'naming', "Naming", 'SORTALPHA'):
            self._draw_naming(layout, context)

        if _section(layout, state, 'animation', "Animation", 'ANIM'):
            self._draw_animation(layout, context)

    # ── Rigging ──────────────────────────────────────────────────────────────

    def _draw_rigging(self, layout, context):
        props = context.scene.arantools_adv_rigging
        col = layout.column(align=False)

        box = col.box()
        box.label(text="Selection", icon='FILTER')
        box.operator("arantools.select_deform_bones", icon='BONE_DATA')
        row = box.row(align=True)
        row.operator("arantools.select_bone_type", text="Control Bones").bone_type = 'CONTROL'
        row.operator("arantools.select_bone_type", text="Mech Bones").bone_type = 'MECH'

        box = col.box()
        box.label(text="Mirror", icon='MOD_MIRROR')
        box.operator("arantools.mirror_bones", icon='MOD_MIRROR')

        box = col.box()
        box.label(text="Feather Rigger", icon='OUTLINER_OB_CURVES')
        fr = context.scene.arantools_feather_rig
        c = box.column(align=True)
        c.prop(fr, "target_armature")
        c.separator()
        c.label(text="Matching:", icon='CON_TRACKTO')
        c.prop(fr, "pointer_method", text="Island Pointer")
        c.prop(fr, "bone_target", text="Measure to")
        c.separator()
        c.label(text="Weighting:", icon='MOD_VERTEX_WEIGHT')
        c.prop(fr, "weight_method", text="Method")
        c.prop(fr, "chain_length")
        c.prop(fr, "exclusive_chains")
        c.separator()
        c.label(text="Bone Filters:", icon='FILTER')
        c.prop(fr, "use_selected_only")
        c.prop(fr, "filter_include", text="Include")
        c.prop(fr, "filter_exclude", text="Exclude")
        c.separator()
        run_row = c.row()
        run_row.enabled = fr.target_armature is not None
        run_row.scale_y = 1.4
        run_row.operator("arantools.rig_feathers", icon='AUTO')
        if fr.weight_method == 'ARP':
            c.label(text="Requires Auto-Rig Pro", icon='ERROR')

        box = col.box()
        box.label(text="Join & Bind", icon='MOD_DATA_TRANSFER')
        c = box.column(align=True)
        c.prop(props, "target_collection")
        c.operator("arantools.join_targets", icon='OBJECT_DATAMODE')
        c.separator()
        c.prop(props, "source_mesh")
        c.prop(props, "mapping_method", text="")
        c.operator("arantools.bind_and_transfer", icon='ARMATURE_DATA')

        box = col.box()
        box.label(text="Weight from Pointer  (ARP)", icon='BONE_DATA')
        c = box.column(align=True)
        c.prop(props, "sharp_edge_pointer_method", text="Pointer By")
        c.prop(props, "chain_length")
        c.operator("arantools.weight_from_pointer", icon='CURVE_PATH')
        c.operator("arantools.direct_arp_bind", icon='LINKED')
        c.label(text="Requires Auto-Rig Pro", icon='ERROR')

    # ── Weight Tools ─────────────────────────────────────────────────────────

    def _draw_weight_tools(self, layout, context):
        col = layout.column(align=False)

        box = col.box()
        box.label(text="Smart Weight Transfer", icon='MOD_DATA_TRANSFER')
        props = context.scene.arantools_smart_transfer
        c = box.column(align=True)
        c.prop(props, "source_mesh")
        c.prop(props, "interp_mode", text="")
        c.prop(props, "clean_empty")
        c.operator("arantools.smart_weight_transfer", icon='ARMATURE_DATA')

        box = col.box()
        box.label(text="Sync Vertex Groups", icon='GROUP_VERTEX')
        box.label(text="Active mesh must have an armature.", icon='INFO')
        box.operator("arantools.sync_vertex_groups", icon='FILE_REFRESH')

        box = col.box()
        box.label(text="Unify Island Weights", icon='SNAP_FACE')
        props = context.scene.arantools_unify_weights
        c = box.column(align=True)
        c.prop(props, "blend", slider=True)
        c.prop(props, "only_selected")
        c.operator("arantools.unify_island_weights", icon='FULLSCREEN_ENTER')

        box = col.box()
        box.label(text="Island Weight Copy", icon='COPY_ID')
        props = context.scene.arantools_island_copy
        c = box.column(align=True)
        c.prop(props, "base_vertex_method", text="Base By")
        if props.base_vertex_method == 'ATTRIBUTE':
            c.prop(props, "base_attribute_name")
        c.prop(props, "blend_factor", slider=True)
        c.prop(props, "only_selected")
        c.operator("arantools.island_weight_copy", icon='FORWARD')

        box = col.box()
        box.label(text="Contact Weight Flooder", icon='PARTICLE_POINT')
        props = context.scene.arantools_contact_flood
        c = box.column(align=True)
        c.prop(props, "source")
        c.prop(props, "blend", slider=True)
        c.prop(props, "use_selection")
        c.prop(props, "use_uv")
        if props.use_uv:
            sub = c.box()
            sub.prop(props, "uv_name")
            row = sub.row(align=True)
            row.prop(props, "uv_axis", expand=True)
            row.prop(props, "uv_direction", expand=True)
        c.operator("arantools.contact_flood", icon='DRIVER_DISTANCE')

    # ── Organization ─────────────────────────────────────────────────────────

    def _draw_organization(self, layout, context):
        col = layout.column(align=False)

        box = col.box()
        box.label(text="Batch Rig Transfer", icon='ARMATURE_DATA')
        props = context.scene.arantools_rt_props
        c = box.column(align=True)
        c.prop(props, "source_collection")
        c.prop(props, "gt_collection")
        c.prop(props, "target_collection")
        c.separator()
        c.prop(props, "apply_modifiers")
        c.prop(props, "transfer_method", text="")
        c.separator()
        c.operator("arantools.rt_populate_list", icon='FILE_REFRESH')
        if props.binding_list:
            box.label(text="Source → Ground Truth:")
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

        box = col.box()
        box.label(text="Collection Baker", icon='RENDER_STILL')
        bake_props = context.scene.arantools_bake_scene
        c = box.column(align=True)
        c.prop(bake_props, "source_collection")
        c.prop(bake_props, "target_collection")
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

    # ── Export ───────────────────────────────────────────────────────────────

    def _draw_export(self, layout, context):
        col = layout.column(align=False)
        props = context.scene.arantools_arp_export

        box = col.box()
        box.label(text="ARP Batch Export", icon='EXPORT')
        c = box.column(align=True)
        c.prop(props, "target_armature")
        c.prop(props, "export_folder")
        c.separator()
        c.label(text="Naming Rules:")
        c.prop(props, "remove_str", text="Remove")
        row = c.row(align=True)
        row.prop(props, "prefix_str", text="Prefix")
        row.prop(props, "suffix_str", text="Suffix")
        c.separator()

        from . import export as _export
        selected_meshes = [
            obj for obj in context.selected_objects
            if obj != props.target_armature and obj.type == 'MESH'
        ]
        if selected_meshes:
            preview_box = box.box()
            obj = selected_meshes[0]
            final_name = _export._get_final_filename(obj.name, props)
            preview_box.label(text=f"Org:  {obj.name}", icon='OBJECT_DATAMODE')
            if not final_name:
                preview_box.label(text="New:  [EMPTY NAME]", icon='ERROR')
            else:
                preview_box.label(text=f"New:  {final_name}.fbx", icon='FORWARD')
            if len(selected_meshes) > 1:
                preview_box.label(text=f"+ {len(selected_meshes) - 1} more selected")
        else:
            box.label(text="Select meshes to preview.", icon='INFO')

        row = box.row()
        row.scale_y = 1.4
        row.enabled = bool(props.target_armature)
        row.operator("arantools.arp_batch_export", icon='EXPORT')
        box.label(text="Requires Auto-Rig Pro", icon='ERROR')

    # ── Naming ───────────────────────────────────────────────────────────────

    def _draw_naming(self, layout, context):
        scene = context.scene
        col = layout.column(align=False)

        box = col.box()
        box.label(text="Bone Renamer", icon='SORTALPHA')
        c = box.column(align=True)
        c.prop(scene, 'arantools_format', text='Format')
        c.separator()

        row = c.row(align=True)
        row.prop(scene, 'arantools_t1', text='T1')
        row.prop(scene, 'arantools_t2', text='T2')
        row = c.row(align=True)
        row.prop(scene, 'arantools_t3', text='T3')
        row.prop(scene, 'arantools_t4', text='T4')
        c.separator()

        row = c.row(align=True)
        row.prop(scene, 'arantools_n1', text='N1')
        row.prop(scene, 'arantools_n2', text='N2')
        row.prop(scene, 'arantools_n3', text='N3')
        c.prop(scene, 'arantools_inc', text='Counter (INC)')
        c.separator()

        preview = scene.arantools_format
        preview = preview.replace('N1', '0' + str(scene.arantools_n1))
        preview = preview.replace('N2', '0' + str(scene.arantools_n2))
        preview = preview.replace('N3', '0' + str(scene.arantools_n3))
        preview = preview.replace('T1', scene.arantools_t1)
        preview = preview.replace('T2', scene.arantools_t2)
        preview = preview.replace('T3', scene.arantools_t3)
        preview = preview.replace('T4', scene.arantools_t4)
        preview = preview.replace('INC', '0' + str(scene.arantools_inc))
        preview_box = c.box()
        preview_box.label(text=preview if preview else "(empty)", icon='BONE_DATA')
        c.separator()

        row = c.row(align=True)
        row.operator("arantools.rename_bone", text='Rename  (Alt+Shift+R)', icon='GREASEPENCIL')
        c.operator("arantools.reset_counter", text='Reset Counter  (Shift+Y)', icon='LOOP_BACK')

    # ── Animation ────────────────────────────────────────────────────────────

    def _draw_animation(self, layout, context):
        scene = context.scene
        col = layout.column(align=False)

        box = col.box()
        box.label(text="Noise on Bones", icon='FORCE_TURBULENCE')
        c = box.column(align=True)
        c.prop(scene, 'arantools_rotation_strength', text='Rot Strength', slider=True)
        c.prop(scene, 'arantools_rotation_scale', text='Rot Speed', slider=True)
        c.prop(scene, 'arantools_location_strenght', text='Loc Strength', slider=True)
        c.prop(scene, 'arantools_location_scale', text='Loc Speed', slider=True)
        c.separator()
        c.prop(scene, 'arantools_frame_length', text='Frame Length')
        c.prop(scene, 'arantools_blend_duration', text='Blend Duration')
        c.separator()
        row = c.row(align=True)
        row.operator("arantools.apply_noise_rotation", text='Rotation', icon='FORCE_FORCE')
        row.operator("arantools.apply_noise_location", text='Location', icon='FORCE_FORCE')


# ============================================================================
# Registration
# ============================================================================

classes = [
    ARANTOOLS_PG_PanelState,
    ARANTOOLS_OT_ToggleSection,
    ARANTOOLS_PT_main,
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
    bpy.types.Scene.arantools_panel_state = bpy.props.PointerProperty(
        type=ARANTOOLS_PG_PanelState
    )


def unregister():
    del bpy.types.Scene.arantools_panel_state
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
    export.unregister()
    organization.unregister()
    weight_tools.unregister()
    naming.unregister()
    animation.unregister()
    rigging.unregister()
