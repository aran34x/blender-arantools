import bpy
from bpy.types import Operator


# ============================================================================
# Helpers
# ============================================================================

# Properties that are type-level constants — never copy these
_SKIP_PROPS = frozenset({
    'rna_type', 'type', 'name', 'is_active', 'show_expanded',
})


def _copy_mod_props(src_mod, dst_mod):
    """Copy all non-read-only RNA properties from src_mod to dst_mod.
    Both must be the same modifier type."""
    if src_mod.type != dst_mod.type:
        return
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

    For each target:
      - If a modifier with the same name already exists → update its values
        in-place (preserving the modifier's current position in the stack).
      - If it doesn't exist → add it, then reorder so that all synced
        modifiers appear in the same relative order as in the source.

    Returns (count_succeeded, error_message_or_None).
    """
    src = props.source_object
    if not src:
        return 0, "No source object set."
    if not props.modifier_items:
        return 0, "No modifier stack saved. Click 'Save Stack' first."

    checked_items    = [item for item in props.modifier_items if item.enabled]
    if not checked_items:
        return 0, "No modifiers are checked."

    checked_names    = [item.mod_name for item in checked_items]
    checked_name_set = set(checked_names)

    old_active = context.view_layer.objects.active
    count      = 0

    for obj in targets:
        if obj is src:
            continue

        # ── Step 1: update existing mods / add missing mods ───────────────
        for item in checked_items:
            src_mod = src.modifiers.get(item.mod_name)
            if src_mod is None:
                continue  # source mod was removed after Save Stack

            dst_mod = obj.modifiers.get(item.mod_name)
            if dst_mod is not None:
                # Already exists → update property values in place
                if dst_mod.type == src_mod.type:
                    _copy_mod_props(src_mod, dst_mod)
            else:
                # Doesn't exist → add and populate
                new_mod = obj.modifiers.new(name=item.mod_name, type=src_mod.type)
                if new_mod:
                    _copy_mod_props(src_mod, new_mod)

        # ── Step 2: reorder synced mods to match source order ─────────────
        # We only adjust relative order among the checked mods; unchecked
        # (native) mods on the target are left at their current positions.
        # Algorithm: walk the checked names in source order; if the current
        # mod sits before the previous one, move it right after it.
        context.view_layer.objects.active = obj

        prev_name = None
        for name in checked_names:
            if name not in obj.modifiers:
                continue
            # Re-read stack every iteration because moving changes indices
            stack   = [m.name for m in obj.modifiers]
            cur_idx = stack.index(name)

            if prev_name is not None and prev_name in stack:
                prev_idx = stack.index(prev_name)
                if cur_idx < prev_idx:
                    # Move this mod to right after prev_name
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

        self.report(
            {'INFO'},
            f"Modifiers copied to {count} object(s). Selection saved.",
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

        msg = f"Reapplied to {count} object(s)."
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
