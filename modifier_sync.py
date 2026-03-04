import bpy
from bpy.types import Operator


# ============================================================================
# Helpers
# ============================================================================

# Properties that are type-level constants — never copy these
_SKIP_PROPS = frozenset({
    'rna_type', 'type', 'name', 'is_active', 'show_expanded',
})


def _copy_geonodes_inputs(src_mod, dst_mod):
    """Copy Geometry Nodes socket input values between two NODES modifiers.

    In Blender 4.0+ socket values live on the modifier as keyed properties
    (modifier["Socket_X"] = value). We read the identifiers from the node
    group's interface tree so we only touch real input sockets.

    Falls back to the older ng.inputs API for Blender 3.x.
    """
    if src_mod.type != 'NODES' or not src_mod.node_group:
        return
    if not dst_mod.node_group:
        return  # node_group wasn't copied (different type?), nothing to do

    ng = src_mod.node_group

    # ── Blender 4.0+ path ─────────────────────────────────────────────────
    if hasattr(ng, 'interface'):
        try:
            for item in ng.interface.items_tree:
                if getattr(item, 'in_out', None) == 'INPUT':
                    ident = item.identifier
                    try:
                        dst_mod[ident] = src_mod[ident]
                    except (KeyError, TypeError):
                        pass
        except Exception:
            pass
        return

    # ── Blender 3.x fallback ──────────────────────────────────────────────
    try:
        for inp in ng.inputs:
            ident = inp.identifier
            try:
                dst_mod[ident] = src_mod[ident]
            except (KeyError, TypeError):
                pass
    except Exception:
        pass


def _copy_mod_props(src_mod, dst_mod):
    """Copy all non-read-only RNA properties from src_mod to dst_mod,
    then copy Geometry Nodes socket values if applicable.
    Both must be the same modifier type."""
    if src_mod.type != dst_mod.type:
        return

    # ── Standard RNA properties ────────────────────────────────────────────
    for prop in src_mod.bl_rna.properties:
        ident = prop.identifier
        if ident in _SKIP_PROPS:
            continue
        if prop.is_readonly:
            continue
        try:
            val = getattr(src_mod, ident)
            setattr(dst_mod, ident, val)
        except Exception:
            pass

    # ── Geometry Nodes socket values (not exposed via bl_rna) ─────────────
    _copy_geonodes_inputs(src_mod, dst_mod)


# ============================================================================
# Property groups
# ============================================================================

class ARANTOOLS_PG_ModItem(bpy.types.PropertyGroup):
    """One modifier entry in the saved stack checklist."""
    mod_name: bpy.props.StringProperty(name="Modifier Name")
    mod_type: bpy.props.StringProperty(name="Modifier Type")
    enabled:  bpy.props.BoolProperty(
        name="Include",
        description="Include this modifier when copying to targets",
        default=True,
    )


class ARANTOOLS_PG_SavedObjName(bpy.types.PropertyGroup):
    """A single saved target object name."""
    obj_name: bpy.props.StringProperty(name="Object Name")


class ARANTOOLS_PG_ModSync(bpy.types.PropertyGroup):
    source_object: bpy.props.PointerProperty(
        type=bpy.types.Object,
        name="Source",
        description="Object whose modifier stack will be saved and synced from",
    )
    modifier_items: bpy.props.CollectionProperty(type=ARANTOOLS_PG_ModItem)
    last_targets:   bpy.props.CollectionProperty(type=ARANTOOLS_PG_SavedObjName)
    replace_all: bpy.props.BoolProperty(
        name="Replace All Modifiers",
        description=(
            "When ON: remove every existing modifier from each target object "
            "before adding the checked ones (target ends up with only the "
            "checked modifiers, in source order).\n"
            "When OFF: update existing mods in-place and add any that are "
            "missing, leaving unrelated mods untouched"
        ),
        default=False,
    )


# ============================================================================
# Save stack operator
# ============================================================================

class ARANTOOLS_OT_ModSync_SaveStack(Operator):
    """Read the modifier stack from the source object and save it for review"""
    bl_idname  = "arantools.modsync_save_stack"
    bl_label   = "Save Stack"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        props = context.scene.arantools_mod_sync
        src   = props.source_object

        if not src:
            self.report({'ERROR'}, "Select a Source Object first.")
            return {'CANCELLED'}

        props.modifier_items.clear()
        for mod in src.modifiers:
            item          = props.modifier_items.add()
            item.mod_name = mod.name
            item.mod_type = mod.type
            item.enabled  = True

        self.report(
            {'INFO'},
            f"Saved {len(props.modifier_items)} modifier(s) from '{src.name}'.",
        )
        return {'FINISHED'}


# ============================================================================
# Core copy logic (shared by Copy and Reapply)
# ============================================================================

def _do_copy(context, props, targets):
    """Copy checked modifiers from source to each target object.

    Replace-all mode (props.replace_all = True):
      All existing modifiers are removed from the target, then the checked
      modifiers are added fresh in source order. No reordering step needed.

    Merge mode (props.replace_all = False, default):
      - Modifier with same name already exists → update values in-place
        (modifier keeps its current stack position).
      - Modifier does not exist → add it, then reorder so all synced mods
        appear in the same relative order as on the source (unchecked mods
        on the target stay where they are).

    Returns (count_succeeded, error_message_or_None).
    """
    src = props.source_object
    if not src:
        return 0, "No source object set."
    if not props.modifier_items:
        return 0, "No modifier stack saved. Click 'Save Stack' first."

    checked_items = [item for item in props.modifier_items if item.enabled]
    if not checked_items:
        return 0, "No modifiers are checked."

    checked_names = [item.mod_name for item in checked_items]

    old_active = context.view_layer.objects.active
    count      = 0

    for obj in targets:
        if obj is src:
            continue

        if props.replace_all:
            # ── Replace mode: wipe target stack, rebuild from checked list ──
            obj.modifiers.clear()
            for item in checked_items:
                src_mod = src.modifiers.get(item.mod_name)
                if src_mod is None:
                    continue
                new_mod = obj.modifiers.new(name=item.mod_name, type=src_mod.type)
                if new_mod:
                    _copy_mod_props(src_mod, new_mod)
            # Mods were added in source order → no reordering needed

        else:
            # ── Merge mode: update existing, add missing ─────────────────
            for item in checked_items:
                src_mod = src.modifiers.get(item.mod_name)
                if src_mod is None:
                    continue  # source mod was removed after Save Stack

                dst_mod = obj.modifiers.get(item.mod_name)
                if dst_mod is not None:
                    # Exists → update values in-place
                    if dst_mod.type == src_mod.type:
                        _copy_mod_props(src_mod, dst_mod)
                else:
                    # Missing → add and populate
                    new_mod = obj.modifiers.new(name=item.mod_name, type=src_mod.type)
                    if new_mod:
                        _copy_mod_props(src_mod, new_mod)

            # ── Reorder: ensure checked mods appear in source order ────────
            # Walk checked names in source order; if a mod sits before the
            # previous one in the target stack, move it right after it.
            context.view_layer.objects.active = obj

            prev_name = None
            for name in checked_names:
                if name not in obj.modifiers:
                    continue
                # Re-read stack each pass — moving changes indices
                stack   = [m.name for m in obj.modifiers]
                cur_idx = stack.index(name)

                if prev_name is not None and prev_name in stack:
                    prev_idx = stack.index(prev_name)
                    if cur_idx < prev_idx:
                        try:
                            bpy.ops.object.modifier_move_to_index(
                                modifier=name, index=prev_idx)
                        except Exception:
                            pass

                prev_name = name

        count += 1

    context.view_layer.objects.active = old_active
    return count, None


# ============================================================================
# Copy-to-selected operator
# ============================================================================

class ARANTOOLS_OT_ModSync_CopyToSelected(Operator):
    """Copy the checked modifiers to all other selected objects.
Saves this selection so you can reapply later"""
    bl_idname  = "arantools.modsync_copy_to_selected"
    bl_label   = "Copy to Selected"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        props   = context.scene.arantools_mod_sync
        src     = props.source_object
        targets = [o for o in context.selected_objects if o is not src]

        if not targets:
            self.report({'WARNING'},
                        "Select at least one other object to copy to.")
            return {'CANCELLED'}

        # Save target names for Reapply
        props.last_targets.clear()
        for obj in targets:
            entry          = props.last_targets.add()
            entry.obj_name = obj.name

        count, err = _do_copy(context, props, targets)
        if err:
            self.report({'ERROR'}, err)
            return {'CANCELLED'}

        mode_label = "replaced" if props.replace_all else "synced"
        self.report(
            {'INFO'},
            f"Modifiers {mode_label} on {count} object(s). Selection saved.",
        )
        return {'FINISHED'}


# ============================================================================
# Reapply-to-last-selection operator
# ============================================================================

class ARANTOOLS_OT_ModSync_Reapply(Operator):
    """Reapply the checked modifiers to the last saved target selection"""
    bl_idname  = "arantools.modsync_reapply_last"
    bl_label   = "Reapply to Last Selection"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        props = context.scene.arantools_mod_sync

        if not props.last_targets:
            self.report({'WARNING'}, "No previous selection saved.")
            return {'CANCELLED'}

        targets = []
        missing = []
        for entry in props.last_targets:
            obj = bpy.data.objects.get(entry.obj_name)
            if obj:
                targets.append(obj)
            else:
                missing.append(entry.obj_name)

        if not targets:
            self.report(
                {'ERROR'},
                "None of the saved objects exist in the scene anymore.",
            )
            return {'CANCELLED'}

        count, err = _do_copy(context, props, targets)
        if err:
            self.report({'ERROR'}, err)
            return {'CANCELLED'}

        mode_label = "replaced" if props.replace_all else "synced"
        msg = f"Modifiers {mode_label} on {count} object(s)."
        if missing:
            msg += f"  ({len(missing)} no longer in scene: {', '.join(missing)})"
        self.report({'INFO'}, msg)
        return {'FINISHED'}


# ============================================================================
# Registration
# ============================================================================

classes = [
    ARANTOOLS_PG_ModItem,
    ARANTOOLS_PG_SavedObjName,
    ARANTOOLS_PG_ModSync,
    ARANTOOLS_OT_ModSync_SaveStack,
    ARANTOOLS_OT_ModSync_CopyToSelected,
    ARANTOOLS_OT_ModSync_Reapply,
]


def register():
    for cls in classes:
        bpy.utils.register_class(cls)
    bpy.types.Scene.arantools_mod_sync = bpy.props.PointerProperty(
        type=ARANTOOLS_PG_ModSync,
    )


def unregister():
    del bpy.types.Scene.arantools_mod_sync
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
