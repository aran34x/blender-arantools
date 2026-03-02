import bpy
from bpy.types import Operator


# ============================================================================
# Selection Tools
# ============================================================================

class ARANTOOLS_OT_select_deform_bones(Operator):
    """Select all bones with Deform enabled"""
    bl_idname = "arantools.select_deform_bones"
    bl_label = "Select Deform Bones"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        armature = context.object
        if not armature or armature.type != 'ARMATURE':
            self.report({'ERROR'}, "No armature selected")
            return {'CANCELLED'}

        bpy.ops.object.mode_set(mode='EDIT')
        bpy.ops.armature.select_all(action='DESELECT')
        bpy.ops.object.mode_set(mode='OBJECT')

        for bone in armature.data.bones:
            if bone.use_deform:
                bone.select = True

        return {'FINISHED'}


class ARANTOOLS_OT_select_bone_type(Operator):
    """Select bones by naming convention (CTRL_ / MCH_) or deform flag"""
    bl_idname = "arantools.select_bone_type"
    bl_label = "Select By Type"
    bl_options = {'REGISTER', 'UNDO'}

    bone_type: bpy.props.EnumProperty(
        name="Type",
        items=[
            ('DEFORM', "Deform", "Deform bones"),
            ('CONTROL', "Control", "Bones prefixed CTRL_"),
            ('MECH', "Mech", "Bones prefixed MCH_"),
        ]
    )

    def execute(self, context):
        armature = context.object
        if not armature or armature.type != 'ARMATURE':
            self.report({'ERROR'}, "No armature selected")
            return {'CANCELLED'}

        bpy.ops.object.mode_set(mode='EDIT')
        bpy.ops.armature.select_all(action='DESELECT')
        bpy.ops.object.mode_set(mode='OBJECT')

        for bone in armature.data.bones:
            if self.bone_type == 'DEFORM' and bone.use_deform:
                bone.select = True
            elif self.bone_type == 'CONTROL' and bone.name.startswith('CTRL_'):
                bone.select = True
            elif self.bone_type == 'MECH' and bone.name.startswith('MCH_'):
                bone.select = True

        return {'FINISHED'}


# ============================================================================
# Mirror Bones
# ============================================================================

class ARANTOOLS_OT_mirror_bones(Operator):
    """Mirror .L bones to .R using Blender's Symmetrize"""
    bl_idname = "arantools.mirror_bones"
    bl_label = "Mirror Bones L→R"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        armature = context.object
        if not armature or armature.type != 'ARMATURE':
            self.report({'ERROR'}, "No armature selected")
            return {'CANCELLED'}

        bpy.ops.object.mode_set(mode='EDIT')
        bpy.ops.armature.select_all(action='DESELECT')
        bpy.ops.object.mode_set(mode='OBJECT')

        for bone in armature.data.bones:
            if bone.name.endswith('.L'):
                bone.select = True

        bpy.ops.object.mode_set(mode='EDIT')
        bpy.ops.armature.symmetrize(direction='NEGATIVE_X')
        bpy.ops.object.mode_set(mode='OBJECT')

        self.report({'INFO'}, "Mirrored .L bones to .R")
        return {'FINISHED'}


# ============================================================================
# Feather Rigger — helpers
# ============================================================================

def _fr_segment_distance(pt, v1, v2):
    """Distance from point pt to the nearest point on segment v1-v2."""
    seg = v2 - v1
    seg_len = seg.length
    if seg_len < 1e-4:
        return (pt - v1).length
    t = max(0.0, min(1.0, seg.normalized().dot((pt - v1)) / seg_len))
    return (pt - (v1 + seg * t)).length


def _fr_closest_vertex_dist(mesh_obj, target_world):
    """Squared-root of closest vertex distance from an island mesh to a world point."""
    mw_inv = mesh_obj.matrix_world.inverted_safe()
    target_local = mw_inv @ target_world
    best = min(
        ((v.co - target_local).length_squared for v in mesh_obj.data.vertices),
        default=float('inf')
    )
    return best ** 0.5


def _fr_get_island_pointer(mesh_obj, method):
    """
    Return a world-space 'pointer' location for an island mesh.
    Returns None for CLOSEST_VERTEX (distance is computed differently).
    """
    me = mesh_obj.data
    mw = mesh_obj.matrix_world

    if method == 'CLOSEST_VERTEX':
        return None  # handled per-chain in the solver

    elif method == 'SHARP_EDGE':
        for e in me.edges:
            if e.use_edge_sharp:
                v1 = me.vertices[e.vertices[0]].co
                v2 = me.vertices[e.vertices[1]].co
                return mw @ ((v1 + v2) / 2.0)
        # Fallback: centroid
        if me.vertices:
            from mathutils import Vector
            c = sum((v.co for v in me.vertices), Vector()) / len(me.vertices)
            return mw @ c
        return None

    elif method == 'UV_Y_MAX':
        if not me.uv_layers.active:
            return None
        uv_data = me.uv_layers.active.data
        best_y, best_co = float('-inf'), None
        for loop in me.loops:
            y = uv_data[loop.index].uv.y
            if y > best_y:
                best_y = y
                best_co = me.vertices[loop.vertex_index].co
        return (mw @ best_co) if best_co is not None else None

    elif method == 'UV_Y_MIN':
        if not me.uv_layers.active:
            return None
        uv_data = me.uv_layers.active.data
        best_y, best_co = float('inf'), None
        for loop in me.loops:
            y = uv_data[loop.index].uv.y
            if y < best_y:
                best_y = y
                best_co = me.vertices[loop.vertex_index].co
        return (mw @ best_co) if best_co is not None else None

    elif method == 'CENTROID':
        if me.vertices:
            from mathutils import Vector
            c = sum((v.co for v in me.vertices), Vector()) / len(me.vertices)
            return mw @ c
        return None

    return None


def _fr_get_bone_point(armature_obj, bone_name, target):
    """Return world-space reference point on a rest bone."""
    bone = armature_obj.data.bones.get(bone_name)
    if not bone:
        return None
    mw = armature_obj.matrix_world
    if target == 'TIP':
        return mw @ bone.tail_local
    elif target == 'HEAD':
        return mw @ bone.head_local
    else:  # CENTER
        return mw @ ((bone.head_local + bone.tail_local) / 2.0)


def _fr_filter_bones(armature_obj, props):
    """Apply all bone filters and return a list of valid bpy.types.Bone objects."""
    bones = [b for b in armature_obj.data.bones if b.use_deform]

    if props.use_selected_only:
        selected = {b.name for b in armature_obj.data.bones if b.select}
        bones = [b for b in bones if b.name in selected]

    if props.filter_include.strip():
        keywords = [k.strip().lower() for k in props.filter_include.split(',') if k.strip()]
        if keywords:
            bones = [b for b in bones if any(k in b.name.lower() for k in keywords)]

    if props.filter_exclude.strip():
        keywords = [k.strip().lower() for k in props.filter_exclude.split(',') if k.strip()]
        if keywords:
            bones = [b for b in bones if not any(k in b.name.lower() for k in keywords)]

    return bones


def _fr_build_chains(armature_obj, props):
    """
    Build bone chains from filtered leaf bones.
    Each chain walks up `chain_length` parents from the leaf (tip).
    Returns a list of dicts: {bones, tip_name, tip_world}.
    """
    valid_bones = _fr_filter_bones(armature_obj, props)
    valid_names = {b.name for b in valid_bones}

    # Leaves = bones with no valid-filtered children
    leaves = [b for b in valid_bones if not any(c.name in valid_names for c in b.children)]

    chains = []
    for leaf in leaves:
        chain = []
        cur = leaf
        for _ in range(props.chain_length):
            if cur and cur.name in valid_names:
                chain.append(cur.name)
                cur = cur.parent
            else:
                break
        if not chain:
            continue
        chains.append({
            'bones': chain,
            'tip_name': chain[0],
            'tip_world': _fr_get_bone_point(armature_obj, chain[0], props.bone_target),
        })

    return chains


def _fr_solve_linear_weights(mesh_obj, armature_obj, bone_names):
    """
    Assign vertex weights using inverse-square distance falloff to bone segments.
    Produces smooth gradients without needing Auto-Rig Pro.
    """
    groups = []
    for name in bone_names:
        vg = mesh_obj.vertex_groups.get(name) or mesh_obj.vertex_groups.new(name=name)
        groups.append(vg)

    mw_arm = armature_obj.matrix_world
    segments = []
    for name in bone_names:
        b = armature_obj.data.bones.get(name)
        if b:
            segments.append((mw_arm @ b.head_local, mw_arm @ b.tail_local))

    if not segments:
        return

    mw_mesh = mesh_obj.matrix_world
    for v in mesh_obj.data.vertices:
        v_world = mw_mesh @ v.co
        weights = []
        total = 0.0
        for head, tail in segments:
            d = _fr_segment_distance(v_world, head, tail)
            w = 1.0 / (d * d + 1e-5)
            weights.append(w)
            total += w
        if total > 0:
            for i, w in enumerate(weights):
                norm = w / total
                if norm > 0.005:
                    groups[i].add([v.index], norm, 'REPLACE')


# ============================================================================
# Feather Rigger — property group
# ============================================================================

class ARANTOOLS_PG_FeatherRig(bpy.types.PropertyGroup):
    target_armature: bpy.props.PointerProperty(
        type=bpy.types.Object,
        name="Armature",
        poll=lambda self, obj: obj.type == 'ARMATURE'
    )

    # --- How islands are matched to chains ---
    pointer_method: bpy.props.EnumProperty(
        name="Island Pointer",
        description="How the representative point for each island is determined",
        items=[
            ('CLOSEST_VERTEX', "Closest Vertex",
             "Measure from every vertex to each bone — most accurate, no setup needed"),
            ('SHARP_EDGE', "Sharp Edge Center",
             "Use the center of the first marked sharp edge on each island"),
            ('UV_Y_MAX', "Highest UV-Y Vertex",
             "Use the vertex with the highest UV Y-value (tip of feather in UV space)"),
            ('UV_Y_MIN', "Lowest UV-Y Vertex",
             "Use the vertex with the lowest UV Y-value (root of feather in UV space)"),
            ('CENTROID', "Island Centroid",
             "Use the geometric center of each island"),
        ],
        default='CLOSEST_VERTEX'
    )

    bone_target: bpy.props.EnumProperty(
        name="Measure to Bone",
        description="Which part of the bone to measure distance to",
        items=[
            ('TIP',    "Bone Tip",    "Measure to the tail / tip of each bone"),
            ('CENTER', "Bone Center", "Measure to the midpoint of each bone"),
            ('HEAD',   "Bone Head",   "Measure to the head / root of each bone"),
        ],
        default='TIP'
    )

    # --- How weights are applied ---
    weight_method: bpy.props.EnumProperty(
        name="Weight Method",
        description="How vertex weights are calculated and applied",
        items=[
            ('MATH', "Mathematical",
             "Inverse-square falloff based on distance to bone segments. No ARP needed."),
            ('ARP',  "Auto-Rig Pro",
             "Call ARP's bind operator (requires Auto-Rig Pro to be installed)"),
        ],
        default='MATH'
    )

    # --- Chain settings ---
    chain_length: bpy.props.IntProperty(
        name="Chain Length",
        description="Number of bones per chain, walking up from the leaf (tip) bone",
        default=2, min=1
    )
    exclusive_chains: bpy.props.BoolProperty(
        name="Exclusive (1 chain per island)",
        description="Each bone chain can only own one island. "
                    "OFF allows multiple islands to share the same chain.",
        default=True
    )

    # --- Bone filters ---
    use_selected_only: bpy.props.BoolProperty(
        name="Only Selected Bones",
        description="Restrict to bones currently selected in Pose or Edit Mode",
        default=False
    )
    filter_include: bpy.props.StringProperty(
        name="Include",
        description="Only use bones whose names contain any of these words (comma separated). "
                    "Leave empty to include all.",
        default=""
    )
    filter_exclude: bpy.props.StringProperty(
        name="Exclude",
        description="Ignore bones whose names contain any of these words (comma separated). "
                    "Applied after Include.",
        default=""
    )


# ============================================================================
# Feather Rigger — operator
# ============================================================================

class ARANTOOLS_OT_RigFeathers(Operator):
    """
    Auto-rig feathers/hair chains.
    Separates the mesh into loose islands, matches each island to a bone chain
    using the chosen pointer & distance method, then applies weights.
    """
    bl_idname = "arantools.rig_feathers"
    bl_label = "Rig Feathers"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return context.mode == 'OBJECT'

    def execute(self, context):
        props = context.scene.arantools_feather_rig
        armature = props.target_armature
        active_obj = context.active_object

        if not active_obj or active_obj.type != 'MESH':
            self.report({'ERROR'}, "Active object must be a Mesh.")
            return {'CANCELLED'}
        if not armature:
            self.report({'ERROR'}, "Assign an Armature in the panel.")
            return {'CANCELLED'}

        original_name = active_obj.name

        # 1. Pre-separate into loose islands
        bpy.ops.object.parent_clear(type='CLEAR')
        bpy.ops.object.mode_set(mode='OBJECT')
        bpy.ops.mesh.separate(type='LOOSE')
        meshes = [o for o in context.selected_objects if o.type == 'MESH']

        if not meshes:
            self.report({'ERROR'}, "No mesh islands found after separation.")
            return {'CANCELLED'}

        # 2. Build filtered bone chains
        chains = _fr_build_chains(armature, props)

        if not chains:
            self.report({'ERROR'}, "No bones found. Check your Bone Filters.")
            context.view_layer.objects.active = meshes[0]
            bpy.ops.object.join()
            context.active_object.name = original_name
            return {'CANCELLED'}

        # 3. Global solver: compute all (island × chain) distances, sort, assign greedily
        candidates = []
        for m_idx, mesh in enumerate(meshes):
            pointer = _fr_get_island_pointer(mesh, props.pointer_method)
            for c_idx, chain in enumerate(chains):
                bone_pt = chain['tip_world']
                if bone_pt is None:
                    continue
                if pointer is None:
                    # CLOSEST_VERTEX: measure from every vertex to this bone point
                    dist = _fr_closest_vertex_dist(mesh, bone_pt)
                else:
                    dist = (pointer - bone_pt).length
                candidates.append((dist, m_idx, c_idx))

        candidates.sort(key=lambda x: x[0])

        assigned_meshes = set()
        assigned_chains = set()
        assignments = {}  # mesh -> chain

        for dist, m_idx, c_idx in candidates:
            if m_idx in assigned_meshes:
                continue
            if props.exclusive_chains and c_idx in assigned_chains:
                continue
            assigned_meshes.add(m_idx)
            assigned_chains.add(c_idx)
            assignments[meshes[m_idx]] = chains[c_idx]

        # 4. Apply rigging
        count = 0
        try:
            for mesh, chain in assignments.items():
                context.view_layer.objects.active = mesh

                if props.weight_method == 'MATH':
                    _fr_solve_linear_weights(mesh, armature, chain['bones'])
                    bpy.ops.object.select_all(action='DESELECT')
                    mesh.select_set(True)
                    armature.select_set(True)
                    context.view_layer.objects.active = armature
                    bpy.ops.object.parent_set(type='ARMATURE', keep_transform=True)

                elif props.weight_method == 'ARP':
                    # Select the chain bones
                    context.view_layer.objects.active = armature
                    bpy.ops.object.mode_set(mode='EDIT')
                    bpy.ops.armature.select_all(action='DESELECT')
                    for bone_name in chain['bones']:
                        eb = armature.data.edit_bones.get(bone_name)
                        if eb:
                            eb.select = True
                            eb.select_head = True
                            eb.select_tail = True
                    bpy.ops.object.mode_set(mode='OBJECT')

                    # Select all vertices of this island
                    context.view_layer.objects.active = mesh
                    bpy.ops.object.mode_set(mode='EDIT')
                    bpy.ops.mesh.select_all(action='SELECT')
                    bpy.ops.object.mode_set(mode='OBJECT')

                    bpy.ops.object.select_all(action='DESELECT')
                    mesh.select_set(True)
                    armature.select_set(True)
                    context.view_layer.objects.active = armature
                    try:
                        bpy.ops.arp.bind_to_rig()
                    except AttributeError:
                        self.report({'ERROR'}, "Auto-Rig Pro is not enabled.")
                        return {'CANCELLED'}

                count += 1

        finally:
            # 5. Rejoin all islands back into one mesh
            if bpy.context.mode != 'OBJECT':
                bpy.ops.object.mode_set(mode='OBJECT')

            bpy.ops.object.select_all(action='DESELECT')
            valid = [m for m in meshes if m and m.name in bpy.data.objects]
            if valid:
                for m in valid:
                    m.select_set(True)
                context.view_layer.objects.active = valid[0]
                bpy.ops.object.join()
                final = context.active_object
                final.name = original_name

                # Ensure final mesh is parented to the armature
                bpy.ops.object.select_all(action='DESELECT')
                final.select_set(True)
                armature.select_set(True)
                context.view_layer.objects.active = armature
                bpy.ops.object.parent_set(type='ARMATURE', keep_transform=True)

        skipped = len(meshes) - count
        if skipped > 0:
            self.report({'WARNING'}, f"Rigged {count} island(s). Skipped {skipped} (no matching chain found).")
        else:
            self.report({'INFO'}, f"Rigged {count} island(s) successfully.")

        return {'FINISHED'}


# ============================================================================
# Advanced Rigging (Join/Bind workflow + ARP tools)
# ============================================================================

class ARANTOOLS_PG_AdvRigging(bpy.types.PropertyGroup):
    source_mesh: bpy.props.PointerProperty(
        name="Source Mesh",
        type=bpy.types.Object,
        description="Rigged character body to copy the armature and weights from",
        poll=lambda self, obj: obj.type == 'MESH'
    )
    target_collection: bpy.props.PointerProperty(
        name="Target Collection",
        type=bpy.types.Collection,
        description="Collection to place the joined result in"
    )
    mapping_method: bpy.props.EnumProperty(
        name="Mapping Method",
        items=[
            ('NEAREST', 'Nearest Vertex', 'Copy from the nearest vertex'),
            ('POLYINTERP_NEAREST', 'Nearest Face Interpolated', 'Interpolate from nearest face'),
            ('POLYINTERP_LNORPROJ', 'Projected Face Interpolated', 'Interpolate from projected face'),
        ],
        default='POLYINTERP_NEAREST'
    )
    chain_length: bpy.props.IntProperty(
        name="Chain Length",
        description="Number of parent bones to include in the ARP binding chain",
        default=3, min=0
    )
    sharp_edge_pointer_method: bpy.props.EnumProperty(
        name="Pointer Method",
        items=[
            ('SHARP_EDGE', 'Sharp Edge Center', 'Use the center of the first sharp edge on an island'),
            ('UV_Y_MAX', 'Highest UV-Y Vertex', 'Use the vertex with the highest UV Y-value'),
        ],
        default='SHARP_EDGE'
    )


class ARANTOOLS_OT_JoinTargets(Operator):
    """Duplicate and join selected objects into a single converted mesh"""
    bl_idname = "arantools.join_targets"
    bl_label = "Join Selected Targets"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return context.selected_objects and any(
            obj.type in {'MESH', 'CURVE', 'SURFACE', 'FONT', 'META'}
            for obj in context.selected_objects
        )

    def execute(self, context):
        if context.active_object and context.active_object.mode != 'OBJECT':
            bpy.ops.object.mode_set(mode='OBJECT')

        selected = [obj for obj in context.selected_objects
                    if obj.type in {'MESH', 'CURVE', 'SURFACE', 'FONT', 'META'}]
        if not selected:
            self.report({'ERROR'}, "No convertible objects selected.")
            return {'CANCELLED'}

        originals = selected[:]
        bpy.ops.object.duplicate(linked=False)
        for obj in originals:
            obj.hide_set(True)

        duplicated = context.selected_objects
        if not duplicated:
            self.report({'ERROR'}, "Duplication failed.")
            return {'CANCELLED'}

        context.view_layer.objects.active = duplicated[0]
        try:
            bpy.ops.object.convert(target='MESH')
        except RuntimeError as e:
            self.report({'ERROR'}, f"Conversion failed: {e}")
            bpy.ops.object.delete()
            return {'CANCELLED'}

        bpy.ops.object.join()
        joined = context.active_object
        joined.name = "Joined_Target"

        props = context.scene.arantools_adv_rigging
        if props.target_collection:
            tc = props.target_collection
            if joined.name not in tc.objects:
                tc.objects.link(joined)
            for col in [c for c in joined.users_collection if c != tc]:
                col.objects.unlink(joined)

        self.report({'INFO'}, f"Created '{joined.name}'")
        return {'FINISHED'}


class ARANTOOLS_OT_BindAndTransfer(Operator):
    """Parent the active mesh to the source's armature and transfer weights"""
    bl_idname = "arantools.bind_and_transfer"
    bl_label = "Bind and Transfer Weights"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        props = context.scene.arantools_adv_rigging
        return (props.source_mesh and context.active_object
                and context.active_object.type == 'MESH')

    def find_armature(self, obj):
        if obj.parent and obj.parent.type == 'ARMATURE':
            return obj.parent
        for mod in obj.modifiers:
            if mod.type == 'ARMATURE' and mod.object:
                return mod.object
        return None

    def execute(self, context):
        props = context.scene.arantools_adv_rigging
        source = props.source_mesh
        target = context.active_object

        if source == target:
            self.report({'ERROR'}, "Source and Target cannot be the same object.")
            return {'CANCELLED'}

        armature = self.find_armature(source)
        if not armature:
            self.report({'ERROR'}, "Source mesh has no associated armature.")
            return {'CANCELLED'}

        is_posing = armature.data.pose_position == 'POSE'
        if is_posing:
            armature.data.pose_position = 'REST'

        bpy.ops.object.select_all(action='DESELECT')
        target.select_set(True)
        armature.select_set(True)
        context.view_layer.objects.active = armature
        bpy.ops.object.parent_set(type='ARMATURE_AUTO')

        bpy.ops.object.select_all(action='DESELECT')
        target.select_set(True)
        context.view_layer.objects.active = target

        mod = target.modifiers.new(name="WeightDataTransfer", type='DATA_TRANSFER')
        mod.object = source
        mod.use_vert_data = True
        mod.data_types_verts = {'VGROUP_WEIGHTS'}
        mod.vert_mapping = props.mapping_method
        bpy.ops.object.modifier_apply(modifier=mod.name)

        if is_posing:
            armature.data.pose_position = 'POSE'

        self.report({'INFO'}, f"Weights transferred from '{source.name}' to '{target.name}'")
        return {'FINISHED'}


class ARANTOOLS_OT_WeightFromPointer(Operator):
    """For each island, find the nearest bone tip and bind a chain using ARP"""
    bl_idname = "arantools.weight_from_pointer"
    bl_label = "Weight from Pointer"
    bl_options = {'REGISTER', 'UNDO'}

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
        props = context.scene.arantools_adv_rigging
        mesh_obj = armature_obj = None

        initial_selection = context.selected_objects[:]
        active_object = context.view_layer.objects.active

        for obj in initial_selection:
            if obj.type == 'MESH':
                mesh_obj = obj
            elif obj.type == 'ARMATURE':
                armature_obj = obj

        if not mesh_obj or not armature_obj:
            self.report({'ERROR'}, "Select one Mesh and one Armature.")
            return {'CANCELLED'}

        if not hasattr(context.scene, 'arp_bind_selected_bones'):
            self.report({'ERROR'}, "Auto-Rig Pro not found. Is it enabled?")
            return {'CANCELLED'}

        if context.active_object and context.active_object.mode != 'OBJECT':
            bpy.ops.object.mode_set(mode='OBJECT')

        mesh_data = mesh_obj.data
        context.view_layer.objects.active = armature_obj
        bpy.ops.object.mode_set(mode='EDIT')
        candidate_tips = {bone.name for bone in context.selected_bones}
        bpy.ops.object.mode_set(mode='OBJECT')

        if not candidate_tips:
            self.report({'ERROR'}, "Select tip bones in Armature Edit Mode first.")
            return {'CANCELLED'}

        islands = self.get_islands(mesh_data)
        if not islands:
            self.report({'WARNING'}, "No vertex islands found.")
            return {'CANCELLED'}

        if props.sharp_edge_pointer_method == 'UV_Y_MAX' and not mesh_data.uv_layers.active:
            self.report({'ERROR'}, "Mesh has no active UV layer.")
            return {'CANCELLED'}

        if mesh_obj.vertex_groups:
            context.view_layer.objects.active = mesh_obj
            bpy.ops.object.vertex_group_remove(all=True)

        processed = 0
        for island in islands:
            pointer_world = None

            if props.sharp_edge_pointer_method == 'SHARP_EDGE':
                island_set = set(island)
                for edge in mesh_data.edges:
                    if edge.use_edge_sharp and (edge.vertices[0] in island_set or edge.vertices[1] in island_set):
                        v1 = mesh_data.vertices[edge.vertices[0]].co
                        v2 = mesh_data.vertices[edge.vertices[1]].co
                        pointer_world = mesh_obj.matrix_world @ ((v1 + v2) / 2.0)
                        break

            elif props.sharp_edge_pointer_method == 'UV_Y_MAX':
                uv_data = mesh_data.uv_layers.active.data
                vert_uvs = {loop.vertex_index: uv_data[loop.index].uv for loop in reversed(mesh_data.loops)}
                candidates = {v for v in island if v in vert_uvs}
                if candidates:
                    best = max(candidates, key=lambda v: vert_uvs[v].y)
                    pointer_world = mesh_obj.matrix_world @ mesh_data.vertices[best].co

            if pointer_world is None:
                continue

            best_tip = None
            min_dist = float('inf')
            for bone_name in candidate_tips:
                rest_bone = armature_obj.data.bones.get(bone_name)
                if rest_bone:
                    tip_world = armature_obj.matrix_world @ rest_bone.tail_local
                    dist = (tip_world - pointer_world).length
                    if dist < min_dist:
                        min_dist = dist
                        best_tip = bone_name

            if not best_tip:
                continue

            context.view_layer.objects.active = armature_obj
            bpy.ops.object.mode_set(mode='EDIT')
            bpy.ops.armature.select_all(action='DESELECT')
            current_bone = armature_obj.data.edit_bones.get(best_tip)
            if current_bone:
                armature_obj.data.edit_bones.active = current_bone
                for _ in range(props.chain_length):
                    if current_bone:
                        current_bone.select = True
                        current_bone = current_bone.parent
                    else:
                        break
            bpy.ops.object.mode_set(mode='OBJECT')

            context.view_layer.objects.active = mesh_obj
            bpy.ops.object.mode_set(mode='EDIT')
            bpy.ops.mesh.select_all(action='DESELECT')
            bpy.ops.object.mode_set(mode='OBJECT')
            for v_idx in island:
                mesh_data.vertices[v_idx].select = True

            bpy.ops.object.select_all(action='DESELECT')
            mesh_obj.select_set(True)
            armature_obj.select_set(True)
            context.view_layer.objects.active = armature_obj
            try:
                bpy.ops.arp.bind_to_rig()
            except AttributeError:
                self.report({'ERROR'}, "Auto-Rig Pro is not enabled.")
                return {'CANCELLED'}

            processed += 1

        bpy.ops.object.mode_set(mode='OBJECT')
        bpy.ops.object.select_all(action='DESELECT')
        for obj in initial_selection:
            obj.select_set(True)
        context.view_layer.objects.active = active_object

        self.report({'INFO'}, f"Processed {processed} island(s).")
        return {'FINISHED'}


class ARANTOOLS_OT_DirectArpBind(Operator):
    """Bind the current vertex/bone selection directly using Auto-Rig Pro"""
    bl_idname = "arantools.direct_arp_bind"
    bl_label = "Direct ARP Bind"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        sel = context.selected_objects
        return any(o.type == 'MESH' for o in sel) and any(o.type == 'ARMATURE' for o in sel)

    def execute(self, context):
        mesh_obj = armature_obj = None
        active = context.active_object

        if active and active.type == 'ARMATURE':
            armature_obj = active
            mesh_obj = next((o for o in context.selected_objects if o.type == 'MESH'), None)
        elif active and active.type == 'MESH':
            mesh_obj = active
            armature_obj = next((o for o in context.selected_objects if o.type == 'ARMATURE'), None)
        else:
            mesh_obj = next((o for o in context.selected_objects if o.type == 'MESH'), None)
            armature_obj = next((o for o in context.selected_objects if o.type == 'ARMATURE'), None)

        if not mesh_obj or not armature_obj:
            self.report({'ERROR'}, "Select one Mesh and one Armature.")
            return {'CANCELLED'}

        if not hasattr(context.scene, 'arp_bind_selected_bones'):
            self.report({'ERROR'}, "Auto-Rig Pro not found. Is it enabled?")
            return {'CANCELLED'}

        if context.mode != 'OBJECT':
            bpy.ops.object.mode_set(mode='OBJECT')

        context.view_layer.objects.active = armature_obj
        bpy.ops.object.mode_set(mode='EDIT')
        selected_bone_count = len(context.selected_bones)
        bpy.ops.object.mode_set(mode='OBJECT')

        if selected_bone_count == 0:
            self.report({'ERROR'}, "No bones selected in Armature Edit Mode.")
            return {'CANCELLED'}

        bpy.ops.object.select_all(action='DESELECT')
        mesh_obj.select_set(True)
        armature_obj.select_set(True)
        context.view_layer.objects.active = armature_obj

        try:
            bpy.ops.arp.bind_to_rig()
            self.report({'INFO'}, "Direct bind successful.")
        except AttributeError:
            self.report({'ERROR'}, "Auto-Rig Pro is not enabled.")
            return {'CANCELLED'}
        except Exception as e:
            self.report({'ERROR'}, f"Bind error: {e}")
            return {'CANCELLED'}

        context.view_layer.objects.active = mesh_obj
        return {'FINISHED'}


# ============================================================================
# Registration
# ============================================================================

classes = [
    ARANTOOLS_OT_select_deform_bones,
    ARANTOOLS_OT_select_bone_type,
    ARANTOOLS_OT_mirror_bones,
    ARANTOOLS_PG_FeatherRig,
    ARANTOOLS_OT_RigFeathers,
    ARANTOOLS_PG_AdvRigging,
    ARANTOOLS_OT_JoinTargets,
    ARANTOOLS_OT_BindAndTransfer,
    ARANTOOLS_OT_WeightFromPointer,
    ARANTOOLS_OT_DirectArpBind,
]


def register():
    for cls in classes:
        bpy.utils.register_class(cls)
    bpy.types.Scene.arantools_feather_rig = bpy.props.PointerProperty(type=ARANTOOLS_PG_FeatherRig)
    bpy.types.Scene.arantools_adv_rigging = bpy.props.PointerProperty(type=ARANTOOLS_PG_AdvRigging)


def unregister():
    del bpy.types.Scene.arantools_adv_rigging
    del bpy.types.Scene.arantools_feather_rig
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
