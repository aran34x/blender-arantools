import bpy
import os
from bpy.types import Operator


# ============================================================================
# Normal-map baking (high → low, by naming convention)
# ============================================================================
#
# The artist names a low-poly mesh "Object" and its high-poly counterpart
# "Object<high_suffix>" (default "_High"). This tool scans for those pairs,
# bakes a tangent-space normal map from each high onto its low (Cycles
# selected-to-active) OR from each low's own Multires modifier, writes the PNG
# to an output folder, then restores the scene to exactly its prior state.
#
# Output can be per-object, grouped by material, or grouped by hand — grouped
# members all bake into one shared texture for texture packing.


# ── Output folder ───────────────────────────────────────────────────────────

def _resolve_output_folder(props):
    """Return an absolute output folder, or "" if it can't be resolved."""
    raw = props.output_folder.strip()
    if raw:
        return bpy.path.abspath(raw)
    if bpy.data.filepath:
        return os.path.join(os.path.dirname(bpy.data.filepath), "Bakes")
    return ""


def _gpu_backend_label():
    """Return a short description of the enabled Cycles GPU backend
    (e.g. 'OPTIX: NVIDIA RTX 4090'), or "" if no GPU device is enabled."""
    try:
        prefs = bpy.context.preferences.addons['cycles'].preferences
        backend = prefs.compute_device_type
        if not backend or backend == 'NONE':
            return ""
        try:
            prefs.get_devices()
        except Exception:  # noqa: BLE001
            pass
        active = [d.name for d in prefs.devices
                  if getattr(d, 'use', False) and getattr(d, 'type', '') == backend]
        if active:
            return f"{backend}: {', '.join(active)}"
        return backend
    except (KeyError, AttributeError):
        return ""


# ── Pair / target resolution ─────────────────────────────────────────────────

def _find_bake_pairs(seeds, pool, props):
    """Return [(low, high, cage), ...]. `seeds` drive which pairs to bake;
    `pool` is where partners are resolved. Selecting either member finds the
    other (the seed is normalised to its low base name)."""
    high_suffix = props.high_suffix
    cage_suffix = props.cage_suffix
    by_name = {o.name: o for o in pool if o.type == 'MESH'}

    pairs = []
    seen = set()
    for seed in seeds:
        if seed.type != 'MESH':
            continue
        if high_suffix and seed.name.endswith(high_suffix):
            base = seed.name[:-len(high_suffix)]
        else:
            base = seed.name
        if base in seen:
            continue
        low = by_name.get(base)
        high = by_name.get(base + high_suffix) if high_suffix else None
        if low is None or high is None:
            continue
        seen.add(base)
        cage = None
        if props.use_cage and cage_suffix:
            cage = by_name.get(base + cage_suffix)
        pairs.append((low, high, cage))

    pairs.sort(key=lambda p: p[0].name.lower())
    return pairs


def _find_multires_targets(seeds, pool, props):
    """Return [low, ...] meshes that carry a Multires modifier (selecting the
    high resolves to its low; a lone low with multires is also picked up)."""
    high_suffix = props.high_suffix
    by_name = {o.name: o for o in pool if o.type == 'MESH'}
    targets = []
    seen = set()
    for seed in seeds:
        if seed.type != 'MESH':
            continue
        if high_suffix and seed.name.endswith(high_suffix):
            base = seed.name[:-len(high_suffix)]
        else:
            base = seed.name
        if base in seen:
            continue
        low = by_name.get(base)
        if low is None:
            continue
        if not any(m.type == 'MULTIRES' for m in low.modifiers):
            continue
        seen.add(base)
        targets.append(low)
    targets.sort(key=lambda o: o.name.lower())
    return targets


def _selected_mesh_objects(context):
    return [o for o in context.selected_objects if o.type == 'MESH']


def _resolve_group_members(group, pool, props, multires_mode):
    """Resolve a manual group's stored objects into bakeable members
    [(low, high, cage), ...], deduped by low name."""
    high_suffix = props.high_suffix
    cage_suffix = props.cage_suffix
    by_name = {o.name: o for o in pool if o.type == 'MESH'}

    members = []
    seen = set()
    for entry in group.members:
        obj = entry.obj
        if obj is None:
            continue
        name = obj.name
        if high_suffix and name.endswith(high_suffix):
            base = name[:-len(high_suffix)]
        else:
            base = name
        if base in seen:
            continue
        low = by_name.get(base)
        if low is None:
            continue
        if multires_mode:
            if not any(m.type == 'MULTIRES' for m in low.modifiers):
                continue
            seen.add(base)
            members.append((low, None, None))
        else:
            high = by_name.get(base + high_suffix) if high_suffix else None
            if high is None:
                continue
            seen.add(base)
            cage = by_name.get(base + cage_suffix) \
                if props.use_cage and cage_suffix else None
            members.append((low, high, cage))
    return members


# ── Image helpers ─────────────────────────────────────────────────────────────

def _flip_green_channel(img):
    """Invert the green channel of an image in place (OpenGL → DirectX)."""
    n = len(img.pixels)
    try:
        import numpy as np
        buf = np.empty(n, dtype=np.float32)
        img.pixels.foreach_get(buf)
        buf[1::4] = 1.0 - buf[1::4]
        img.pixels.foreach_set(buf)
    except ImportError:
        px = list(img.pixels)
        for i in range(1, n, 4):
            px[i] = 1.0 - px[i]
        img.pixels.foreach_set(px)
    img.update()


def _get_or_create_image(name, res):
    """Reuse an existing image of this name or make a fresh one.
    Returns (image, created_new)."""
    img = bpy.data.images.get(name)
    if img is not None:
        if tuple(img.size) != (res, res):
            try:
                img.scale(res, res)
            except RuntimeError:
                pass
        created = False
    else:
        img = bpy.data.images.new(name, width=res, height=res,
                                  alpha=False, float_buffer=False)
        created = True
    img.colorspace_settings.name = 'Non-Color'
    return img, created


# Tag so re-bakes update our nodes in place instead of stacking new ones.
_NODE_TAG = "arantools_normal"


def _prepare_target_material(obj):
    """Return the material to wire the baked normal into, creating one if the
    object has none and making a single-user copy if it's shared."""
    mesh = obj.data
    if mesh.materials:
        idx = min(obj.active_material_index, len(mesh.materials) - 1)
        mat = mesh.materials[idx]
        if mat is None:
            mat = bpy.data.materials.new(f"{obj.name}_Mat")
            mesh.materials[idx] = mat
        elif mat.users > 1:
            mat = mat.copy()
            mesh.materials[idx] = mat
    else:
        mat = bpy.data.materials.new(f"{obj.name}_Mat")
        mesh.materials.append(mat)
    return mat


def _wire_normal_map(mat, img, flip_green):
    """Build TexImage → (green flip) → Normal Map → Principled.Normal on `mat`.
    Our nodes are tagged so re-baking replaces them rather than piling up."""
    mat.use_nodes = True
    nt = mat.node_tree
    nodes, links = nt.nodes, nt.links

    bsdf = next((n for n in nodes if n.type == 'BSDF_PRINCIPLED'), None)
    if bsdf is None:
        out = next((n for n in nodes if n.type == 'OUTPUT_MATERIAL'), None)
        if out is None:
            out = nodes.new('ShaderNodeOutputMaterial')
            out.location = (600, 0)
        bsdf = nodes.new('ShaderNodeBsdfPrincipled')
        bsdf.location = (300, 0)
        links.new(bsdf.outputs['BSDF'], out.inputs['Surface'])

    for n in list(nodes):
        if n.get(_NODE_TAG):
            nodes.remove(n)

    tex = nodes.new('ShaderNodeTexImage')
    tex[_NODE_TAG] = 1
    tex.image = img
    tex.label = "Baked Normal"
    tex.location = (-900, -300)

    nm = nodes.new('ShaderNodeNormalMap')
    nm[_NODE_TAG] = 1
    nm.space = 'TANGENT'
    nm.location = (-200, -300)

    if flip_green:
        sep = nodes.new('ShaderNodeSeparateColor')
        sep[_NODE_TAG] = 1
        sep.location = (-650, -300)
        inv = nodes.new('ShaderNodeMath')
        inv[_NODE_TAG] = 1
        inv.operation = 'SUBTRACT'
        inv.inputs[0].default_value = 1.0
        inv.location = (-470, -430)
        comb = nodes.new('ShaderNodeCombineColor')
        comb[_NODE_TAG] = 1
        comb.location = (-400, -300)
        links.new(tex.outputs['Color'], sep.inputs['Color'])
        links.new(sep.outputs['Red'],   comb.inputs['Red'])
        links.new(sep.outputs['Green'], inv.inputs[1])
        links.new(inv.outputs['Value'], comb.inputs['Green'])
        links.new(sep.outputs['Blue'],  comb.inputs['Blue'])
        links.new(comb.outputs['Color'], nm.inputs['Color'])
    else:
        links.new(tex.outputs['Color'], nm.inputs['Color'])

    links.new(nm.outputs['Normal'], bsdf.inputs['Normal'])


# ============================================================================
# Property groups
# ============================================================================

class ARANTOOLS_PG_BakeGroupMember(bpy.types.PropertyGroup):
    obj: bpy.props.PointerProperty(
        name="Object",
        type=bpy.types.Object,
        poll=lambda self, obj: obj.type == 'MESH',
    )


class ARANTOOLS_PG_BakeGroup(bpy.types.PropertyGroup):
    name: bpy.props.StringProperty(
        name="Texture Name",
        description="Output texture base name for this group. All members bake "
                    "into this one image (the suffix is added automatically)",
        default="Texture",
    )
    members: bpy.props.CollectionProperty(type=ARANTOOLS_PG_BakeGroupMember)
    expanded: bpy.props.BoolProperty(default=True)


class ARANTOOLS_PG_BakedResult(bpy.types.PropertyGroup):
    name: bpy.props.StringProperty()      # image / texture name
    filepath: bpy.props.StringProperty()  # absolute path to the saved PNG


class ARANTOOLS_PG_NormalBake(bpy.types.PropertyGroup):
    bake_mode: bpy.props.EnumProperty(
        name="Method",
        description="How the normal map is captured",
        items=[
            ('S2A', "Selected → Active",
             "Cast rays from the low mesh outward to the high mesh. Needs a "
             "high/low pair. Uses extrusion or a cage"),
            ('MULTIRES', "From Multires",
             "Bake the low mesh's own Multires modifier detail onto its base "
             "level. No ray casting, so it avoids cage/extrusion artifacts. "
             "Build the multires first with the Multires from High Poly tool"),
        ],
        default='S2A',
    )
    high_suffix: bpy.props.StringProperty(
        name="High Suffix",
        description="Suffix that marks the high-poly mesh. "
                    "Low 'Trunk' pairs with high 'Trunk_High'",
        default="_High",
    )
    cage_suffix: bpy.props.StringProperty(
        name="Cage Suffix",
        description="Suffix of an optional cage mesh (e.g. 'Trunk_Cage'). "
                    "Only used when Use Cage is on",
        default="_Cage",
    )
    output_folder: bpy.props.StringProperty(
        name="Output Folder",
        description="Folder where the baked normal maps are written. "
                    "Leave empty to use a 'Bakes' folder next to the .blend",
        default="",
        subtype='DIR_PATH',
    )
    image_suffix: bpy.props.StringProperty(
        name="Map Suffix",
        description="Appended to the texture name for the output file "
                    "(e.g. 'Trunk' + '_N' → Trunk_N.png)",
        default="_N",
    )
    resolution: bpy.props.EnumProperty(
        name="Resolution",
        description="Output texture size (square)",
        items=[
            ('512',  "512",  ""),
            ('1024', "1024", ""),
            ('2048', "2048", ""),
            ('4096', "4096", ""),
        ],
        default='2048',
    )
    samples: bpy.props.IntProperty(
        name="Samples",
        description="Cycles samples for the bake. Normal maps need very few; "
                    "1 is usually enough and fastest",
        default=1, min=1, max=64,
    )
    margin: bpy.props.IntProperty(
        name="Margin",
        description="Pixel bleed past UV island edges. Prevents seams when "
                    "the texture is mip-mapped or filtered",
        default=16, min=0, max=64,
    )
    cage_extrusion: bpy.props.FloatProperty(
        name="Extrusion",
        description="How far the low mesh is inflated along its normals before "
                    "rays are fired inward at the high mesh. Raise until the "
                    "whole high surface is captured",
        default=0.05, min=0.0, soft_max=1.0, subtype='DISTANCE',
    )
    max_ray_distance: bpy.props.FloatProperty(
        name="Max Ray Dist",
        description="How far rays travel inward. 0 = unlimited (auto). "
                    "Raise to avoid missing geometry, lower to avoid catching "
                    "the wrong surface",
        default=0.0, min=0.0, soft_max=1.0, subtype='DISTANCE',
    )
    use_cage: bpy.props.BoolProperty(
        name="Use Cage",
        description="Use a per-pair cage mesh ('<name><cage_suffix>') to control "
                    "ray direction instead of plain extrusion",
        default=False,
    )
    flip_green: bpy.props.BoolProperty(
        name="Flip Green (DirectX / Unreal)",
        description="Invert the green channel so the map reads as DirectX-style "
                    "(Unreal Engine, 3ds Max). Off = OpenGL (Blender, Unity)",
        default=True,
    )
    align_pivots: bpy.props.BoolProperty(
        name="Overlap Pivots",
        description="Temporarily move each high mesh (and its cage) so its "
                    "origin coincides with the low mesh's origin during the "
                    "bake, then restore. Lets you keep the pair moved apart "
                    "in the viewport — they only overlap while baking",
        default=True,
    )
    assign_to_material: bpy.props.BoolProperty(
        name="Assign to Material",
        description="Wire the baked map into the low mesh's material (Principled "
                    "BSDF → Normal) so you see it immediately in Material Preview / "
                    "Rendered. Makes a single-user copy of a shared material first "
                    "so other objects keep their own normals",
        default=True,
    )
    cycles_device: bpy.props.EnumProperty(
        name="Device",
        description="Which device Cycles uses for the bake. GPU needs a backend "
                    "enabled in Preferences > System > Cycles Render Devices",
        items=[
            ('GPU', "GPU Compute", "Bake on the GPU (CUDA / OptiX / HIP / Metal / oneAPI)"),
            ('CPU', "CPU",         "Bake on the CPU"),
        ],
        default='GPU',
    )

    # ── Multires-setup tool (build a multires on the low from the high) ──
    multires_subdivisions: bpy.props.IntProperty(
        name="Subdivisions",
        description="How many Catmull-Clark levels to add to the Multires "
                    "modifier before shrinkwrapping it to the high mesh",
        default=4, min=1, max=8,
    )
    multires_wrap_method: bpy.props.EnumProperty(
        name="Projection",
        description="How the subdivided multires is snapped onto the high mesh",
        items=[
            ('PROJECT', "Project",
             "Cast rays along vertex normals (both directions) onto the high "
             "mesh. Best general result for matching surfaces"),
            ('NEAREST_SURFACEPOINT', "Nearest Surface",
             "Snap each vertex to the closest point on the high surface"),
            ('NEAREST_VERTEX', "Nearest Vertex",
             "Snap each vertex to the closest high-mesh vertex"),
            ('TARGET_PROJECT', "Target Normal Project",
             "Project using the high mesh's interpolated normals"),
        ],
        default='PROJECT',
    )
    multires_project_limit: bpy.props.FloatProperty(
        name="Project Limit",
        description="Max distance a ray may travel in Project mode "
                    "(0 = unlimited)",
        default=0.0, min=0.0, soft_max=1.0, subtype='DISTANCE',
    )
    scope: bpy.props.EnumProperty(
        name="Scope",
        description="Which objects to scan for high/low pairs",
        items=[
            ('SELECTED', "Selected", "Only the objects selected in the viewport"),
            ('SCENE',    "Scene",    "Every mesh in the current scene"),
        ],
        default='SCENE',
    )
    grouping: bpy.props.EnumProperty(
        name="Group Into Textures",
        description="How baked maps are grouped into output textures. For "
                    "shared textures the members' UVs must occupy different "
                    "regions of 0-1 space (texture packing)",
        items=[
            ('OBJECT', "Per Object",
             "One texture per low mesh, named after the mesh"),
            ('MATERIAL', "By Material",
             "One texture per material — every low mesh using that material "
             "bakes into it, named after the material"),
            ('MANUAL', "Manual Groups",
             "Define groups by hand; each group bakes into one shared texture"),
        ],
        default='OBJECT',
    )
    bake_groups: bpy.props.CollectionProperty(type=ARANTOOLS_PG_BakeGroup)
    bake_group_index: bpy.props.IntProperty(default=0)
    # Textures produced by the most recent bake (for the "Open" buttons).
    last_baked: bpy.props.CollectionProperty(type=ARANTOOLS_PG_BakedResult)


# ============================================================================
# Operator — bake
# ============================================================================

class ARANTOOLS_OT_NormalBake(Operator):
    """Scan for <name> / <name><suffix> mesh pairs and bake a tangent-space
normal map onto each low mesh, writing a PNG per texture to the output folder,
then restore the scene."""
    bl_idname = "arantools.normal_bake"
    bl_label = "Bake Normal Maps"
    bl_options = {'REGISTER'}

    def execute(self, context):
        props = context.scene.arantools_normal_bake
        scene = context.scene

        pool = list(scene.objects)
        multires_mode = props.bake_mode == 'MULTIRES'
        suffix = props.image_suffix

        # Build a list of bake jobs: (image_name, [(low, high, cage), ...]).
        jobs = []
        if props.grouping == 'MANUAL':
            if not props.bake_groups:
                self.report({'WARNING'},
                            "No bake groups defined. Add one, or switch "
                            "grouping to Per Object / By Material.")
                return {'CANCELLED'}
            claimed = set()  # a low maps to exactly one output texture
            for group in props.bake_groups:
                name = group.name.strip()
                if not name:
                    continue
                members = _resolve_group_members(group, pool, props,
                                                 multires_mode)
                members = [m for m in members if m[0].name not in claimed]
                for m in members:
                    claimed.add(m[0].name)
                if members:
                    jobs.append((f"{name}{suffix}", members))
            if not jobs:
                self.report({'WARNING'},
                            "No bakeable members in any group (check naming / "
                            "multires modifiers).")
                return {'CANCELLED'}
        else:
            members = self._scope_members(context, props, pool, multires_mode)
            if not members:
                if multires_mode:
                    self.report({'WARNING'},
                                "No meshes with a Multires modifier found in "
                                "scope. Build one with 'Multires from High "
                                "Poly' first.")
                else:
                    self.report({'WARNING'},
                                f"No high/low pairs found (looking for '<name>' "
                                f"+ '{props.high_suffix}').")
                return {'CANCELLED'}

            if props.grouping == 'MATERIAL':
                buckets = {}
                no_mat = []
                for m in members:
                    mat = m[0].active_material
                    if mat is None:
                        no_mat.append(m[0].name)
                        continue
                    buckets.setdefault(mat.name, []).append(m)
                jobs = [(f"{mat_name}{suffix}", mem)
                        for mat_name, mem in buckets.items()]
                if no_mat:
                    print(f"[AranTools] Skipped (no material): "
                          f"{', '.join(no_mat)}")
                if not jobs:
                    self.report({'WARNING'},
                                "No low meshes have a material to group by.")
                    return {'CANCELLED'}
            else:  # OBJECT
                jobs = [(f"{m[0].name}{suffix}", [m]) for m in members]

        folder = _resolve_output_folder(props)
        if not folder:
            self.report({'ERROR'},
                        "No output folder set — pick a folder, or save the "
                        ".blend to use the default 'Bakes' folder beside it.")
            return {'CANCELLED'}
        try:
            os.makedirs(folder, exist_ok=True)
        except OSError as e:
            self.report({'ERROR'}, f"Cannot create output folder: {e}")
            return {'CANCELLED'}

        res = int(props.resolution)

        # ── Record everything we are about to change ──────────────────────
        view_layer = context.view_layer
        prev_active = view_layer.objects.active
        prev_selected = [o for o in scene.objects if o.select_get()]
        prev_mode = prev_active.mode if prev_active else 'OBJECT'

        if prev_active and prev_active.mode != 'OBJECT':
            bpy.ops.object.mode_set(mode='OBJECT')

        prev_engine = scene.render.engine
        bake = scene.render.bake
        prev_bake = {
            'use_selected_to_active': bake.use_selected_to_active,
            'cage_extrusion':         bake.cage_extrusion,
            'max_ray_distance':       bake.max_ray_distance,
            'use_cage':               bake.use_cage,
            'cage_object':            bake.cage_object,
            'margin':                 bake.margin,
            'margin_type':            bake.margin_type,
            'use_clear':              bake.use_clear,
            'normal_space':           bake.normal_space,
            'normal_r':               bake.normal_r,
            'normal_g':               bake.normal_g,
            'normal_b':               bake.normal_b,
        }
        for key in ('use_multires', 'type', 'use_lores_mesh'):
            if hasattr(bake, key):
                prev_bake[key] = getattr(bake, key)
        prev_samples = None
        prev_device = None

        temp_images = []
        temp_mats = []
        restore_indices = []   # (mesh, original material_index array)
        restore_matrices = []  # (object, original matrix_world)

        baked, failed = [], []

        try:
            scene.render.engine = 'CYCLES'
            prev_samples = scene.cycles.samples
            scene.cycles.samples = props.samples
            prev_device = scene.cycles.device
            scene.cycles.device = props.cycles_device

            bake.margin = props.margin

            if multires_mode:
                if hasattr(bake, 'use_multires'):
                    bake.use_multires = True
                if hasattr(bake, 'type'):
                    bake.type = 'NORMALS'
                if hasattr(bake, 'use_lores_mesh'):
                    bake.use_lores_mesh = False
                bake.use_selected_to_active = False
            else:
                if hasattr(bake, 'use_multires'):
                    bake.use_multires = False
                bake.use_selected_to_active = True
                bake.cage_extrusion = props.cage_extrusion
                bake.max_ray_distance = props.max_ray_distance
                bake.normal_space = 'TANGENT'
                bake.normal_r = 'POS_X'
                bake.normal_g = 'NEG_Y' if props.flip_green else 'POS_Y'
                bake.normal_b = 'POS_Z'

            for img_name, members in jobs:
                try:
                    self._bake_job(context, img_name, members, props, res,
                                   folder, multires_mode, temp_images,
                                   temp_mats, restore_indices, restore_matrices)
                    baked.append(img_name)
                except Exception as e:  # noqa: BLE001 — report and continue
                    failed.append((img_name, str(e)))
                    print(f"[AranTools] Bake failed for '{img_name}': {e}")
        finally:
            for obj, mat_world in restore_matrices:
                try:
                    obj.matrix_world = mat_world
                except ReferenceError:
                    pass

            for mesh, orig in restore_indices:
                try:
                    if len(mesh.polygons) == len(orig):
                        mesh.polygons.foreach_set('material_index', orig)
                    mesh.materials.pop(index=len(mesh.materials) - 1)
                except (RuntimeError, ReferenceError):
                    pass
            for mat in temp_mats:
                try:
                    bpy.data.materials.remove(mat)
                except (RuntimeError, ReferenceError):
                    pass
            for img in temp_images:
                try:
                    bpy.data.images.remove(img)
                except (RuntimeError, ReferenceError):
                    pass

            for key, val in prev_bake.items():
                try:
                    setattr(bake, key, val)
                except (TypeError, ReferenceError):
                    pass
            if prev_samples is not None:
                scene.cycles.samples = prev_samples
            if prev_device is not None:
                scene.cycles.device = prev_device
            scene.render.engine = prev_engine

            for o in scene.objects:
                o.select_set(o in prev_selected)
            view_layer.objects.active = prev_active
            if prev_active and prev_mode != 'OBJECT':
                try:
                    bpy.ops.object.mode_set(mode=prev_mode)
                except RuntimeError:
                    pass

        # ── Remember what we produced (for the "Open Baked Texture" UI) ────
        props.last_baked.clear()
        for img_name in baked:
            entry = props.last_baked.add()
            entry.name = img_name
            entry.filepath = os.path.join(folder, f"{img_name}.png")

        if failed and not baked:
            self.report({'ERROR'},
                        f"All {len(failed)} bake(s) failed — see System Console.")
            return {'CANCELLED'}
        msg = f"Baked {len(baked)} normal map(s) → {folder}"
        if failed:
            msg += f"  ({len(failed)} failed — see console)"
        self.report({'INFO'}, msg)
        return {'FINISHED'}

    # ------------------------------------------------------------------ #

    def _scope_members(self, context, props, pool, multires_mode):
        """Resolve the scope (selected / scene) into bakeable members."""
        if props.scope == 'SELECTED':
            seeds = list(context.selected_objects)
        else:
            seeds = pool
        if multires_mode:
            return [(low, None, None)
                    for low in _find_multires_targets(seeds, pool, props)]
        return list(_find_bake_pairs(seeds, pool, props))

    # ------------------------------------------------------------------ #

    def _bake_job(self, context, img_name, members, props, res, folder,
                  multires_mode, temp_images, temp_mats, restore_indices,
                  restore_matrices):
        """Bake every member of one job into a single shared image, then save
        and assign it. The first member clears the image, the rest accumulate
        into it (texture packing). Raises on failure."""
        img, created = _get_or_create_image(img_name, res)
        if not props.assign_to_material and created:
            temp_images.append(img)

        targets = {}
        if props.assign_to_material:
            for low, _h, _c in members:
                if low not in targets:
                    targets[low] = _prepare_target_material(low)

        for i, (low, high, cage) in enumerate(members):
            do_clear = (i == 0)
            if multires_mode:
                self._bake_one_multires(context, low, img, do_clear,
                                        temp_mats, restore_indices)
            else:
                self._bake_one_pair(context, low, high, cage, img, do_clear,
                                    props, temp_mats, restore_indices,
                                    restore_matrices)

        if multires_mode and props.flip_green:
            _flip_green_channel(img)

        filepath = os.path.join(folder, f"{img_name}.png")
        img.filepath_raw = filepath
        img.file_format = 'PNG'
        img.save()

        for mat in targets.values():
            _wire_normal_map(mat, img, props.flip_green)

    # ------------------------------------------------------------------ #

    def _setup_bake_target(self, low, img, temp_mats, restore_indices):
        """Give the low mesh a temp single-slot material whose active node is
        `img`, reassigning all faces to it."""
        mat = bpy.data.materials.new(f"_AranBake_{low.name}")
        mat.use_nodes = True
        temp_mats.append(mat)
        node = mat.node_tree.nodes.new('ShaderNodeTexImage')
        node.image = img
        node.select = True
        mat.node_tree.nodes.active = node

        mesh = low.data
        orig = [0] * len(mesh.polygons)
        mesh.polygons.foreach_get('material_index', orig)
        restore_indices.append((mesh, orig))
        mesh.materials.append(mat)
        temp_index = len(mesh.materials) - 1
        mesh.polygons.foreach_set(
            'material_index', [temp_index] * len(mesh.polygons))

    def _bake_one_pair(self, context, low, high, cage, img, do_clear, props,
                       temp_mats, restore_indices, restore_matrices):
        """Selected-to-active bake of one high→low pair into `img`."""
        scene = context.scene
        view_layer = context.view_layer

        if props.align_pivots:
            target = low.matrix_world.translation.copy()
            for obj in (high, cage):
                if obj is None:
                    continue
                restore_matrices.append((obj, obj.matrix_world.copy()))
                m = obj.matrix_world.copy()
                m.translation = target
                obj.matrix_world = m
            view_layer.update()

        self._setup_bake_target(low, img, temp_mats, restore_indices)

        for o in scene.objects:
            o.select_set(False)
        high.select_set(True)
        low.select_set(True)
        view_layer.objects.active = low

        bake = scene.render.bake
        bake.use_clear = do_clear
        if cage is not None:
            bake.use_cage = True
            bake.cage_object = cage
        else:
            bake.use_cage = False
            bake.cage_object = None

        result = bpy.ops.object.bake(type='NORMAL')
        if 'FINISHED' not in result:
            raise RuntimeError(f"bake operator returned {result}")

    def _bake_one_multires(self, context, low, img, do_clear,
                           temp_mats, restore_indices):
        """Multires bake of one low mesh's own detail into `img`."""
        scene = context.scene
        view_layer = context.view_layer
        mesh = low.data

        if not mesh.uv_layers:
            raise RuntimeError("mesh has no UV map")
        mod = next((m for m in low.modifiers if m.type == 'MULTIRES'), None)
        if mod is None:
            raise RuntimeError("no Multires modifier")
        mod.show_viewport = True
        if mod.total_levels:
            mod.levels = mod.total_levels

        self._setup_bake_target(low, img, temp_mats, restore_indices)

        for o in scene.objects:
            o.select_set(False)
        low.select_set(True)
        view_layer.objects.active = low

        scene.render.bake.use_clear = do_clear
        result = bpy.ops.object.bake_image()
        if 'FINISHED' not in result:
            raise RuntimeError(f"bake_image operator returned {result}")


# ============================================================================
# Operator — open output / texture
# ============================================================================

class ARANTOOLS_OT_OpenBakeFolder(Operator):
    """Open the normal-map output folder in the system file browser"""
    bl_idname = "arantools.open_bake_folder"
    bl_label = "Open Output Folder"
    bl_options = {'REGISTER'}

    def execute(self, context):
        props = context.scene.arantools_normal_bake
        folder = _resolve_output_folder(props)
        if not folder:
            self.report({'ERROR'},
                        "No output folder set — pick a folder, or save the "
                        ".blend first.")
            return {'CANCELLED'}
        try:
            os.makedirs(folder, exist_ok=True)
        except OSError as e:
            self.report({'ERROR'}, f"Cannot create output folder: {e}")
            return {'CANCELLED'}
        bpy.ops.wm.path_open(filepath=folder)
        return {'FINISHED'}


class ARANTOOLS_OT_OpenBakedTexture(Operator):
    """Show a texture from the last bake in an Image Editor (or the system
image viewer if no editor is available)"""
    bl_idname = "arantools.open_baked_texture"
    bl_label = "Open Baked Texture"
    bl_options = {'REGISTER'}

    index: bpy.props.IntProperty(default=0)

    def execute(self, context):
        props = context.scene.arantools_normal_bake
        if not (0 <= self.index < len(props.last_baked)):
            self.report({'ERROR'}, "No baked texture to open.")
            return {'CANCELLED'}
        entry = props.last_baked[self.index]
        path = entry.filepath
        if not path or not os.path.isfile(path):
            self.report({'ERROR'}, f"File not found: {path}")
            return {'CANCELLED'}

        img = bpy.data.images.get(entry.name)
        if img is None:
            try:
                img = bpy.data.images.load(path, check_existing=True)
            except RuntimeError as e:
                self.report({'ERROR'}, f"Could not load image: {e}")
                return {'CANCELLED'}
        else:
            img.reload()

        for area in context.screen.areas:
            if area.type == 'IMAGE_EDITOR':
                area.spaces.active.image = img
                self.report({'INFO'}, f"Opened {entry.name}")
                return {'FINISHED'}
        try:
            bpy.ops.wm.window_new()
            win = context.window_manager.windows[-1]
            area = win.screen.areas[0]
            area.type = 'IMAGE_EDITOR'
            area.spaces.active.image = img
            self.report({'INFO'}, f"Opened {entry.name} in a new window")
            return {'FINISHED'}
        except RuntimeError:
            bpy.ops.wm.path_open(filepath=path)
            return {'FINISHED'}


# ============================================================================
# Operator — multires setup
# ============================================================================

class ARANTOOLS_OT_MultiresFromHigh(Operator):
    """For each <name> / <name><suffix> pair, add a Multires modifier to the
low mesh, subdivide it, and shrinkwrap the subdivided detail onto the high
mesh — baking the high-poly shape into the multires. Leaves the low mesh with
a multires modifier ready for a 'From Multires' bake."""
    bl_idname = "arantools.multires_from_high"
    bl_label = "Build Multires from High Poly"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        props = context.scene.arantools_normal_bake
        scene = context.scene
        view_layer = context.view_layer

        pool = list(scene.objects)
        if props.scope == 'SELECTED':
            seeds = list(context.selected_objects)
        else:
            seeds = pool
        pairs = _find_bake_pairs(seeds, pool, props)
        if not pairs:
            self.report({'WARNING'},
                        f"No high/low pairs found (looking for '<name>' + "
                        f"'{props.high_suffix}').")
            return {'CANCELLED'}

        prev_active = view_layer.objects.active
        prev_selected = [o for o in scene.objects if o.select_get()]
        prev_mode = prev_active.mode if prev_active else 'OBJECT'
        if prev_active and prev_active.mode != 'OBJECT':
            bpy.ops.object.mode_set(mode='OBJECT')

        done, failed = [], []
        try:
            for low, high, _cage in pairs:
                try:
                    self._build(context, low, high, props)
                    done.append(low.name)
                except Exception as e:  # noqa: BLE001
                    failed.append((low.name, str(e)))
                    print(f"[AranTools] Multires setup failed for "
                          f"'{low.name}': {e}")
        finally:
            for o in scene.objects:
                o.select_set(o in prev_selected)
            view_layer.objects.active = prev_active
            if prev_active and prev_mode != 'OBJECT':
                try:
                    bpy.ops.object.mode_set(mode=prev_mode)
                except RuntimeError:
                    pass

        if failed and not done:
            self.report({'ERROR'},
                        f"All {len(failed)} setup(s) failed — see console.")
            return {'CANCELLED'}
        msg = f"Built multires on {len(done)} mesh(es)"
        if failed:
            msg += f"  ({len(failed)} failed — see console)"
        self.report({'INFO'}, msg)
        return {'FINISHED'}

    def _build(self, context, low, high, props):
        view_layer = context.view_layer

        for o in context.scene.objects:
            o.select_set(False)
        low.select_set(True)
        view_layer.objects.active = low

        mod = next((m for m in low.modifiers if m.type == 'MULTIRES'), None)
        if mod is None:
            mod = low.modifiers.new(name="Multires", type='MULTIRES')
        mod.show_viewport = True

        target_levels = props.multires_subdivisions
        guard = 0
        while mod.total_levels < target_levels and guard < target_levels + 2:
            bpy.ops.object.multires_subdivide(modifier=mod.name,
                                              mode='CATMULL_CLARK')
            guard += 1
        mod.levels = mod.total_levels
        mod.sculpt_levels = mod.total_levels

        sw = low.modifiers.new(name="_AranShrinkwrap", type='SHRINKWRAP')
        sw.target = high
        sw.wrap_method = props.multires_wrap_method
        if props.multires_wrap_method == 'PROJECT':
            sw.use_negative_direction = True
            sw.use_positive_direction = True
            sw.project_limit = props.multires_project_limit

        try:
            bpy.ops.object.modifier_apply(modifier=sw.name)
        except RuntimeError as e:
            if sw.name in low.modifiers:
                low.modifiers.remove(sw)
            raise RuntimeError(f"could not apply shrinkwrap: {e}")


# ============================================================================
# Bake-group management operators
# ============================================================================

class ARANTOOLS_OT_BakeGroup_AddFromSelection(Operator):
    """Create a new bake group from the selected meshes. The texture name
defaults to the first object's name."""
    bl_idname = "arantools.bakegroup_add_from_selection"
    bl_label = "Add Group from Selection"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        props = context.scene.arantools_normal_bake
        meshes = _selected_mesh_objects(context)
        if not meshes:
            self.report({'WARNING'}, "Select one or more meshes first.")
            return {'CANCELLED'}
        group = props.bake_groups.add()
        group.name = meshes[0].name
        for m in meshes:
            group.members.add().obj = m
        props.bake_group_index = len(props.bake_groups) - 1
        return {'FINISHED'}


class ARANTOOLS_OT_BakeGroup_Add(Operator):
    """Add a new empty bake group."""
    bl_idname = "arantools.bakegroup_add"
    bl_label = "Add Empty Group"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        props = context.scene.arantools_normal_bake
        group = props.bake_groups.add()
        group.name = f"Texture_{len(props.bake_groups):02d}"
        props.bake_group_index = len(props.bake_groups) - 1
        return {'FINISHED'}


class ARANTOOLS_OT_BakeGroup_Remove(Operator):
    """Delete this bake group."""
    bl_idname = "arantools.bakegroup_remove"
    bl_label = "Delete Group"
    bl_options = {'REGISTER', 'UNDO'}

    index: bpy.props.IntProperty()

    def execute(self, context):
        props = context.scene.arantools_normal_bake
        if 0 <= self.index < len(props.bake_groups):
            props.bake_groups.remove(self.index)
        return {'FINISHED'}


class ARANTOOLS_OT_BakeGroup_SetMembers(Operator):
    """Replace this group's members with the current selection."""
    bl_idname = "arantools.bakegroup_set_members"
    bl_label = "Set from Selection"
    bl_options = {'REGISTER', 'UNDO'}

    index: bpy.props.IntProperty()

    def execute(self, context):
        props = context.scene.arantools_normal_bake
        if not (0 <= self.index < len(props.bake_groups)):
            return {'CANCELLED'}
        group = props.bake_groups[self.index]
        group.members.clear()
        for m in _selected_mesh_objects(context):
            group.members.add().obj = m
        return {'FINISHED'}


class ARANTOOLS_OT_BakeGroup_AddMembers(Operator):
    """Append the current selection to this group (no duplicates)."""
    bl_idname = "arantools.bakegroup_add_members"
    bl_label = "Add from Selection"
    bl_options = {'REGISTER', 'UNDO'}

    index: bpy.props.IntProperty()

    def execute(self, context):
        props = context.scene.arantools_normal_bake
        if not (0 <= self.index < len(props.bake_groups)):
            return {'CANCELLED'}
        group = props.bake_groups[self.index]
        existing = {e.obj.name for e in group.members if e.obj is not None}
        added = 0
        for m in _selected_mesh_objects(context):
            if m.name in existing:
                continue
            group.members.add().obj = m
            added += 1
        self.report({'INFO'}, f"Added {added} mesh(es) to group.")
        return {'FINISHED'}


class ARANTOOLS_OT_BakeGroup_Clear(Operator):
    """Remove all members from this group."""
    bl_idname = "arantools.bakegroup_clear"
    bl_label = "Clear Members"
    bl_options = {'REGISTER', 'UNDO'}

    index: bpy.props.IntProperty()

    def execute(self, context):
        props = context.scene.arantools_normal_bake
        if 0 <= self.index < len(props.bake_groups):
            props.bake_groups[self.index].members.clear()
        return {'FINISHED'}


class ARANTOOLS_OT_BakeGroup_Select(Operator):
    """Select this group's members in the viewport (deselects everything else)."""
    bl_idname = "arantools.bakegroup_select"
    bl_label = "Select Members"
    bl_options = {'REGISTER', 'UNDO'}

    index: bpy.props.IntProperty()

    def execute(self, context):
        props = context.scene.arantools_normal_bake
        if not (0 <= self.index < len(props.bake_groups)):
            return {'CANCELLED'}
        group = props.bake_groups[self.index]
        for o in context.scene.objects:
            o.select_set(False)
        last = None
        for entry in group.members:
            if entry.obj is not None:
                entry.obj.select_set(True)
                last = entry.obj
        if last is not None:
            context.view_layer.objects.active = last
        return {'FINISHED'}


# ============================================================================
# Registration
# ============================================================================

classes = [
    ARANTOOLS_PG_BakeGroupMember,
    ARANTOOLS_PG_BakeGroup,
    ARANTOOLS_PG_BakedResult,
    ARANTOOLS_PG_NormalBake,
    ARANTOOLS_OT_NormalBake,
    ARANTOOLS_OT_OpenBakeFolder,
    ARANTOOLS_OT_OpenBakedTexture,
    ARANTOOLS_OT_MultiresFromHigh,
    ARANTOOLS_OT_BakeGroup_AddFromSelection,
    ARANTOOLS_OT_BakeGroup_Add,
    ARANTOOLS_OT_BakeGroup_Remove,
    ARANTOOLS_OT_BakeGroup_SetMembers,
    ARANTOOLS_OT_BakeGroup_AddMembers,
    ARANTOOLS_OT_BakeGroup_Clear,
    ARANTOOLS_OT_BakeGroup_Select,
]


def register():
    for cls in classes:
        bpy.utils.register_class(cls)
    bpy.types.Scene.arantools_normal_bake = bpy.props.PointerProperty(
        type=ARANTOOLS_PG_NormalBake
    )


def unregister():
    del bpy.types.Scene.arantools_normal_bake
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
