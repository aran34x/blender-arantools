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
from . import island_flatten
from . import modifier_sync


_PANEL_SPACE = 'VIEW_3D'
_PANEL_REGION = 'UI'
_PANEL_CATEGORY = 'Aran Tools'


# ============================================================================
# Main panel — single panel with icon tab switcher
# ============================================================================

class ARANTOOLS_OT_reload_addon(bpy.types.Operator):
    bl_idname = "arantools.reload_addon"
    bl_label = "Reload Addon"
    bl_description = "Reload Aran Tools, picking up any code changes without restarting Blender"

    def execute(self, context):
        import importlib
        import sys
        import addon_utils

        def do_reload():
            # Force-reload every cached arantools module from disk first
            submod_names = sorted(
                k for k in sys.modules
                if k == 'arantools' or k.startswith('arantools.')
            )
            for mod_name in submod_names:
                try:
                    importlib.reload(sys.modules[mod_name])
                except Exception as e:
                    print(f"[AranTools] reload warning – {mod_name}: {e}")

            addon_utils.disable("arantools", default_set=False)
            addon_utils.enable("arantools", default_set=False)

        bpy.app.timers.register(do_reload, first_interval=0.0)
        return {'FINISHED'}


# ============================================================================
# Tool registry  (id, name, description, tab, icon, draw-method name)
# ============================================================================

_TOOL_REGISTRY = [
    # (id, name, description, tab, icon, draw_method, is_small)
    # is_small=True: always expanded, no collapse toggle (for single-button tools)
    ('select_deform',  'Select Deform Bones',   'Select all bones with the Deform flag enabled',                                    'RIGGING',      'BONE_DATA',          '_draw_t_select_deform',  True),
    ('feather_rigger', 'Feather Rigger',         'Auto-rig feather or hair mesh islands to bone chains',                            'RIGGING',      'OUTLINER_OB_CURVES', '_draw_t_feather_rigger', False),
    ('join_bind',      'Join & Bind',            'Join costume meshes and bind them to a character by transferring weights',         'RIGGING',      'MOD_DATA_TRANSFER',  '_draw_t_join_bind',      False),
    ('weight_pointer',   'Weight from Pointer',      'Bind mesh islands to bones via sharp edge or UV pointers — requires Auto-Rig Pro', 'RIGGING', 'CURVE_PATH',        '_draw_t_weight_pointer',   False),
    ('island_flatten',   'Flatten Island Weights',   'Average deform bone weights across each mesh island so the island bends as a rigid unit', 'RIGGING', 'MOD_SMOOTH', '_draw_t_island_flatten',   False),
    ('smart_transfer', 'Smart Weight Transfer',  'Copy vertex weights from a source mesh with interpolation options',               'WEIGHT',       'MOD_DATA_TRANSFER',  '_draw_t_smart_transfer', False),
    ('sync_vgroups',   'Sync Vertex Groups',     'Add missing vertex groups from the armature to the active mesh',                  'WEIGHT',       'GROUP_VERTEX',       '_draw_t_sync_vgroups',   True),
    ('unify_island',   'Unify Island Weights',   'Blend and unify vertex weights uniformly across UV islands',                      'WEIGHT',       'SNAP_FACE',          '_draw_t_unify_island',   False),
    ('island_copy',    'Island Weight Copy',     'Copy weights from one mesh island to others using a base vertex reference',       'WEIGHT',       'COPY_ID',            '_draw_t_island_copy',    False),
    ('contact_flood',  'Contact Weight Flooder', 'Flood vertex weights based on proximity or UV contact with a source object',      'WEIGHT',       'PARTICLE_POINT',     '_draw_t_contact_flood',  False),
    ('batch_rig',      'Batch Rig Transfer',     'Transfer rigs from a source collection to a target collection in bulk',           'ORGANIZATION', 'ARMATURE_DATA',      '_draw_t_batch_rig',      False),
    ('coll_baker',     'Collection Baker',       'Bake and rename meshes from one collection into another',                         'ORGANIZATION', 'RENDER_STILL',       '_draw_t_coll_baker',     False),
    ('mod_sync',       'Modifier Sync',          'Save a modifier stack from one object and copy or sync it to other objects',       'ORGANIZATION', 'MODIFIER',           '_draw_t_mod_sync',       False),
    ('arp_export',     'ARP Batch Export',       'Export selected meshes as FBX files using Auto-Rig Pro naming conventions',       'EXPORT',       'EXPORT',             '_draw_t_arp_export',     False),
    ('export_sets',    'ARP Export Sets',        'Group meshes into named sets and batch-export each as its own FBX via Auto-Rig Pro. Overwrites existing files', 'EXPORT', 'GROUP',          '_draw_t_export_sets',    False),
    ('seq_namer',      'Object Sequence Namer',  'Name selected objects as a numbered sequence (e.g. Monkey_01, Monkey_02), filling gaps and respecting existing names', 'NAMING', 'LINENUMBERS_ON', '_draw_t_seq_namer', False),
    ('bone_renamer',   'Bone Renamer',           'Rename bones using a custom token-based format string with auto-counters',        'NAMING',       'SORTALPHA',          '_draw_t_bone_renamer',   False),
    ('anim_org',       'Animation Organization', 'Manage an armature\'s actions: list, create (with Fake User), and auto-set the timeline from a "_NNN" duration suffix', 'ANIMATION', 'ACTION',           '_draw_t_anim_org',       False),
    ('spring_smooth',  'Spring Smooth Curves',   'Damped-spring resample of selected pose bones\' transform curves. Smooths motion between keyframes while preserving stop points', 'ANIMATION', 'IPO_BOUNCE',     '_draw_t_spring_smooth',  False),
    ('noise_bones',    'Noise on Bones',         'Add procedural noise FCurve modifiers to pose bones for organic motion',          'ANIMATION',    'FORCE_TURBULENCE',   '_draw_t_noise_bones',    False),
]

# Only collapsible (non-small) tools need a BoolProperty
_OPEN_TOOL_IDS = [entry[0] for entry in _TOOL_REGISTRY if not entry[6]]


class ARANTOOLS_OT_clear_search(bpy.types.Operator):
    bl_idname = "arantools.clear_search"
    bl_label = "Clear Search"
    bl_description = "Clear the tool search filter"

    def execute(self, context):
        context.scene.arantools_search = ""
        return {'FINISHED'}


class ARANTOOLS_PT_main(Panel):
    bl_label = "Aran Tools"
    bl_idname = "ARANTOOLS_PT_main"
    bl_space_type = _PANEL_SPACE
    bl_region_type = _PANEL_REGION
    bl_category = _PANEL_CATEGORY

    def draw(self, context):
        layout = self.layout
        scene = context.scene

        # ── Search box ────────────────────────────────────────────────────
        row = layout.row(align=True)
        row.prop(scene, 'arantools_search', text="", icon='VIEWZOOM')
        if scene.arantools_search:
            row.operator("arantools.clear_search", text="", icon='X')

        search = scene.arantools_search.strip().lower()
        if search:
            self._draw_search(layout, scene, context, search)
            return

        # ── Active tab name ───────────────────────────────────────────────
        tab_info = {
            'RIGGING':      ('Rigging',      'ARMATURE_DATA'),
            'WEIGHT':       ('Weight Tools', 'MOD_VERTEX_WEIGHT'),
            'ORGANIZATION': ('Organization', 'OUTLINER'),
            'EXPORT':       ('Export',       'EXPORT'),
            'NAMING':       ('Naming',       'SORTALPHA'),
            'ANIMATION':    ('Animation',    'ANIM'),
        }
        tab_name, tab_icon = tab_info[scene.arantools_active_tab]
        layout.label(text=tab_name, icon=tab_icon)
        layout.separator(factor=0.5)

        main_row = layout.row()

        # ── Vertical icon tabs on the left with color coding ──────────────
        tab_col = main_row.column(align=True)
        tab_col.scale_x = 1.3
        tab_col.scale_y = 1.3

        tabs = [
            ('RIGGING',      'ARMATURE_DATA',     'COLORSET_11_VEC'),
            ('WEIGHT',       'MOD_VERTEX_WEIGHT', 'COLORSET_02_VEC'),
            ('ORGANIZATION', 'OUTLINER',          'COLORSET_06_VEC'),
            ('EXPORT',       'EXPORT',            'COLORSET_12_VEC'),
            ('NAMING',       'SORTALPHA',         'COLORSET_03_VEC'),
            ('ANIMATION',    'ANIM',              'COLORSET_05_VEC'),
        ]

        for tab_value, tab_icon, color_icon in tabs:
            row = tab_col.row(align=True)
            color_sub = row.row()
            color_sub.scale_x = 0.4
            color_sub.label(text="", icon=color_icon)
            row.prop_enum(scene, 'arantools_active_tab', tab_value, text="", icon=tab_icon)

        tab_col.separator()
        tab_col.operator("arantools.reload_addon", text="", icon='FILE_REFRESH')

        # ── Content area — collapsible tool sections ──────────────────────
        content_col = main_row.column()
        active_tab = scene.arantools_active_tab
        for tool_id, tool_name, tool_desc, tool_tab, tool_icon, draw_method, is_small in _TOOL_REGISTRY:
            if tool_tab != active_tab:
                continue
            box, expanded = self._section(content_col, scene, tool_id, tool_name, tool_icon, is_small)
            if expanded:
                getattr(self, draw_method)(box, context)

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _section(self, layout, scene, tool_id, label, icon, is_small=False):
        """Draw a section header. Returns (box, is_expanded).

        Small tools are always expanded with no collapse toggle.
        Collapsible tools show a tooltip (the tool description) on the toggle.
        """
        box = layout.box()
        row = box.row(align=True)
        if is_small:
            row.label(text=label, icon=icon)
            return box, True
        prop_name = f'arantools_open_{tool_id}'
        expanded = getattr(scene, prop_name)
        row.prop(scene, prop_name, text="",
                 icon='TRIA_DOWN' if expanded else 'TRIA_RIGHT', emboss=False)
        row.label(text=label, icon=icon)
        return box, expanded

    def _draw_search(self, layout, scene, context, search):
        """Draw filtered tools from all tabs matching the search term."""
        tab_labels = {
            'RIGGING':      ('Rigging',      'ARMATURE_DATA'),
            'WEIGHT':       ('Weight Tools', 'MOD_VERTEX_WEIGHT'),
            'ORGANIZATION': ('Organization', 'OUTLINER'),
            'EXPORT':       ('Export',       'EXPORT'),
            'NAMING':       ('Naming',       'SORTALPHA'),
            'ANIMATION':    ('Animation',    'ANIM'),
        }
        found = False
        current_tab = None
        for tool_id, tool_name, tool_desc, tool_tab, tool_icon, draw_method, is_small in _TOOL_REGISTRY:
            if search not in tool_name.lower() and search not in tool_desc.lower():
                continue
            found = True
            if tool_tab != current_tab:
                current_tab = tool_tab
                tab_name, tab_icon = tab_labels[tool_tab]
                layout.label(text=tab_name, icon=tab_icon)
            box, expanded = self._section(layout, scene, tool_id, tool_name, tool_icon, is_small)
            if expanded:
                getattr(self, draw_method)(box, context)
        if not found:
            layout.label(text="No tools match your search.", icon='INFO')

    # ── Individual tool content ───────────────────────────────────────────────

    def _draw_t_select_deform(self, layout, context):
        layout.operator("arantools.select_deform_bones", icon='BONE_DATA')

    def _draw_t_feather_rigger(self, layout, context):
        fr = context.scene.arantools_feather_rig
        col = layout.column(align=True)
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

    def _draw_t_join_bind(self, layout, context):
        props = context.scene.arantools_adv_rigging
        col = layout.column(align=True)
        col.label(text="Step 1 — Join", icon='OBJECT_DATAMODE')
        col.label(text="Select costume meshes in viewport first", icon='MOUSE_LMB')
        col.prop(props, "target_collection", text="Output Collection")
        col.operator("arantools.join_targets", text="Join Selected into One Mesh", icon='OBJECT_DATAMODE')
        col.separator()
        col.label(text="Step 2 — Bind", icon='ARMATURE_DATA')
        col.label(text="Make the joined mesh active first", icon='MOUSE_LMB')
        col.prop(props, "source_mesh", text="Rigged Body")
        col.prop(props, "mapping_method", text="Vertex Mapping")
        col.operator("arantools.bind_and_transfer", text="Bind & Copy Weights", icon='ARMATURE_DATA')

    def _draw_t_weight_pointer(self, layout, context):
        props = context.scene.arantools_adv_rigging
        col = layout.column(align=True)
        col.prop(props, "sharp_edge_pointer_method", text="Pointer By")
        col.prop(props, "chain_length")
        col.operator("arantools.weight_from_pointer", icon='CURVE_PATH')
        col.operator("arantools.direct_arp_bind", icon='LINKED')

    def _draw_t_island_flatten(self, layout, context):
        props = context.scene.arantools_island_flatten
        col = layout.column(align=True)
        col.prop(props, "blend", slider=True)
        col.prop(props, "only_selected")
        col.separator()
        col.operator("arantools.island_flatten_weights", icon='MOD_SMOOTH')

    def _draw_t_smart_transfer(self, layout, context):
        props = context.scene.arantools_smart_transfer
        col = layout.column(align=True)
        col.prop(props, "source_mesh")
        col.prop(props, "interp_mode", text="")
        col.prop(props, "clean_empty")
        col.operator("arantools.smart_weight_transfer", icon='ARMATURE_DATA')

    def _draw_t_sync_vgroups(self, layout, context):
        layout.label(text="Active mesh must have an armature.", icon='INFO')
        layout.operator("arantools.sync_vertex_groups", icon='FILE_REFRESH')

    def _draw_t_unify_island(self, layout, context):
        props = context.scene.arantools_unify_weights
        col = layout.column(align=True)
        col.prop(props, "blend", slider=True)
        col.prop(props, "only_selected")
        col.operator("arantools.unify_island_weights", icon='FULLSCREEN_ENTER')

    def _draw_t_island_copy(self, layout, context):
        props = context.scene.arantools_island_copy
        col = layout.column(align=True)
        col.prop(props, "base_vertex_method", text="Base By")
        if props.base_vertex_method == 'ATTRIBUTE':
            col.prop(props, "base_attribute_name")
        col.prop(props, "blend_factor", slider=True)
        col.prop(props, "only_selected")
        col.operator("arantools.island_weight_copy", icon='FORWARD')

    def _draw_t_contact_flood(self, layout, context):
        props = context.scene.arantools_contact_flood
        col = layout.column(align=True)
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

    def _draw_t_batch_rig(self, layout, context):
        props = context.scene.arantools_rt_props
        col = layout.column(align=True)
        col.prop(props, "source_collection")
        col.prop(props, "gt_collection")
        col.prop(props, "target_collection")
        col.separator()
        col.prop(props, "apply_modifiers")
        col.prop(props, "transfer_method", text="")
        col.separator()
        col.operator("arantools.rt_populate_list", icon='FILE_REFRESH')
        if props.binding_list:
            layout.label(text="Source -> Ground Truth:")
            layout.template_list(
                "ARANTOOLS_UL_RT_BindingList", "",
                props, "binding_list",
                props, "binding_index",
                rows=4,
            )
            row = layout.row()
            row.enabled = bool(props.target_collection)
            row.operator("arantools.rt_execute_rigging", icon='PLAY')
            if not props.target_collection:
                layout.label(text="Select a Target Collection!", icon='ERROR')

    def _draw_t_coll_baker(self, layout, context):
        bake_props = context.scene.arantools_bake_scene
        col = layout.column(align=True)
        col.prop(bake_props, "source_collection")
        col.prop(bake_props, "target_collection")
        if bake_props.source_collection:
            layout.separator()
            mesh_box = layout.box()
            mesh_box.label(text="Set Target Names:", icon='OUTLINER_OB_MESH')
            for obj in bake_props.source_collection.objects:
                if obj.type != 'MESH':
                    continue
                row = mesh_box.row(align=True)
                row.label(text=obj.name, icon='MESH_DATA')
                row.prop(obj.arantools_bake_props, "target_name", text="")
            row = layout.row()
            row.scale_y = 1.4
            row.operator("arantools.collection_bake", icon='RENDER_STILL')
        else:
            layout.label(text="Select a Source Collection.", icon='INFO')

    def _draw_t_arp_export(self, layout, context):
        from . import export as _export
        props = context.scene.arantools_arp_export
        col = layout.column(align=True)
        col.prop(props, "target_armature")
        col.prop(props, "export_folder")
        col.separator()

        # ── Mesh naming ───────────────────────────────────────────────────
        col.label(text="Mesh Naming:", icon='OBJECT_DATAMODE')
        col.prop(props, "remove_str", text="Remove")
        row = col.row(align=True)
        row.prop(props, "prefix_str", text="Prefix")
        row.prop(props, "suffix_str", text="Suffix")

        # ── Mesh preview ──────────────────────────────────────────────────
        selected_meshes = [
            obj for obj in context.selected_objects
            if obj != props.target_armature and obj.type == 'MESH'
        ]
        if selected_meshes:
            preview_box = layout.box()
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
            layout.label(text="Select meshes to preview.", icon='INFO')

        col2 = layout.column(align=True)
        col2.separator()

        # ── Anim naming ───────────────────────────────────────────────────
        col2.label(text="Anim Naming:", icon='ANIM')
        col2.prop(props, "anim_remove_str", text="Remove")
        row = col2.row(align=True)
        row.prop(props, "anim_prefix_str", text="Prefix")
        row.prop(props, "anim_suffix_str", text="Suffix")
        col2.prop(props, "anim_only_active")

        # ── Anim preview ──────────────────────────────────────────────────
        arm = props.target_armature
        if arm is None:
            layout.label(text="Pick an armature to preview animations.", icon='INFO')
            anim_actions = []
        elif props.anim_only_active:
            if arm.animation_data and arm.animation_data.action:
                anim_actions = [arm.animation_data.action]
            else:
                layout.label(text="Armature has no active action.", icon='INFO')
                anim_actions = []
        else:
            anim_actions = list(_export._iter_armature_actions(arm))
            if not anim_actions:
                layout.label(text="No armature actions found.", icon='INFO')

        if anim_actions:
            preview_box = layout.box()
            act = anim_actions[0]
            final_anim = _export._get_anim_filename(act.name, props)
            preview_box.label(text="Org:  " + act.name, icon='ACTION')
            if not final_anim:
                preview_box.label(text="New:  [EMPTY NAME]", icon='ERROR')
            else:
                preview_box.label(text="New:  " + final_anim + ".fbx", icon='FORWARD')
            if len(anim_actions) > 1:
                preview_box.label(text="+ " + str(len(anim_actions) - 1) + " more action(s)")

        # ── Export buttons ────────────────────────────────────────────────
        row = layout.row(align=True)
        row.scale_y = 1.4
        row.enabled = bool(props.target_armature)
        row.operator("arantools.arp_batch_export", text="Export Meshes", icon='EXPORT')
        row.operator("arantools.arp_anim_export",  text="Animations",    icon='ANIM')
        layout.label(text="Requires Auto-Rig Pro", icon='ERROR')

    def _draw_t_export_sets(self, layout, context):
        from . import export as _export
        props = context.scene.arantools_arp_export
        col = layout.column(align=True)

        # ── Dedicated folder for this tool ────────────────────────────────
        col.prop(props, "export_sets_folder", text="Folder")
        if props.target_armature is None:
            col.label(text="Set Armature in ARP Batch Export above.",
                      icon='ERROR')
        else:
            col.label(text=f"Armature: {props.target_armature.name}",
                      icon='ARMATURE_DATA')
        col.separator()

        # ── Add buttons ───────────────────────────────────────────────────
        add_row = col.row(align=True)
        add_row.scale_y = 1.2
        add_row.operator("arantools.expset_add_from_selection",
                         text="+ From Selection", icon='RESTRICT_SELECT_OFF')
        add_row.operator("arantools.expset_add",
                         text="+ Empty", icon='NEWFOLDER')

        if not props.export_sets:
            col.separator()
            col.label(text="No sets yet. Select meshes → '+ From Selection'.",
                      icon='INFO')
            return

        # ── Duplicate-filename detector ───────────────────────────────────
        name_counts = {}
        for s in props.export_sets:
            nm = _export._clean_relpath(s.filename)
            if nm:
                name_counts[nm] = name_counts.get(nm, 0) + 1

        # ── One box per set ───────────────────────────────────────────────
        for i, eset in enumerate(props.export_sets):
            box = layout.box()
            header = box.row(align=True)
            header.prop(eset, "expanded", text="", emboss=False,
                        icon='TRIA_DOWN' if eset.expanded else 'TRIA_RIGHT')
            header.prop(eset, "filename", text="")
            op = header.operator("arantools.expset_remove",
                                 text="", icon='X')
            op.index = i

            if not eset.expanded:
                continue

            body = box.column(align=True)

            # Preview: cleaned filename
            clean = _export._clean_relpath(eset.filename)
            if not clean:
                body.label(text="Empty filename — set will be skipped.",
                           icon='ERROR')
            elif name_counts.get(clean, 0) > 1:
                body.label(text=f"Org:  {eset.filename}", icon='OBJECT_DATAMODE')
                body.label(text=f"New:  {clean}.fbx  (duplicate!)", icon='ERROR')
            else:
                body.label(text=f"Org:  {eset.filename}", icon='OBJECT_DATAMODE')
                body.label(text=f"New:  {clean}.fbx", icon='FORWARD')

            # Mesh summary
            valid = [e.obj for e in eset.meshes if e.obj is not None]
            missing = len(eset.meshes) - len(valid)
            if valid:
                names = ", ".join(m.name for m in valid[:6])
                if len(valid) > 6:
                    names += f" … (+{len(valid) - 6})"
                body.label(text=f"Meshes ({len(valid)}): {names}",
                           icon='MESH_DATA')
            else:
                body.label(text="No meshes yet — pick selection.", icon='INFO')
            if missing:
                body.label(text=f"{missing} deleted reference(s).", icon='ERROR')

            # Mesh management buttons
            mbtn = body.row(align=True)
            for op_id, text, icon in (
                ("arantools.expset_set_meshes", "Set",    'IMPORT'),
                ("arantools.expset_add_meshes", "Add",    'ADD'),
                ("arantools.expset_clear",      "Clear",  'X'),
                ("arantools.expset_select",     "Select", 'RESTRICT_SELECT_OFF'),
            ):
                op = mbtn.operator(op_id, text=text, icon=icon)
                op.index = i

            # Per-set export
            ex_row = body.row()
            ex_row.scale_y = 1.2
            ex_row.enabled = bool(valid) and bool(clean) \
                             and props.target_armature is not None
            op = ex_row.operator("arantools.expset_export",
                                 text="Export This Set", icon='EXPORT')
            op.index = i

        # ── Batch button ──────────────────────────────────────────────────
        layout.separator()
        big = layout.row()
        big.scale_y = 1.4
        big.enabled = props.target_armature is not None
        big.operator("arantools.expset_export_all",
                     text="Export All Sets", icon='EXPORT')
        layout.label(text="Overwrites existing files. Requires Auto-Rig Pro.",
                     icon='INFO')

        # ── Animation Export sub-section ──────────────────────────────────
        layout.separator(factor=2.0)
        anim_header = layout.row()
        anim_header.label(text="Animation Export", icon='ANIM')

        acol = layout.column(align=True)
        acol.prop(props, "anim_export_folder", text="Folder")
        acol.prop(props, "anim_export_prefix", text="Prefix")

        if props.target_armature is None:
            acol.label(text="Pick an armature above first.", icon='ERROR')
            return

        # Refresh / select-all / select-none
        list_btns = acol.row(align=True)
        list_btns.operator("arantools.anim_export_refresh",
                           text="Refresh", icon='FILE_REFRESH')
        list_btns.operator("arantools.anim_export_select_all", text="All")
        list_btns.operator("arantools.anim_export_select_none", text="None")

        if not props.anim_export_items:
            acol.label(text="Click Refresh to list armature actions.",
                       icon='INFO')
            return

        layout.template_list(
            "ARANTOOLS_UL_AnimExportList", "",
            props, "anim_export_items",
            props, "anim_export_index",
            rows=6,
        )

        # ── Single-line preview + button for full popup ───────────────────
        enabled_items = [it for it in props.anim_export_items
                         if it.enabled and it.action is not None]
        prev_col = layout.column(align=True)
        if enabled_items:
            first = enabled_items[0]
            base = bpy.path.clean_name(props.anim_export_prefix + first.action.name)
            if base:
                prev_col.label(text=f"e.g.  {base}.fbx   "
                                    f"({len(enabled_items)} total)",
                               icon='FILE')
            else:
                prev_col.label(text="[EMPTY FILENAME]", icon='ERROR')
            prev_col.operator("arantools.anim_export_preview_paths",
                              text="Preview All Paths", icon='VIEWZOOM')
        else:
            prev_col.label(text="No actions ticked.", icon='INFO')

        big2 = layout.row()
        big2.scale_y = 1.4
        big2.enabled = bool(enabled_items)
        big2.operator("arantools.anim_export_run",
                      text="Export Selected Animations", icon='EXPORT')
        layout.label(text="Filename = Prefix + Action name. Overwrites existing.",
                     icon='INFO')

    def _draw_t_mod_sync(self, layout, context):
        props = context.scene.arantools_mod_sync
        col   = layout.column(align=True)

        # ── Source object ──────────────────────────────────────────────────
        col.label(text="Source Object:", icon='OBJECT_DATA')
        row = col.row(align=True)
        row.prop(props, "source_object", text="")
        row.operator("arantools.modsync_save_stack",
                     text="Save Stack", icon='IMPORT')
        col.separator()

        # ── Modifier checklist ─────────────────────────────────────────────
        if props.modifier_items:
            col.label(text="Modifiers to Copy:", icon='MODIFIER')
            mod_box = col.box()
            mod_col = mod_box.column(align=True)
            for item in props.modifier_items:
                row = mod_col.row(align=True)
                row.prop(item, "enabled", text="")
                row.label(text=item.mod_name, icon='MODIFIER_DATA')
                row.label(text=item.mod_type)
            col.separator()

            col.prop(props, "replace_all")
            col.separator()
            copy_row = col.row(align=True)
            copy_row.scale_y = 1.3
            if props.replace_all:
                copy_row.operator("arantools.modsync_copy_to_selected",
                                  text="Replace on Selected", icon='TRASH')
            else:
                copy_row.operator("arantools.modsync_copy_to_selected",
                                  text="Copy to Selected", icon='COPYDOWN')
        else:
            col.label(text="Pick a source object, then Save Stack.", icon='INFO')

        col.separator()

        # ── Last targets / reapply ─────────────────────────────────────────
        # Only show when a stack is loaded AND targets exist — the button
        # silently fails without both, so don't show it in a broken state.
        if props.modifier_items:
            if props.last_targets:
                col.label(
                    text=f"Last selection  ({len(props.last_targets)} object(s)):",
                    icon='RESTRICT_SELECT_OFF',
                )
                tgt_box = col.box()
                tgt_col = tgt_box.column(align=True)
                for entry in props.last_targets:
                    exists = bpy.data.objects.get(entry.obj_name) is not None
                    tgt_col.label(
                        text=entry.obj_name,
                        icon='OBJECT_DATA' if exists else 'ERROR',
                    )
                col.separator()
                col.operator("arantools.modsync_reapply_last", icon='FILE_REFRESH')
            else:
                col.label(text="No previous selection saved.", icon='INFO')

    def _draw_t_seq_namer(self, layout, context):
        props = context.scene.arantools_seq_namer
        col = layout.column(align=True)
        col.prop(props, "base_name", text="Base Name")
        row = col.row(align=True)
        row.prop(props, "padding", text="Digits")
        col.separator()
        col.prop(props, "replace_existing")
        col.separator()
        col.operator("arantools.sequence_name_objects", icon='LINENUMBERS_ON')

    def _draw_t_bone_renamer(self, layout, context):
        scene = context.scene
        col = layout.column(align=True)
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

    def _draw_t_anim_org(self, layout, context):
        from . import animation as _anim
        props = context.scene.arantools_anim_org
        col = layout.column(align=True)

        col.prop(props, "armature")
        if props.armature is None:
            col.label(text="Pick an armature to begin.", icon='INFO')
            return

        arm = props.armature
        col.separator()

        # ── Active action status ─────────────────────────────────────────
        if arm.animation_data and arm.animation_data.action:
            act = arm.animation_data.action
            info = col.row(align=True)
            info.label(text="Active:  " + act.name, icon='ACTION')
            info.operator("arantools.animorg_sync_timeline", text="", icon='TIME')
            parsed = _anim._parse_duration(act.name)
            if parsed is None:
                col.label(text="Name has no '_NNN' duration suffix.", icon='ERROR')
        else:
            col.label(text="No active action on this armature.", icon='INFO')

        col.separator()

        # ── Action list ──────────────────────────────────────────────────
        col.label(text="Actions:", icon='OUTLINER_DATA_GP_LAYER')
        col.template_list(
            "ARANTOOLS_UL_AnimOrg_Actions", "",
            bpy.data, "actions",
            props, "action_index",
            rows=8,
        )
        filt = col.row(align=True)
        filt.prop(props, "only_armature_actions", toggle=True)
        filt.prop(props, "auto_sync_timeline", toggle=True)

        col.separator()

        # ── New action ───────────────────────────────────────────────────
        col.label(text="Create New Action:", icon='ADD')
        col.prop(props, "new_action_name", text="")
        parsed_new = _anim._parse_duration(props.new_action_name)
        if parsed_new is not None:
            col.label(text=f"Timeline will be 1 → {parsed_new}", icon='TIME')
        else:
            col.label(text="Append '_NNN' to set timeline (e.g. _400).", icon='INFO')
        create_row = col.row()
        create_row.scale_y = 1.3
        create_row.operator("arantools.animorg_new_action",
                            text="Create & Activate", icon='ADD')

    def _draw_t_spring_smooth(self, layout, context):
        props = context.scene.arantools_curve_smooth
        col   = layout.column(align=True)

        # ── Channels + per-axis locks ─────────────────────────────────────
        col.label(text="Channels  /  Axes:", icon='DECORATE_KEYFRAME')

        for chan_prop, axes_prop, label, icon in (
            ('apply_location', 'location_axes', 'Location', 'OBJECT_ORIGIN'),
            ('apply_rotation', 'rotation_axes', 'Rotation', 'ORIENTATION_GIMBAL'),
            ('apply_scale',    'scale_axes',    'Scale',    'OBJECT_DATAMODE'),
        ):
            row = col.row(align=True)
            row.prop(props, chan_prop, text=label, toggle=True, icon=icon)
            axis_row = row.row(align=True)
            axis_row.enabled = getattr(props, chan_prop)
            axis_row.prop(props, axes_prop, text="", toggle=True)
        col.separator()

        # ── Spring parameters ─────────────────────────────────────────────
        col.label(text="Spring:", icon='IPO_BOUNCE')
        col.prop(props, "stiffness", slider=True)
        col.prop(props, "damping",   slider=True)
        col.prop(props, "blend",     slider=True, text="Strength")
        col.separator()

        # ── Stop preservation ─────────────────────────────────────────────
        col.label(text="Stop Points:", icon='SNAP_MIDPOINT')
        col.prop(props, "preserve_stops")
        sub = col.column(align=True)
        sub.enabled = props.preserve_stops
        sub.prop(props, "stop_tolerance")
        col.separator()

        # ── Advanced ──────────────────────────────────────────────────────
        col.prop(props, "substeps")
        col.separator()

        # ── Decimate (uses Blender's graph.decimate) ──────────────────────
        col.label(text="Decimate:", icon='SHARPCURVE')
        col.prop(props, "decimate_after")
        sub = col.column(align=True)
        sub.enabled = props.decimate_after
        sub.prop(props, "decimate_mode", text="")
        if props.decimate_mode == 'ERROR':
            sub.prop(props, "decimate_error")
        else:
            sub.prop(props, "decimate_ratio", slider=True)
        col.separator()

        # ── Apply ─────────────────────────────────────────────────────────
        if context.mode != 'POSE':
            col.label(text="Enter Pose Mode to run.", icon='ERROR')
        elif not (context.selected_pose_bones or []):
            col.label(text="Select pose bones first.", icon='INFO')
        apply_row = col.row()
        apply_row.scale_y = 1.4
        apply_row.operator("arantools.spring_smooth_curves",
                           text="Spring Smooth", icon='IPO_BOUNCE')
        col.operator("arantools.decimate_smooth_curves",
                     text="Decimate Only", icon='SHARPCURVE')

    def _draw_t_noise_bones(self, layout, context):
        scene = context.scene
        col   = layout.column(align=True)

        # ── Timing ────────────────────────────────────────────────────────
        col.label(text="Timing:", icon='TIME')
        col.prop(scene, 'arantools_frame_length',   text='Last Frame')
        col.prop(scene, 'arantools_blend_duration', text='Blend In/Out')
        col.separator()

        # ── Rotation ──────────────────────────────────────────────────────
        col.label(text="Rotation:", icon='ORIENTATION_GIMBAL')
        col.prop(scene, 'arantools_rotation_strength', text='Strength', slider=True)
        col.prop(scene, 'arantools_rotation_scale',
                 text='Time Scale  (↑ = slower)', slider=True)
        col.separator()

        # ── Location ──────────────────────────────────────────────────────
        col.label(text="Location:", icon='OBJECT_ORIGIN')
        col.prop(scene, 'arantools_location_strenght', text='Strength', slider=True)
        col.prop(scene, 'arantools_location_scale',
                 text='Time Scale  (↑ = slower)', slider=True)
        col.separator()

        # ── Advanced ──────────────────────────────────────────────────────
        adv_box = col.box()
        adv_row = adv_box.row(align=True)
        adv_row.prop(scene, 'arantools_advanced_options',
                     icon='TRIA_DOWN' if scene.arantools_advanced_options else 'TRIA_RIGHT',
                     emboss=False, text="Advanced")

        if scene.arantools_advanced_options:
            sub = adv_box.column(align=True)

            # ── Rotation per-axis ──────────────────────────────────────
            sub.label(text="Rotation Axis:", icon='ORIENTATION_GIMBAL')
            row = sub.row(align=True)
            row.label(text="Strength")
            row.prop(scene, 'arantools_rotation_axis_multipliers',
                     text='', slider=True)
            row = sub.row(align=True)
            row.label(text="Scale")
            row.prop(scene, 'arantools_rotation_axis_multiplier_speed',
                     text='', slider=True)
            sub.separator()

            # ── Location per-axis ──────────────────────────────────────
            sub.label(text="Location Axis:", icon='OBJECT_ORIGIN')
            row = sub.row(align=True)
            row.label(text="Strength")
            row.prop(scene, 'arantools_location_axis_multipliers',
                     text='', slider=True)
            row = sub.row(align=True)
            row.label(text="Scale")
            row.prop(scene, 'arantools_location_axis_multiplier_speed',
                     text='', slider=True)
            sub.separator()

            # ── Divisors ───────────────────────────────────────────────
            sub.label(text="Divisors:", icon='DRIVER_TRANSFORM')
            sub.prop(scene, 'arantools_location_strength_divisor',
                     text='Loc Strength ÷')
            sub.prop(scene, 'arantools_scale_divisor',
                     text='Scale ÷')

        col.separator()

        # ── Apply ─────────────────────────────────────────────────────────
        col.label(text="Apply:", icon='FORCE_TURBULENCE')
        apply_row = col.row(align=True)
        apply_row.scale_y = 1.3
        apply_row.operator("arantools.apply_noise_rotation", icon='ORIENTATION_GIMBAL')
        apply_row.operator("arantools.apply_noise_location", icon='OBJECT_ORIGIN')
        apply_row.operator("arantools.apply_noise_both",     icon='FORCE_TURBULENCE')
        col.separator()
        col.operator("arantools.remove_noise", icon='X')


# ============================================================================
# Registration
# ============================================================================

classes = [
    ARANTOOLS_OT_reload_addon,
    ARANTOOLS_OT_clear_search,
    ARANTOOLS_PT_main,
]


def register():
    rigging.register()
    animation.register()
    naming.register()
    weight_tools.register()
    organization.register()
    export.register()
    island_flatten.register()
    modifier_sync.register()

    bpy.types.Scene.arantools_active_tab = bpy.props.EnumProperty(
        items=[
            ('RIGGING',       "", "Rigging",       'ARMATURE_DATA',      0),
            ('WEIGHT',        "", "Weight Tools",   'MOD_VERTEX_WEIGHT',  1),
            ('ORGANIZATION',  "", "Organization",   'OUTLINER',           2),
            ('EXPORT',        "", "Export",         'EXPORT',             3),
            ('NAMING',        "", "Naming",         'SORTALPHA',          4),
            ('ANIMATION',     "", "Animation",      'ANIM',               5),
        ],
        default='RIGGING',
    )

    bpy.types.Scene.arantools_search = bpy.props.StringProperty(
        name="Search Tools",
        description="Filter tools by name or description",
        default="",
        options={'TEXTEDIT_UPDATE'},
    )

    _tool_desc_map = {entry[0]: entry[2] for entry in _TOOL_REGISTRY}
    for tool_id in _OPEN_TOOL_IDS:
        setattr(bpy.types.Scene, f'arantools_open_{tool_id}',
                bpy.props.BoolProperty(
                    default=False,
                    description=_tool_desc_map[tool_id],
                ))

    for cls in classes:
        bpy.utils.register_class(cls)


def unregister():
    del bpy.types.Scene.arantools_active_tab
    del bpy.types.Scene.arantools_search
    for tool_id in _OPEN_TOOL_IDS:
        delattr(bpy.types.Scene, f'arantools_open_{tool_id}')
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
    modifier_sync.unregister()
    island_flatten.unregister()
    export.unregister()
    organization.unregister()
    weight_tools.unregister()
    naming.unregister()
    animation.unregister()
    rigging.unregister()
