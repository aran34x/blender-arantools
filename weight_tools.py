import bpy
from collections import defaultdict
from bpy.types import Operator
from mathutils import Vector


def _wt_get_islands(me):
    """Connected-component vertex islands of a mesh (by edges)."""
    adjacency = [[] for _ in me.vertices]
    for e in me.edges:
        v1, v2 = e.vertices
        adjacency[v1].append(v2)
        adjacency[v2].append(v1)
    visited = set()
    islands = []
    for v_idx in range(len(me.vertices)):
        if v_idx not in visited:
            stack, island = [v_idx], []
            visited.add(v_idx)
            while stack:
                cur = stack.pop()
                island.append(cur)
                for neigh in adjacency[cur]:
                    if neigh not in visited:
                        visited.add(neigh)
                        stack.append(neigh)
            islands.append(island)
    return islands


def _wt_segment_distance(pt, v1, v2):
    """Distance from point `pt` to the nearest point on segment v1–v2."""
    seg = v2 - v1
    seg_len_sq = seg.length_squared
    if seg_len_sq < 1e-12:
        return (pt - v1).length
    t = max(0.0, min(1.0, (pt - v1).dot(seg) / seg_len_sq))
    return (pt - (v1 + seg * t)).length


# ============================================================================
# Smart Weight Transfer
# ============================================================================

class ARANTOOLS_PG_SmartTransfer(bpy.types.PropertyGroup):
    source_mesh: bpy.props.PointerProperty(
        type=bpy.types.Object,
        name="Source",
        description="The rigged mesh to copy weights from",
        poll=lambda self, obj: obj.type == 'MESH'
    )
    interp_mode: bpy.props.EnumProperty(
        name="Method",
        items=[
            ('NEAREST', "Nearest Vertex", "Fastest"),
            ('POLYINTERP_NEAREST', "Nearest Face (Smooth)", "Standard, recommended"),
            ('POLYINTERP_VNORPROJ', "Projected Face", "Good for layered clothing"),
        ],
        default='POLYINTERP_NEAREST'
    )
    clean_empty: bpy.props.BoolProperty(name="Clean Empty Groups", default=True)


class ARANTOOLS_OT_SmartWeightTransfer(Operator):
    """Pre-creates vertex groups then transfers weights and binds to armature"""
    bl_idname = "arantools.smart_weight_transfer"
    bl_label = "Transfer & Bind"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        props = context.scene.arantools_smart_transfer
        source = props.source_mesh
        targets = [obj for obj in context.selected_objects if obj.type == 'MESH']

        if not source:
            self.report({'ERROR'}, "Assign a SOURCE mesh in the panel!")
            return {'CANCELLED'}
        if not source.vertex_groups:
            self.report({'ERROR'}, f"Source '{source.name}' has no vertex groups to transfer!")
            return {'CANCELLED'}
        if source in targets:
            targets.remove(source)
        if not targets:
            self.report({'ERROR'}, "Select TARGET meshes in the 3D Viewport!")
            return {'CANCELLED'}

        armature = source.parent
        if not armature or armature.type != 'ARMATURE':
            for mod in source.modifiers:
                if mod.type == 'ARMATURE' and mod.object:
                    armature = mod.object
                    break
        if not armature:
            self.report({'ERROR'}, f"Source '{source.name}' has no associated armature!")
            return {'CANCELLED'}

        if bpy.context.object and bpy.context.object.mode != 'OBJECT':
            bpy.ops.object.mode_set(mode='OBJECT')

        count = 0
        for target in targets:
            if target.data.shape_keys:
                self.report({'WARNING'}, f"Skipping '{target.name}': Has Shape Keys.")
                continue

            context.view_layer.objects.active = target

            for src_grp in source.vertex_groups:
                if src_grp.name not in target.vertex_groups:
                    target.vertex_groups.new(name=src_grp.name)

            dt_mod = target.modifiers.new(name="WeightTransfer_Temp", type='DATA_TRANSFER')
            dt_mod.object = source
            dt_mod.use_vert_data = True
            dt_mod.data_types_verts = {'VGROUP_WEIGHTS'}
            dt_mod.vert_mapping = props.interp_mode

            try:
                bpy.ops.object.modifier_apply(modifier=dt_mod.name)
            except RuntimeError as e:
                self.report({'ERROR'}, f"Apply failed on {target.name}. See console.")
                print(f"Error: {e}")
                continue

            if props.clean_empty and target.vertex_groups:
                bpy.ops.object.vertex_group_clean(group_select_mode='ALL', limit=0.001)

            if target.parent != armature:
                bpy.ops.object.select_all(action='DESELECT')
                target.select_set(True)
                armature.select_set(True)
                context.view_layer.objects.active = armature
                bpy.ops.object.parent_set(type='ARMATURE', keep_transform=True)

            arm_mod = next((m for m in target.modifiers if m.type == 'ARMATURE'), None)
            if not arm_mod:
                arm_mod = target.modifiers.new(name="Armature", type='ARMATURE')
                arm_mod.object = armature
            arm_mod.show_viewport = True
            count += 1

        if count > 0:
            self.report({'INFO'}, f"Transferred & bound {count} mesh(es).")
        else:
            self.report({'WARNING'}, "No meshes were processed. Check errors.")
        return {'FINISHED'}


# ============================================================================
# Sync Vertex Groups
# ============================================================================

class ARANTOOLS_OT_SyncVertexGroups(Operator):
    """Adds empty vertex groups for any deform bones missing from the active mesh"""
    bl_idname = "arantools.sync_vertex_groups"
    bl_label = "Sync Groups from Armature"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        obj = context.active_object
        return obj and obj.type == 'MESH'

    def find_armature(self, obj):
        for mod in obj.modifiers:
            if mod.type == 'ARMATURE' and mod.object:
                return mod.object
        if obj.parent and obj.parent.type == 'ARMATURE':
            return obj.parent
        return None

    def execute(self, context):
        obj = context.active_object
        armature_obj = self.find_armature(obj)
        if not armature_obj:
            self.report({'ERROR'}, "No associated armature found.")
            return {'CANCELLED'}

        deform_bone_names = {bone.name for bone in armature_obj.data.bones if bone.use_deform}
        existing_group_names = {vg.name for vg in obj.vertex_groups}
        missing_names = deform_bone_names - existing_group_names

        if not missing_names:
            self.report({'INFO'}, "Vertex groups already in sync.")
            return {'FINISHED'}

        for name in sorted(missing_names):
            obj.vertex_groups.new(name=name)

        self.report({'INFO'}, f"Added {len(missing_names)} missing vertex group(s).")
        return {'FINISHED'}


# ============================================================================
# Unify Island Weights
# ============================================================================

class ARANTOOLS_PG_UnifyWeights(bpy.types.PropertyGroup):
    blend: bpy.props.FloatProperty(
        name="Blend",
        description="0 = no change, 1 = full unification to dominant bone",
        min=0.0, max=1.0, default=1.0
    )
    only_selected: bpy.props.BoolProperty(
        name="Only Selected Vertices",
        description="Only apply to vertices selected in Edit Mode",
        default=False
    )


class ARANTOOLS_OT_UnifyIslandWeights(Operator):
    """For each disconnected mesh island, finds the dominant bone and assigns the whole island to it"""
    bl_idname = "arantools.unify_island_weights"
    bl_label = "Unify Island Weights"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        obj = context.active_object
        return obj and obj.type == 'MESH' and bool(obj.vertex_groups)

    def get_islands(self, me):
        adjacency = [[] for _ in me.vertices]
        for e in me.edges:
            v1, v2 = e.vertices
            adjacency[v1].append(v2)
            adjacency[v2].append(v1)
        visited = set()
        islands = []
        for v_idx in range(len(me.vertices)):
            if v_idx not in visited:
                stack, island = [v_idx], set()
                visited.add(v_idx)
                while stack:
                    cur = stack.pop()
                    island.add(cur)
                    for neigh in adjacency[cur]:
                        if neigh not in visited:
                            visited.add(neigh)
                            stack.append(neigh)
                islands.append(list(island))
        return islands

    def execute(self, context):
        props = context.scene.arantools_unify_weights
        obj = context.active_object
        if obj.mode != 'OBJECT':
            bpy.ops.object.mode_set(mode='OBJECT')
        me = obj.data
        blend_factor = props.blend

        selected_verts = None
        if props.only_selected:
            selected_verts = {v.index for v in me.vertices if v.select}
            if not selected_verts:
                self.report({'WARNING'}, "No vertices selected.")
                return {'CANCELLED'}

        islands = self.get_islands(me)
        if not islands:
            self.report({'WARNING'}, "No vertex islands found.")
            return {'CANCELLED'}

        processed = 0
        for island in islands:
            if not island:
                continue

            group_totals = defaultdict(float)
            for v_idx in island:
                for g in me.vertices[v_idx].groups:
                    group_totals[g.group] += g.weight

            if not group_totals:
                continue

            winner = max(group_totals, key=group_totals.get)
            target_verts = island if selected_verts is None else [v for v in island if v in selected_verts]
            if not target_verts:
                continue

            if blend_factor == 1.0:
                groups_to_clear = set()
                for v_idx in target_verts:
                    for g in me.vertices[v_idx].groups:
                        groups_to_clear.add(g.group)
                groups_to_clear.discard(winner)
                for vg_idx in groups_to_clear:
                    obj.vertex_groups[vg_idx].remove(target_verts)
                obj.vertex_groups[winner].add(target_verts, 1.0, 'REPLACE')
            else:
                initial = {v: {g.group: g.weight for g in me.vertices[v].groups} for v in target_verts}
                all_groups = set(initial[v].keys() for v in target_verts)
                all_groups = set().union(*[initial[v].keys() for v in target_verts])
                all_groups.add(winner)

                for vg_idx in all_groups:
                    vg = obj.vertex_groups[vg_idx]
                    is_winner = (vg_idx == winner)
                    to_remove = []
                    to_add = defaultdict(list)
                    for v_idx in target_verts:
                        old_w = initial[v_idx].get(vg_idx, 0.0)
                        target_w = 1.0 if is_winner else 0.0
                        blended = old_w * (1.0 - blend_factor) + target_w * blend_factor
                        if blended > 1e-5:
                            to_add[round(blended, 4)].append(v_idx)
                        elif old_w > 1e-5:
                            to_remove.append(v_idx)
                    if to_remove:
                        vg.remove(to_remove)
                    for w, verts in to_add.items():
                        if verts:
                            vg.add(verts, w, 'REPLACE')
            processed += 1

        self.report({'INFO'}, f"Unified {processed} island(s).")
        return {'FINISHED'}


# ============================================================================
# Island Weight Copy
# ============================================================================

class ARANTOOLS_PG_IslandCopy(bpy.types.PropertyGroup):
    base_vertex_method: bpy.props.EnumProperty(
        name="Base Vertex By",
        items=[
            ('UV_Y', 'Lowest UV-Y', 'Vertex with the lowest UV Y-value on each island'),
            ('SHARP', 'Marked Sharp', 'First vertex found on a sharp edge'),
            ('ATTRIBUTE', 'Attribute', 'Vertex marked with a custom boolean attribute'),
        ],
        default='UV_Y'
    )
    base_attribute_name: bpy.props.StringProperty(name="Attribute Name", default="base_vert")
    blend_factor: bpy.props.FloatProperty(name="Blend Factor", min=0.0, max=1.0, default=1.0)
    only_selected: bpy.props.BoolProperty(name="Only Selected Vertices", default=False)


class ARANTOOLS_OT_IslandWeightCopy(Operator):
    """Copies weights from a single base vertex to the rest of each mesh island"""
    bl_idname = "arantools.island_weight_copy"
    bl_label = "Copy Weights by Island"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return context.active_object and context.active_object.type == 'MESH'

    def get_islands(self, me):
        adjacency = [[] for _ in me.vertices]
        for e in me.edges:
            v1, v2 = e.vertices
            adjacency[v1].append(v2)
            adjacency[v2].append(v1)
        visited = set()
        islands = []
        for v_idx in range(len(me.vertices)):
            if v_idx not in visited:
                stack, island = [v_idx], set()
                visited.add(v_idx)
                while stack:
                    cur = stack.pop()
                    island.add(cur)
                    for neigh in adjacency[cur]:
                        if neigh not in visited:
                            visited.add(neigh)
                            stack.append(neigh)
                islands.append(list(island))
        return islands

    def execute(self, context):
        props = context.scene.arantools_island_copy
        obj = context.active_object
        if obj.mode != 'OBJECT':
            bpy.ops.object.mode_set(mode='OBJECT')
        me = obj.data

        islands = self.get_islands(me)
        if not islands:
            self.report({'WARNING'}, "No vertex islands found.")
            return {'CANCELLED'}

        selected_verts = {v.index for v in me.vertices if v.select} if props.only_selected else None

        for island in islands:
            source_idx = -1

            if props.base_vertex_method == 'UV_Y':
                if not me.uv_layers.active:
                    self.report({'ERROR'}, "No active UV layer.")
                    return {'CANCELLED'}
                uv_data = me.uv_layers.active.data
                vert_uvs = {loop.vertex_index: uv_data[loop.index].uv for loop in reversed(me.loops)}
                candidates = {v for v in island if v in vert_uvs}
                if candidates:
                    source_idx = min(candidates, key=lambda v: vert_uvs[v].y)

            elif props.base_vertex_method == 'SHARP':
                sharp_verts = {v for e in me.edges if e.use_edge_sharp for v in e.vertices}
                for v_idx in island:
                    if v_idx in sharp_verts:
                        source_idx = v_idx
                        break

            elif props.base_vertex_method == 'ATTRIBUTE':
                attr = me.attributes.get(props.base_attribute_name)
                if not attr or attr.domain != 'POINT' or attr.data_type != 'BOOLEAN':
                    self.report({'ERROR'}, f"Attribute '{props.base_attribute_name}' not found or invalid.")
                    return {'CANCELLED'}
                for v_idx in island:
                    if attr.data[v_idx].value:
                        source_idx = v_idx
                        break

            if source_idx == -1:
                continue

            src_weights = {g.group: g.weight for g in me.vertices[source_idx].groups}
            target_verts = island if selected_verts is None else [v for v in island if v in selected_verts]
            if not target_verts:
                continue

            if props.blend_factor == 1.0:
                for vg_idx, weight in src_weights.items():
                    obj.vertex_groups[vg_idx].add(target_verts, weight, 'REPLACE')
                all_indices = {vg.index for vg in obj.vertex_groups}
                for vg_idx in all_indices - src_weights.keys():
                    obj.vertex_groups[vg_idx].remove(target_verts)
            else:
                for v_idx in target_verts:
                    old = {g.group: g.weight for g in me.vertices[v_idx].groups}
                    for vg in obj.vertex_groups:
                        old_w = old.get(vg.index, 0.0)
                        src_w = src_weights.get(vg.index, 0.0)
                        blended = old_w * (1 - props.blend_factor) + src_w * props.blend_factor
                        if blended > 1e-5:
                            vg.add([v_idx], blended, 'REPLACE')
                        elif vg.index in old:
                            vg.remove([v_idx])

        self.report({'INFO'}, f"Applied weights across {len(islands)} island(s).")
        return {'FINISHED'}


# ============================================================================
# Contact Weight Flooder
# ============================================================================

class ARANTOOLS_PG_ContactFlood(bpy.types.PropertyGroup):
    source: bpy.props.PointerProperty(
        name="Source",
        type=bpy.types.Object,
        description="Rigged mesh to copy weights from via raycast",
        poll=lambda self, obj: obj.type == 'MESH'
    )
    blend: bpy.props.FloatProperty(name="Blend", default=1.0, min=0.0, max=1.0)
    use_selection: bpy.props.BoolProperty(
        name="Use Selected Verts",
        default=True,
        description="Use selected vertices to find the contact point."
    )
    use_uv: bpy.props.BoolProperty(
        name="Use UV Fallback",
        default=False,
        description="If no selection found, fall back to UV extreme."
    )
    uv_name: bpy.props.StringProperty(name="UV Map", default="UVMap")
    uv_axis: bpy.props.EnumProperty(
        name="Axis",
        items=[('U', "U", ""), ('V', "V", "")],
        default='V'
    )
    uv_direction: bpy.props.EnumProperty(
        name="Direction",
        items=[('MIN', "Min", ""), ('MAX', "Max", "")],
        default='MIN'
    )


class ARANTOOLS_OT_ContactFlood(Operator):
    """Raycasts each secondary mesh onto the source to copy contact-point weights"""
    bl_idname = "arantools.contact_flood"
    bl_label = "Flood Weights"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        props = context.scene.arantools_contact_flood
        source = props.source

        if not source:
            self.report({'ERROR'}, "No Source Object selected.")
            return {'CANCELLED'}

        if context.object and context.object.mode != 'OBJECT':
            bpy.ops.object.mode_set(mode='OBJECT')

        bpy.ops.mesh.separate(type='LOOSE')
        meshes = [o for o in context.selected_objects if o.type == 'MESH' and o != source]

        source_mx = source.matrix_world
        source_imx = source_mx.inverted_safe()

        for mesh in meshes:
            mesh_mx = mesh.matrix_world
            candidate_indices = []

            if props.use_selection:
                candidate_indices = [v.index for v in mesh.data.vertices if v.select]

            if not candidate_indices and props.use_uv:
                if props.uv_name in mesh.data.uv_layers:
                    uv_data = mesh.data.uv_layers[props.uv_name].data
                    best_val = float('-inf') if props.uv_direction == 'MAX' else float('inf')
                    target_v_idx = -1
                    for loop in mesh.data.loops:
                        uv = uv_data[loop.index].uv
                        val = uv.x if props.uv_axis == 'U' else uv.y
                        if (props.uv_direction == 'MAX' and val > best_val) or \
                           (props.uv_direction == 'MIN' and val < best_val):
                            best_val = val
                            target_v_idx = loop.vertex_index
                    if target_v_idx != -1:
                        candidate_indices = [target_v_idx]

            if not candidate_indices:
                candidate_indices = range(len(mesh.data.vertices))

            best_poly_index = -1
            best_hit_local = None
            min_dist = float('inf')

            for i in candidate_indices:
                v_world = mesh_mx @ mesh.data.vertices[i].co
                v_in_source = source_imx @ v_world
                result = source.closest_point_on_mesh(v_in_source)
                if not result[0]:
                    continue
                hit_loc_local = result[1]
                dist = (v_world - source_mx @ hit_loc_local).length
                if dist < min_dist:
                    min_dist = dist
                    best_poly_index = result[3]
                    best_hit_local = hit_loc_local

            if best_poly_index == -1:
                continue

            # Find nearest vertex on contact polygon
            poly = source.data.polygons[best_poly_index]
            best_v_idx = min(poly.vertices, key=lambda vi: (source.data.vertices[vi].co - best_hit_local).length)
            w_data = {}
            for g in source.data.vertices[best_v_idx].groups:
                try:
                    w_data[source.vertex_groups[g.group].name] = g.weight
                except IndexError:
                    continue

            all_vert_indices = [v.index for v in mesh.data.vertices]
            for name, val in w_data.items():
                if name not in mesh.vertex_groups:
                    mesh.vertex_groups.new(name=name)
                vg = mesh.vertex_groups[name]
                if props.blend >= 0.99:
                    if val > 0.001:
                        vg.add(all_vert_indices, val, 'REPLACE')
                    else:
                        vg.remove(all_vert_indices)
                else:
                    for v in mesh.data.vertices:
                        try:
                            old_w = vg.weight(v.index)
                        except RuntimeError:
                            old_w = 0.0
                        final_w = val * props.blend + old_w * (1.0 - props.blend)
                        if final_w > 0.001:
                            vg.add([v.index], final_w, 'REPLACE')
                        else:
                            vg.remove([v.index])

        if meshes:
            context.view_layer.objects.active = meshes[0]
            bpy.ops.object.join()
            self.report({'INFO'}, "Flooded weights successfully.")

        return {'FINISHED'}


# ============================================================================
# Rigid Bind by Island — weight 1.0 to the nearest bone per island
# ============================================================================

class ARANTOOLS_PG_IslandRigid(bpy.types.PropertyGroup):
    armature: bpy.props.PointerProperty(
        name="Armature",
        type=bpy.types.Object,
        description="Armature to bind to. Leave empty to auto-detect from the "
                    "active mesh's Armature modifier or parent",
        poll=lambda self, obj: obj.type == 'ARMATURE',
    )
    deform_only: bpy.props.BoolProperty(
        name="Deform Bones Only",
        description="Only consider bones with the Deform flag enabled",
        default=True,
    )
    measure: bpy.props.EnumProperty(
        name="Measure",
        description="How each island's distance to a bone is measured",
        items=[
            ('CLOSEST', "Closest Vertex",
             "Nearest distance from any vertex in the island to the bone — "
             "most accurate"),
            ('CENTROID', "Island Centroid",
             "Distance from the island's average position to the bone — "
             "faster on dense meshes"),
        ],
        default='CLOSEST',
    )
    only_selected: bpy.props.BoolProperty(
        name="Only Selected Vertices",
        description="Restrict to islands containing vertices selected in Edit "
                    "Mode (only those vertices are reweighted)",
        default=False,
    )
    bind_to_armature: bpy.props.BoolProperty(
        name="Add Armature Modifier",
        description="Parent the mesh to the armature and add an Armature "
                    "modifier if one isn't present",
        default=False,
    )


class ARANTOOLS_OT_IslandRigidBind(Operator):
    """For each mesh island, find the closest bone and assign every vertex in
that island a weight of 1.0 to that bone (clearing other groups)"""
    bl_idname = "arantools.island_rigid_bind"
    bl_label = "Bind Islands to Nearest Bone"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        obj = context.active_object
        return obj is not None and obj.type == 'MESH'

    def _find_armature(self, obj, props):
        if props.armature is not None:
            return props.armature
        for mod in obj.modifiers:
            if mod.type == 'ARMATURE' and mod.object:
                return mod.object
        if obj.parent and obj.parent.type == 'ARMATURE':
            return obj.parent
        return None

    def execute(self, context):
        props = context.scene.arantools_island_rigid
        obj = context.active_object

        arm = self._find_armature(obj, props)
        if arm is None:
            self.report({'ERROR'}, "No armature found. Pick one in the panel "
                                   "or add an Armature modifier.")
            return {'CANCELLED'}

        if obj.mode != 'OBJECT':
            bpy.ops.object.mode_set(mode='OBJECT')
        me = obj.data

        # World-space bone segments (rest pose).
        arm_mw = arm.matrix_world
        segments = []  # (bone_name, head_world, tail_world)
        for b in arm.data.bones:
            if props.deform_only and not b.use_deform:
                continue
            segments.append((b.name,
                             arm_mw @ b.head_local,
                             arm_mw @ b.tail_local))
        if not segments:
            self.report({'ERROR'}, "Armature has no "
                        + ("deform " if props.deform_only else "")
                        + "bones to bind to.")
            return {'CANCELLED'}

        islands = _wt_get_islands(me)
        if not islands:
            self.report({'WARNING'}, "No vertex islands found.")
            return {'CANCELLED'}

        selected_verts = None
        if props.only_selected:
            selected_verts = {v.index for v in me.vertices if v.select}
            if not selected_verts:
                self.report({'WARNING'}, "No vertices selected.")
                return {'CANCELLED'}

        mesh_mw = obj.matrix_world
        world_co = [mesh_mw @ v.co for v in me.vertices]

        processed = 0
        for island in islands:
            target_verts = (island if selected_verts is None
                            else [v for v in island if v in selected_verts])
            if not target_verts:
                continue

            # Find the nearest bone for this island.
            if props.measure == 'CENTROID':
                rep = Vector((0.0, 0.0, 0.0))
                for v in island:
                    rep += world_co[v]
                rep /= len(island)
                best_name, best_d = None, float('inf')
                for name, head, tail in segments:
                    d = _wt_segment_distance(rep, head, tail)
                    if d < best_d:
                        best_d, best_name = d, name
            else:  # CLOSEST
                best_name, best_d = None, float('inf')
                for name, head, tail in segments:
                    for v in island:
                        d = _wt_segment_distance(world_co[v], head, tail)
                        if d < best_d:
                            best_d, best_name = d, name
                        if best_d == 0.0:
                            break

            if best_name is None:
                continue

            vg = obj.vertex_groups.get(best_name) or \
                obj.vertex_groups.new(name=best_name)
            winner_idx = vg.index

            # Clear every other group on these verts, then set weight 1.0.
            groups_to_clear = set()
            for v in target_verts:
                for g in me.vertices[v].groups:
                    groups_to_clear.add(g.group)
            groups_to_clear.discard(winner_idx)
            for gi in groups_to_clear:
                obj.vertex_groups[gi].remove(target_verts)
            vg.add(target_verts, 1.0, 'REPLACE')
            processed += 1

        # Optional: ensure the mesh is actually driven by the armature.
        if props.bind_to_armature:
            if obj.parent != arm:
                bpy.ops.object.select_all(action='DESELECT')
                obj.select_set(True)
                arm.select_set(True)
                context.view_layer.objects.active = arm
                bpy.ops.object.parent_set(type='ARMATURE', keep_transform=True)
                context.view_layer.objects.active = obj
            if not any(m.type == 'ARMATURE' and m.object == arm
                       for m in obj.modifiers):
                m = obj.modifiers.new(name="Armature", type='ARMATURE')
                m.object = arm

        self.report({'INFO'},
                    f"Bound {processed} island(s) to nearest bone of "
                    f"'{arm.name}'.")
        return {'FINISHED'}


# ============================================================================
# Registration
# ============================================================================

classes = [
    ARANTOOLS_PG_SmartTransfer,
    ARANTOOLS_OT_SmartWeightTransfer,
    ARANTOOLS_OT_SyncVertexGroups,
    ARANTOOLS_PG_UnifyWeights,
    ARANTOOLS_OT_UnifyIslandWeights,
    ARANTOOLS_PG_IslandCopy,
    ARANTOOLS_OT_IslandWeightCopy,
    ARANTOOLS_PG_ContactFlood,
    ARANTOOLS_OT_ContactFlood,
    ARANTOOLS_PG_IslandRigid,
    ARANTOOLS_OT_IslandRigidBind,
]


def register():
    for cls in classes:
        bpy.utils.register_class(cls)
    bpy.types.Scene.arantools_smart_transfer = bpy.props.PointerProperty(type=ARANTOOLS_PG_SmartTransfer)
    bpy.types.Scene.arantools_unify_weights = bpy.props.PointerProperty(type=ARANTOOLS_PG_UnifyWeights)
    bpy.types.Scene.arantools_island_copy = bpy.props.PointerProperty(type=ARANTOOLS_PG_IslandCopy)
    bpy.types.Scene.arantools_contact_flood = bpy.props.PointerProperty(type=ARANTOOLS_PG_ContactFlood)
    bpy.types.Scene.arantools_island_rigid = bpy.props.PointerProperty(type=ARANTOOLS_PG_IslandRigid)


def unregister():
    del bpy.types.Scene.arantools_island_rigid
    del bpy.types.Scene.arantools_contact_flood
    del bpy.types.Scene.arantools_island_copy
    del bpy.types.Scene.arantools_unify_weights
    del bpy.types.Scene.arantools_smart_transfer
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
