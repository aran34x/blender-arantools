import bpy
from bpy.types import Operator, PropertyGroup


# ============================================================================
# Auto Weights (Selected) — property group
# ============================================================================
#
# Runs Blender's real "With Automatic Weights" (bone-heat) binding, scoped down
# to a user selection along two independent checkboxes:
#
#   • Only Selected Bones — weights are distributed across ONLY the selected
#     bones. Non-selected deform bones have use_deform temporarily cleared for
#     the bake, so they neither receive groups nor participate in the heat
#     solve's normalization. Their existing groups are left untouched.
#
#   • Only Selected Mesh Parts — the selected vertices are baked AS IF THEY
#     WERE THEIR OWN MESH: we duplicate the object, delete the unselected
#     verts, run Automatic Weights on that island, then copy the resulting
#     groups back onto the original's selected verts. This gives a clean,
#     self-normalized bind over exactly the selected geometry — identical to
#     separating the selection into its own object and auto-weighting it.
#
# Both are checkboxes and are fully independent — either, both, or neither.


class ARANTOOLS_PG_AutoWeights(PropertyGroup):
    only_selected_bones: bpy.props.BoolProperty(
        name="Only Selected Bones",
        description="Distribute weights across ONLY the bones selected in the "
                    "armature. Other deform bones are ignored — they don't "
                    "receive groups and their existing groups stay untouched",
        default=True,
    )
    only_selected_verts: bpy.props.BoolProperty(
        name="Only Selected Mesh Parts",
        description="Rig only the selected vertices (selection made in Edit "
                    "Mode), computing their weights as if the selection were "
                    "its own separate mesh. Unselected vertices are untouched",
        default=True,
    )


# ============================================================================
# Auto Weights (Selected) — helpers
# ============================================================================

_ORIG_IDX_ATTR = "arantools_orig_idx"


def _has_armature_modifier(obj, armature):
    return any(m.type == 'ARMATURE' and m.object == armature
               for m in obj.modifiers)


def _ensure_armature_bound(context, obj, armature):
    """Guarantee obj is parented to armature with an Armature modifier, WITHOUT
    computing or clearing any weights (ARMATURE_NAME = empty groups)."""
    if _has_armature_modifier(obj, armature):
        return
    bpy.ops.object.select_all(action='DESELECT')
    obj.select_set(True)
    armature.select_set(True)
    context.view_layer.objects.active = armature
    bpy.ops.object.parent_set(type='ARMATURE_NAME')


def _bake_selection_as_own_mesh(context, obj, armature, target_bone_names):
    """Duplicate obj, keep only its selected verts, auto-weight that island to
    the (already deform-restricted) armature, and return the resulting weights
    keyed by ORIGINAL vertex index:

        {bone_name: {orig_vert_index: weight}}

    The original object is not modified here."""
    # Isolate obj so duplicate() only copies it.
    bpy.ops.object.select_all(action='DESELECT')
    obj.select_set(True)
    context.view_layer.objects.active = obj
    bpy.ops.object.duplicate()
    temp = context.active_object

    # Stamp each vert with its original index so we can map weights back after
    # deleting geometry (generic attributes survive element deletion).
    if _ORIG_IDX_ATTR in temp.data.attributes:
        temp.data.attributes.remove(temp.data.attributes[_ORIG_IDX_ATTR])
    attr = temp.data.attributes.new(_ORIG_IDX_ATTR, 'INT', 'POINT')
    attr.data.foreach_set('value', list(range(len(temp.data.vertices))))

    # Delete everything that ISN'T selected → the island stands alone.
    bpy.ops.object.mode_set(mode='EDIT')
    bpy.ops.mesh.select_mode(type='VERT')
    bpy.ops.mesh.select_all(action='INVERT')
    bpy.ops.mesh.delete(type='VERT')
    bpy.ops.object.mode_set(mode='OBJECT')

    result = {}
    if len(temp.data.vertices) == 0:
        _delete_temp(context, temp)
        return result

    # Fresh groups only — drop the duplicated groups before the bake.
    bpy.ops.object.select_all(action='DESELECT')
    temp.select_set(True)
    context.view_layer.objects.active = temp
    if temp.vertex_groups:
        bpy.ops.object.vertex_group_remove(all=True)

    # Real Automatic Weights on the isolated island. Because non-target deform
    # bones are disabled by the caller, the heat solve normalizes across
    # exactly the target bones.
    bpy.ops.object.select_all(action='DESELECT')
    temp.select_set(True)
    armature.select_set(True)
    context.view_layer.objects.active = armature
    try:
        bpy.ops.object.parent_set(type='ARMATURE_AUTO')
    except RuntimeError:
        _delete_temp(context, temp)
        return result

    # Map temp vert index → original vert index.
    orig = [0] * len(temp.data.vertices)
    temp.data.attributes[_ORIG_IDX_ATTR].data.foreach_get('value', orig)

    for bname in target_bone_names:
        vg = temp.vertex_groups.get(bname)
        if vg is None:
            continue
        gidx = vg.index
        wmap = {}
        for v in temp.data.vertices:
            for g in v.groups:
                if g.group == gidx:
                    wmap[orig[v.index]] = g.weight
                    break
        if wmap:
            result[bname] = wmap

    _delete_temp(context, temp)
    return result


def _delete_temp(context, temp):
    """Delete a temporary object and its orphaned mesh data."""
    data = temp.data if temp.type == 'MESH' else None
    bpy.ops.object.select_all(action='DESELECT')
    temp.select_set(True)
    context.view_layer.objects.active = temp
    bpy.ops.object.delete()
    if data is not None and data.users == 0:
        bpy.data.meshes.remove(data)


def _apply_weights_to_selection(obj, result):
    """Clear the selected verts from every group, then write the baked weights
    onto them — reproducing a standalone-mesh bind for the selection only."""
    sel_idx = [v.index for v in obj.data.vertices if v.select]
    if not sel_idx:
        return
    for vg in list(obj.vertex_groups):
        try:
            vg.remove(sel_idx)
        except RuntimeError:
            pass
    for bname, wmap in result.items():
        vg = obj.vertex_groups.get(bname) or obj.vertex_groups.new(name=bname)
        for oidx, w in wmap.items():
            if w > 0.0:
                vg.add([oidx], w, 'REPLACE')


def _selected_bone_names(context, armature):
    """Return the set of selected bone names, robust across Blender versions
    where bpy.types.Bone has no `.select`. Reads context.selected_bones while
    the armature is briefly in Edit Mode (the codebase's proven pattern), then
    restores the previous mode/active object. Bone selection is shared between
    Edit/Pose, so this reflects whatever the artist selected."""
    prev_active = context.view_layer.objects.active
    prev_selected = armature.select_get()
    prev_mode = armature.mode

    context.view_layer.objects.active = armature
    armature.select_set(True)
    if armature.mode != 'EDIT':
        bpy.ops.object.mode_set(mode='EDIT')

    names = {eb.name for eb in (context.selected_bones or [])}

    if armature.mode != prev_mode:
        bpy.ops.object.mode_set(mode=prev_mode)
    armature.select_set(prev_selected)
    if prev_active is not None:
        context.view_layer.objects.active = prev_active
    return names


# ============================================================================
# Auto Weights (Selected) — operator
# ============================================================================

class ARANTOOLS_OT_AutoWeightsSelected(Operator):
    """Bind the selected mesh(es) to the armature with Automatic Weights,
    restricted to the selected bones and/or the selected mesh parts"""
    bl_idname = "arantools.auto_weights_selected"
    bl_label = "Auto Weights (Selected)"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        sel = context.selected_objects
        return (any(o.type == 'MESH' for o in sel)
                and any(o.type == 'ARMATURE' for o in sel))

    def execute(self, context):
        props = context.scene.arantools_auto_weights

        # Parenting requires Object Mode; also flushes the Edit-Mode vertex
        # selection so mesh.vertices[i].select is current.
        if context.mode != 'OBJECT':
            bpy.ops.object.mode_set(mode='OBJECT')

        sel = context.selected_objects
        meshes = [o for o in sel if o.type == 'MESH']

        active = context.active_object
        if active is not None and active.type == 'ARMATURE':
            armature = active
        else:
            armature = next((o for o in sel if o.type == 'ARMATURE'), None)

        if not meshes or armature is None:
            self.report({'ERROR'}, "Select at least one Mesh and one Armature.")
            return {'CANCELLED'}

        # ── Resolve the target bone set ───────────────────────────────────
        deform_bones = [b for b in armature.data.bones if b.use_deform]
        if props.only_selected_bones:
            selected_names = _selected_bone_names(context, armature)
            target_bone_names = [b.name for b in deform_bones
                                 if b.name in selected_names]
            if not target_bone_names:
                if selected_names:
                    self.report({'ERROR'},
                                "Selected bones have no Deform flag — enable "
                                "Deform or select deform bones.")
                else:
                    self.report({'ERROR'}, "No selected bones in the armature.")
                return {'CANCELLED'}
        else:
            target_bone_names = [b.name for b in deform_bones]
            if not target_bone_names:
                self.report({'ERROR'}, "Armature has no deform bones.")
                return {'CANCELLED'}

        # ── When scoping to selected parts, keep only meshes that have some ─
        if props.only_selected_verts:
            meshes = [m for m in meshes
                      if any(v.select for v in m.data.vertices)]
            if not meshes:
                self.report({'ERROR'},
                            "No selected vertices. Select mesh parts in Edit "
                            "Mode, or turn off 'Only Selected Mesh Parts'.")
                return {'CANCELLED'}

        # ── Temporarily restrict deform to the target bones ────────────────
        saved_deform = None
        if props.only_selected_bones:
            saved_deform = {b.name: b.use_deform for b in armature.data.bones}
            keep = set(target_bone_names)
            for b in armature.data.bones:
                if b.name not in keep:
                    b.use_deform = False

        try:
            if props.only_selected_verts:
                # Bake each selection as its own mesh, copy weights back.
                for m in meshes:
                    _ensure_armature_bound(context, m, armature)
                    result = _bake_selection_as_own_mesh(
                        context, m, armature, target_bone_names)
                    _apply_weights_to_selection(m, result)
            else:
                # Whole-mesh Automatic Weights on all selected meshes at once.
                bpy.ops.object.select_all(action='DESELECT')
                for m in meshes:
                    m.select_set(True)
                armature.select_set(True)
                context.view_layer.objects.active = armature
                bpy.ops.object.parent_set(type='ARMATURE_AUTO')
        except RuntimeError as e:
            self.report({'ERROR'}, f"Auto-weight bind failed: {e}")
            return {'CANCELLED'}
        finally:
            # Always restore the deform flags, even if a bake raised.
            if saved_deform is not None:
                for b in armature.data.bones:
                    if b.name in saved_deform:
                        b.use_deform = saved_deform[b.name]

        # ── Tidy selection: leave the meshes selected, armature active ─────
        bpy.ops.object.select_all(action='DESELECT')
        for m in meshes:
            if m.name in bpy.data.objects:
                m.select_set(True)
        armature.select_set(True)
        context.view_layer.objects.active = armature

        scope = []
        if props.only_selected_bones:
            scope.append(f"{len(target_bone_names)} bone(s)")
        if props.only_selected_verts:
            scope.append("selected parts")
        scope_txt = (" (" + ", ".join(scope) + ")") if scope else ""
        self.report({'INFO'},
                    f"Auto-weighted {len(meshes)} mesh(es){scope_txt}.")
        return {'FINISHED'}


# ============================================================================
# Registration
# ============================================================================

classes = [
    ARANTOOLS_PG_AutoWeights,
    ARANTOOLS_OT_AutoWeightsSelected,
]


def register():
    for cls in classes:
        bpy.utils.register_class(cls)
    bpy.types.Scene.arantools_auto_weights = bpy.props.PointerProperty(
        type=ARANTOOLS_PG_AutoWeights)


def unregister():
    # Defensive: a hot-reload right after this module was first added can call
    # unregister() before register() ever ran. Guard so teardown never raises
    # and leaves the addon half-registered.
    if hasattr(bpy.types.Scene, "arantools_auto_weights"):
        del bpy.types.Scene.arantools_auto_weights
    for cls in reversed(classes):
        try:
            bpy.utils.unregister_class(cls)
        except RuntimeError:
            pass
