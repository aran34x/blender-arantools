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
    contrast: bpy.props.FloatProperty(
        name="Contrast",
        description="Sharpen or soften the baked weights. 1.0 = Blender's raw "
                    "result. Above 1 pushes each vertex toward its strongest "
                    "bone (harder, more rigid falloff); below 1 spreads "
                    "influence more evenly (softer blends). Each vertex's "
                    "total weight is preserved",
        default=1.0, min=0.2, max=6.0, soft_min=0.5, soft_max=3.0,
        precision=2,
    )
    remove_filter: bpy.props.StringProperty(
        name="Name Contains",
        description="Vertex groups whose name contains this text (case-insensitive) "
                    "are removed by 'Remove Groups'",
        default="feather",
    )


# ============================================================================
# Auto Weights (Selected) — helpers
# ============================================================================

_ORIG_IDX_ATTR = "arantools_orig_idx"


def resolve_armature(context, meshes):
    """Armature to bind to: the active/selected armature if there is one,
    otherwise the first armature already driving one of the meshes through an
    Armature modifier (so an already-rigged mesh can be re-bound on its own)."""
    active = context.active_object
    if active is not None and active.type == 'ARMATURE':
        return active
    arm = next((o for o in context.selected_objects if o.type == 'ARMATURE'),
               None)
    if arm is not None:
        return arm
    for m in meshes:
        for mod in m.modifiers:
            if mod.type == 'ARMATURE' and mod.object is not None:
                return mod.object
    return None


def _apply_contrast(obj, bone_names, gamma, only_selected):
    """Raise each vertex's weights (over bone_names' groups) to `gamma` and
    renormalize so the vertex keeps its original total on those groups."""
    if abs(gamma - 1.0) < 1e-4:
        return
    groups = [obj.vertex_groups.get(n) for n in bone_names]
    groups = [g for g in groups if g is not None]
    if not groups:
        return
    index_of = {g.index: g for g in groups}
    for v in obj.data.vertices:
        if only_selected and not v.select:
            continue
        ws = [(index_of[e.group], e.weight) for e in v.groups
              if e.group in index_of and e.weight > 0.0]
        total = sum(w for _, w in ws)
        if total <= 0.0:
            continue
        powered = [(g, w ** gamma) for g, w in ws]
        ptotal = sum(w for _, w in powered)
        if ptotal <= 0.0:
            continue
        scale = total / ptotal
        for g, w in powered:
            g.add([v.index], w * scale, 'REPLACE')


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
        meshes = [o for o in context.selected_objects if o.type == 'MESH']
        return bool(meshes) and resolve_armature(context, meshes) is not None

    def execute(self, context):
        props = context.scene.arantools_auto_weights

        # Parenting requires Object Mode; also flushes the Edit-Mode vertex
        # selection so mesh.vertices[i].select is current.
        if context.mode != 'OBJECT':
            bpy.ops.object.mode_set(mode='OBJECT')

        sel = context.selected_objects
        meshes = [o for o in sel if o.type == 'MESH']

        armature = resolve_armature(context, meshes)

        if not meshes or armature is None:
            self.report({'ERROR'},
                        "Select a Mesh and an Armature (or a mesh already "
                        "rigged to one).")
            return {'CANCELLED'}
        if armature.name not in context.view_layer.objects:
            self.report({'ERROR'}, f"Armature '{armature.name}' is not in the "
                                   "current view layer.")
            return {'CANCELLED'}

        # ── Resolve the target bone set ───────────────────────────────────
        # Selected bones if any are selected; otherwise every deform bone.
        deform_bones = [b for b in armature.data.bones if b.use_deform]
        if not deform_bones:
            self.report({'ERROR'}, "Armature has no deform bones.")
            return {'CANCELLED'}
        target_bone_names = [b.name for b in deform_bones]
        restrict_bones = False
        if props.only_selected_bones and not armature.hide_viewport:
            selected_names = _selected_bone_names(context, armature)
            picked = [b.name for b in deform_bones if b.name in selected_names]
            if picked:
                target_bone_names = picked
                restrict_bones = True
            elif selected_names:
                self.report({'ERROR'},
                            "Selected bones have no Deform flag — enable "
                            "Deform or select deform bones.")
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
        if restrict_bones:
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
                    _apply_contrast(m, target_bone_names, props.contrast, True)
            else:
                # Whole-mesh Automatic Weights on all selected meshes at once.
                bpy.ops.object.select_all(action='DESELECT')
                for m in meshes:
                    m.select_set(True)
                armature.select_set(True)
                context.view_layer.objects.active = armature
                bpy.ops.object.parent_set(type='ARMATURE_AUTO')
                for m in meshes:
                    _apply_contrast(m, target_bone_names, props.contrast, False)
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
        if restrict_bones:
            scope.append(f"{len(target_bone_names)} bone(s)")
        if props.only_selected_verts:
            scope.append("selected parts")
        scope_txt = (" (" + ", ".join(scope) + ")") if scope else ""
        self.report({'INFO'},
                    f"Auto-weighted {len(meshes)} mesh(es){scope_txt}.")
        return {'FINISHED'}


# ============================================================================
# Remove weight groups by name
# ============================================================================

class ARANTOOLS_OT_RemoveWeightGroups(Operator):
    """Remove every vertex group whose name contains the filter text from the
    selected mesh(es), then renormalize the vertices that lost weight"""
    bl_idname = "arantools.remove_weight_groups"
    bl_label = "Remove Groups by Name"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return any(o.type == 'MESH' for o in context.selected_objects) or (
            context.active_object is not None
            and context.active_object.type == 'MESH')

    def execute(self, context):
        text = context.scene.arantools_auto_weights.remove_filter.strip().lower()
        if not text:
            self.report({'ERROR'}, "Enter the text to match in 'Name Contains'.")
            return {'CANCELLED'}

        meshes = {o for o in context.selected_objects if o.type == 'MESH'}
        if context.active_object is not None and context.active_object.type == 'MESH':
            meshes.add(context.active_object)

        # Vertex-group writes are refused in Edit Mode: drop to Object Mode (which
        # also flushes the vertex selection), run, then go back.
        was_edit = context.mode == 'EDIT_MESH'
        if was_edit:
            bpy.ops.object.mode_set(mode='OBJECT')
        try:
            return self._run(context, meshes, text, was_edit)
        finally:
            if was_edit:
                bpy.ops.object.mode_set(mode='EDIT')

    def _run(self, context, meshes, text, was_edit):
        total_groups = 0
        total_verts = 0
        scoped = False
        for obj in meshes:
            doomed = [vg for vg in obj.vertex_groups if text in vg.name.lower()]
            if not doomed:
                continue
            # Edit Mode + selected verts: only those verts lose the matching weights
            # (the groups themselves stay); otherwise the groups are deleted outright.
            sel = {v.index for v in obj.data.vertices if v.select} if was_edit else set()
            sel_only = bool(sel)
            scoped = scoped or sel_only
            doomed_idx = {vg.index for vg in doomed}

            # Groups that stay in play for renormalizing: the armature's deform
            # bones when the mesh is rigged, otherwise every remaining group.
            deform_names = set()
            for mod in obj.modifiers:
                if mod.type == 'ARMATURE' and mod.object is not None:
                    deform_names |= {b.name for b in mod.object.data.bones
                                     if b.use_deform}
            keep = {vg.index: vg for vg in obj.vertex_groups
                    if vg.index not in doomed_idx
                    and (not deform_names or vg.name in deform_names)}

            # Collect the affected vertices' remaining weights BEFORE removal
            # (removing a group shifts the indices of the ones after it).
            fixes = []   # (vert_index, [(group_name, weight), ...])
            for v in obj.data.vertices:
                if sel_only and v.index not in sel:
                    continue
                if not any(g.group in doomed_idx for g in v.groups):
                    continue
                rest = [(keep[g.group].name, g.weight) for g in v.groups
                        if g.group in keep and g.weight > 0.0]
                fixes.append((v.index, rest))

            if sel_only:
                fix_idx = [vi for vi, _ in fixes]
                for vg in doomed:
                    vg.remove(fix_idx)
            else:
                for vg in doomed:
                    obj.vertex_groups.remove(vg)

            for vi, rest in fixes:
                total = sum(w for _, w in rest)
                if total <= 0.0:
                    continue
                for name, w in rest:
                    obj.vertex_groups[name].add([vi], w / total, 'REPLACE')

            total_groups += len(doomed)
            total_verts += len(fixes)
            obj.data.update()

        if total_groups == 0:
            self.report({'INFO'}, f"No vertex groups contain '{text}'.")
            return {'CANCELLED'}
        what = "weights from selected verts" if scoped else "group(s)"
        self.report({'INFO'}, f"Removed {total_groups} {what}; renormalized "
                              f"{total_verts} vertice(s).")
        return {'FINISHED'}


# ============================================================================
# Registration
# ============================================================================

classes = [
    ARANTOOLS_PG_AutoWeights,
    ARANTOOLS_OT_AutoWeightsSelected,
    ARANTOOLS_OT_RemoveWeightGroups,
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
