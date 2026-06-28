import bpy
import bmesh
import math
import mathutils
from collections import defaultdict
from bpy.types import Operator, PropertyGroup

class ARANTOOLS_PG_TrimMapping(PropertyGroup):
    trim_name: bpy.props.StringProperty(name="Trim")
    material: bpy.props.PointerProperty(
        name="Material",
        type=bpy.types.Material,
        description="Material corresponding to this trim"
    )

class ARANTOOLS_PG_ModifierItem(PropertyGroup):
    name: bpy.props.StringProperty()
    use: bpy.props.BoolProperty(default=True)

def update_modifier_source(self, context):
    self.modifier_items.clear()
    if self.modifier_source_object:
        for mod in self.modifier_source_object.modifiers:
            item = self.modifier_items.add()
            item.name = mod.name
            item.use = True

class ARANTOOLS_PG_PrimitiveBuildings(PropertyGroup):
    # UI Toggles
    ui_show_zenuv: bpy.props.BoolProperty(default=True)
    ui_show_uv: bpy.props.BoolProperty(default=True)
    ui_show_naming: bpy.props.BoolProperty(default=False)
    ui_show_modifiers: bpy.props.BoolProperty(default=False)
    ui_show_collection: bpy.props.BoolProperty(default=False)
    ui_show_generate: bpy.props.BoolProperty(default=True)
    ui_show_single_gen: bpy.props.BoolProperty(default=True)
    ui_show_batch_gen: bpy.props.BoolProperty(default=False)

    modifier_source_object: bpy.props.PointerProperty(
        name="Modifier Source",
        type=bpy.types.Object,
        description="Object to copy modifiers from after UVs are set",
        update=update_modifier_source
    )
    modifier_items: bpy.props.CollectionProperty(type=ARANTOOLS_PG_ModifierItem)
    
    target_material: bpy.props.PointerProperty(
        name="Target Material",
        type=bpy.types.Material,
        description="Material to assign to the final joined mesh"
    )
    grain_axis: bpy.props.EnumProperty(
        name="Wood Grain Axis",
        items=[
            ('U', 'Horizontal (U)', 'Align grain horizontally'),
            ('V', 'Vertical (V)', 'Align grain vertically'),
        ],
        default='V',
        description="Which UV axis represents the wood grain direction"
    )
    use_zenuv: bpy.props.BoolProperty(
        name="Use ZenUV Trims",
        default=True,
        description="If ZenUV is present, pack islands into trims matching their material name"
    )
    trim_mappings: bpy.props.CollectionProperty(type=ARANTOOLS_PG_TrimMapping)
    
    uv_shift: bpy.props.FloatProperty(
        name="UV Shift",
        default=0.0,
        description="Amount to shift UVs for each material (only used if no Zen UV trimsheet found)"
    )
    uv_normalize_scale: bpy.props.BoolProperty(
        name="Normalize UV Scale",
        default=True,
        description="Scale UVs based on real-world 3D size instead of aggressively stretching to fit the trim borders"
    )
    uv_scale_multiplier: bpy.props.FloatProperty(
        name="Texture Scale",
        default=1.0,
        min=0.001,
        description="Multiplier for the normalized UV scale"
    )
    uv_randomize: bpy.props.BoolProperty(
        name="Randomize Location",
        default=True,
        description="Randomly shift the UV island while respecting the trim sheet borders"
    )
    uv_randomize_amount: bpy.props.FloatProperty(
        name="Randomize Amount",
        default=0.1,
        min=0.0,
        description="Maximum distance in UV space to shift the UV island"
    )
    uv_border_margin: bpy.props.FloatProperty(
        name="Border Margin",
        default=0.01,
        min=0.0,
        description="Margin to leave around the trim borders (in UV space)"
    )
    uv_mark_seams: bpy.props.BoolProperty(
        name="Mark Island Seams",
        default=True,
        description="Automatically mark UV island boundaries as seams on the generated mesh"
    )
    batch_name_prefix: bpy.props.StringProperty(
        name="Batch Name Filter",
        default="House",
        description="Process children of objects whose name contains this text"
    )
    batch_target_z: bpy.props.FloatProperty(
        name="Batch Target Z",
        default=10.0,
        description="Global Z position for the new generated objects"
    )
    batch_name_add_prefix: bpy.props.StringProperty(
        name="Add Prefix",
        default="",
        description="Text to add at the beginning of the generated name"
    )
    batch_name_add_suffix: bpy.props.StringProperty(
        name="Add Suffix",
        default="_Primitive",
        description="Text to add at the end of the generated name"
    )
    batch_name_replace_old: bpy.props.StringProperty(
        name="Replace",
        default="",
        description="Text to remove or replace in the original name"
    )
    batch_name_replace_new: bpy.props.StringProperty(
        name="With",
        default="",
        description="Text to replace with (leave empty to just remove)"
    )
    target_collection: bpy.props.PointerProperty(
        type=bpy.types.Collection,
        name="Target Collection",
        description="Collection to place the generated objects in (leave empty to keep in current collection)"
    )

class ARANTOOLS_OT_UpdateTrimMapping(Operator):
    bl_idname = "arantools.update_trim_mapping"
    bl_label = "Refresh ZenUV Trims"
    bl_description = "Populate the list with trims from ZenUV to assign materials"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        props = context.scene.arantools_primitive_buildings
        props.trim_mappings.clear()
        
        if hasattr(context.scene, "zen_uv") and hasattr(context.scene.zen_uv, "trimsheet"):
            for ts in context.scene.zen_uv.trimsheet:
                item = props.trim_mappings.add()
                item.trim_name = getattr(ts, 'name', 'Unnamed')
        return {'FINISHED'}

def _process_primitive_building(context, obj_list, pivot_matrix, props):
    import bmesh
    import math
    from mathutils import Vector
    import random
    from collections import defaultdict
    import bpy

    bpy.ops.object.select_all(action='DESELECT')
    for obj in obj_list:
        obj.select_set(True)
    context.view_layer.objects.active = obj_list[0]
    
    bpy.ops.object.duplicate()
    obj_list = context.selected_objects
    context.view_layer.objects.active = obj_list[0]
    
    bpy.ops.object.make_single_user(type='SELECTED_OBJECTS', object=True, obdata=True)
    bpy.ops.object.convert(target='MESH')
    
    if len(obj_list) > 1:
        bpy.ops.object.join()
        
    obj = context.view_layer.objects.active
    
    # Apply transform so scale is 1,1,1
    bpy.ops.object.transform_apply(location=False, rotation=True, scale=True)
    
    # Setup origin at pivot_matrix
    cursor_loc_save = context.scene.cursor.location.copy()
    context.scene.cursor.location = pivot_matrix.translation
    bpy.ops.object.origin_set(type='ORIGIN_CURSOR', center='MEDIAN')
    context.scene.cursor.location = cursor_loc_save

    mesh = obj.data

    # Step 0: Group polygons by material index
    poly_by_mat = defaultdict(list)
    for p in mesh.polygons:
        poly_by_mat[p.material_index].append(p.index)

    # Step 1: Smart UV project per material
    for mat_idx, poly_indices in poly_by_mat.items():
        if not poly_indices:
            continue
            
        bpy.ops.object.mode_set(mode='OBJECT')
        for p in mesh.polygons:
            p.select = False
        for i in poly_indices:
            mesh.polygons[i].select = True
            
        bpy.ops.object.mode_set(mode='EDIT')
        bpy.ops.uv.smart_project(angle_limit=1.15192, margin_method='SCALED', island_margin=0.01)
        bpy.ops.object.mode_set(mode='OBJECT')

    # Start BMesh for manual transformations
    bm = bmesh.new()
    bm.from_mesh(mesh)
    uv_layer = bm.loops.layers.uv.verify()

    # Step 0 (part 2): Mark seams for material boundaries AND UV boundaries
    if props.uv_mark_seams:
        for edge in bm.edges:
            if len(edge.link_faces) == 2:
                if edge.link_faces[0].material_index != edge.link_faces[1].material_index:
                    edge.seam = True
                    continue
            
            if len(edge.link_faces) != 2:
                edge.seam = True
                continue
                
            l1, l2 = edge.link_loops
            uv1_a = uv1_b = uv2_a = uv2_b = None
            for loop in l1.face.loops:
                if loop.vert == edge.verts[0]: uv1_a = loop[uv_layer].uv
                if loop.vert == edge.verts[1]: uv1_b = loop[uv_layer].uv
            for loop in l2.face.loops:
                if loop.vert == edge.verts[0]: uv2_a = loop[uv_layer].uv
                if loop.vert == edge.verts[1]: uv2_b = loop[uv_layer].uv
                
            if uv1_a and uv1_b and uv2_a and uv2_b:
                if (uv1_a - uv2_a).length > 1e-4 or (uv1_b - uv2_b).length > 1e-4:
                    edge.seam = True

    # Build ZenUV material mappings
    mat_to_trim = {}
    if props.use_zenuv:
        for mapping in props.trim_mappings:
            if mapping.material:
                mat_to_trim[mapping.material.name] = mapping.trim_name

    # Process each material group
    for mat_idx, poly_indices in poly_by_mat.items():
        if not poly_indices: continue
        
        faces = [f for f in bm.faces if f.index in poly_indices]
        if not faces: continue
        
        # Group into UV islands
        parent = {f: f for f in faces}
        def find(f):
            if parent[f] == f: return f
            parent[f] = find(parent[f])
            return parent[f]
        def union(f1, f2):
            root1 = find(f1)
            root2 = find(f2)
            if root1 != root2:
                parent[root1] = root2
                
        uv_dict = defaultdict(list)
        for f in faces:
            for l in f.loops:
                uv = l[uv_layer].uv
                key = (l.vert.index, round(uv.x, 4), round(uv.y, 4))
                uv_dict[key].append(f)
                
        for fs in uv_dict.values():
            for i in range(1, len(fs)):
                union(fs[0], fs[i])
                
        island_dict = defaultdict(list)
        for f in faces:
            island_dict[find(f)].append(f)
            
        islands = list(island_dict.values())
        if not islands: continue

        # Figure out target trim for this material
        mat = None
        if mat_idx < len(obj.material_slots):
            mat = obj.material_slots[mat_idx].material
        elif mat_idx < len(mesh.materials):
            mat = mesh.materials[mat_idx]
            
        mat_name = mat.name if mat else ""
        
        target_trim_name = None
        if props.use_zenuv and hasattr(context.scene, "zen_uv") and hasattr(context.scene.zen_uv, "trimsheet"):
            if mat_name in mat_to_trim:
                target_trim_name = mat_to_trim[mat_name]
            else:
                mat_name_stripped = mat_name.rsplit(".", 1)[0] if ("." in mat_name and mat_name.rsplit(".", 1)[-1].isdigit()) else mat_name
                for mapped_mat_name, t_name in mat_to_trim.items():
                    mapped_stripped = mapped_mat_name.rsplit(".", 1)[0] if ("." in mapped_mat_name and mapped_mat_name.rsplit(".", 1)[-1].isdigit()) else mapped_mat_name
                    if mat_name_stripped == mapped_stripped:
                        target_trim_name = t_name
                        break
                        
        trim_rect = None
        if target_trim_name:
            for ts in context.scene.zen_uv.trimsheet:
                if getattr(ts, 'name', '') == target_trim_name:
                    rect = getattr(ts, 'rect', [])
                    if len(rect) == 4:
                        xs = sorted([float(rect[0]), float(rect[2])])
                        ys = sorted([float(rect[1]), float(rect[3])])
                        trim_rect = (xs[0], ys[0], xs[1], ys[1])
                    break
                    
        if trim_rect:
            min_x, min_y, max_x, max_y = trim_rect
            # Correct ZenUV flipped rects if needed
            if max_x - min_x < 1e-6 or max_y - min_y < 1e-6:
                W = max(1.0, max_x - min_x)
                H = max(1.0, max_y - min_y)
            else:
                W = max_x - min_x
                H = max_y - min_y
                
            margin = props.uv_border_margin
            avail_W = max(1e-6, W - 2*margin)
            avail_H = max(1e-6, H - 2*margin)
            
            trim_center_x = min_x + W / 2
            trim_center_y = min_y + H / 2
            
            island_data = []
            max_w = 0.0
            max_h = 0.0
            
            for island in islands:
                # Step 2: Normalize Scales
                sum_3d = 0.0
                sum_uv = 0.0
                for f in island:
                    for i in range(len(f.loops)):
                        l1 = f.loops[i]
                        l2 = f.loops[(i + 1) % len(f.loops)]
                        d3d = (l1.vert.co - l2.vert.co).length
                        duv = (l1[uv_layer].uv - l2[uv_layer].uv).length
                        sum_3d += d3d
                        sum_uv += duv
                        
                scale_norm = sum_3d / sum_uv if sum_uv > 1e-6 else 1.0
                
                # Also collect vertices for centroid
                uvs = [l[uv_layer].uv for f in island for l in f.loops]
                cx = sum(u.x for u in uvs) / len(uvs)
                cy = sum(u.y for u in uvs) / len(uvs)
                
                for f in island:
                    for l in f.loops:
                        l[uv_layer].uv.x = (l[uv_layer].uv.x - cx) * scale_norm + cx
                        l[uv_layer].uv.y = (l[uv_layer].uv.y - cy) * scale_norm + cy
                        
                # Step 3: Rotate to Grain
                # Measure width and height of the island in UV space to find its actual dominant axis
                uvs = [l[uv_layer].uv for f in island for l in f.loops]
                i_min_x = min(u.x for u in uvs)
                i_max_x = max(u.x for u in uvs)
                i_min_y = min(u.y for u in uvs)
                i_max_y = max(u.y for u in uvs)
                w_i = i_max_x - i_min_x
                h_i = i_max_y - i_min_y
                
                # If the island is taller than it is wide, its dominant axis is V (Vertical)
                # If the island is wider than it is tall, its dominant axis is U (Horizontal)
                is_vertical = h_i > w_i
                wants_vertical = (props.grain_axis == 'V')
                
                # If it doesn't match the requested grain axis, rotate by 90 degrees
                if is_vertical != wants_vertical:
                    cos_r = 0.0
                    sin_r = 1.0 # 90 degree rotation
                    
                    cx = sum(u.x for u in uvs) / len(uvs)
                    cy = sum(u.y for u in uvs) / len(uvs)
                    
                    for f in island:
                        for l in f.loops:
                            u_x = l[uv_layer].uv.x - cx
                            u_y = l[uv_layer].uv.y - cy
                            l[uv_layer].uv.x = u_x * cos_r - u_y * sin_r + cx
                            l[uv_layer].uv.y = u_x * sin_r + u_y * cos_r + cy

                # Measure width and height after rotation/scaling
                uvs = [l[uv_layer].uv for f in island for l in f.loops]
                i_min_x = min(u.x for u in uvs)
                i_max_x = max(u.x for u in uvs)
                i_min_y = min(u.y for u in uvs)
                i_max_y = max(u.y for u in uvs)
                w_i = i_max_x - i_min_x
                h_i = i_max_y - i_min_y
                
                # Using geometric centroid
                c_x = (i_min_x + i_max_x) / 2
                c_y = (i_min_y + i_max_y) / 2
                
                max_w = max(max_w, w_i)
                max_h = max(max_h, h_i)
                island_data.append((island, w_i, h_i, c_x, c_y))
                
            # Step 5: Scale all islands in its own trim so that the largest is within trim margin
            global_clamp = 1.0
            if max_w > avail_W: global_clamp = min(global_clamp, avail_W / max_w)
            if max_h > avail_H: global_clamp = min(global_clamp, avail_H / max_h)
            
            for island, w_i, h_i, cx, cy in island_data:
                # We move the centroid to 0.0, and scale by global_clamp
                for f in island:
                    for l in f.loops:
                        l[uv_layer].uv.x = (l[uv_layer].uv.x - cx) * global_clamp
                        l[uv_layer].uv.y = (l[uv_layer].uv.y - cy) * global_clamp
                        
                # Step 6: Evaluate texture scale (multiply)
                tex_scale = props.uv_normalize_scale if props.uv_normalize_scale > 0.0 else 1.0
                for f in island:
                    for l in f.loops:
                        l[uv_layer].uv.x *= tex_scale
                        l[uv_layer].uv.y *= tex_scale
                        
                # Step 7: Randomize location & Step 4: Move centroid to trim center
                R = props.uv_randomize_amount if props.uv_randomize else 0.0
                rx = random.uniform(-R, R)
                ry = random.uniform(-R, R)
                
                for f in island:
                    for l in f.loops:
                        l[uv_layer].uv.x += trim_center_x + rx
                        l[uv_layer].uv.y += trim_center_y + ry
                        
                # Step 8: Final double check & constraint
                uvs = [l[uv_layer].uv for f in island for l in f.loops]
                final_min_x = min(u.x for u in uvs)
                final_max_x = max(u.x for u in uvs)
                final_min_y = min(u.y for u in uvs)
                final_max_y = max(u.y for u in uvs)
                
                b_min_x = min_x + margin
                b_max_x = max_x - margin
                b_min_y = min_y + margin
                b_max_y = max_y - margin
                
                shift_x = 0.0
                shift_y = 0.0
                
                # Minimal shift towards center
                if final_min_x < b_min_x: shift_x = b_min_x - final_min_x
                if final_max_x + shift_x > b_max_x: shift_x = b_max_x - final_max_x
                
                if final_min_y < b_min_y: shift_y = b_min_y - final_min_y
                if final_max_y + shift_y > b_max_y: shift_y = b_max_y - final_max_y
                
                if shift_x != 0.0 or shift_y != 0.0:
                    for f in island:
                        for l in f.loops:
                            l[uv_layer].uv.x += shift_x
                            l[uv_layer].uv.y += shift_y
                            
                final_min_x += shift_x
                final_max_x += shift_x
                final_min_y += shift_y
                final_max_y += shift_y
                
                # Minimal scale down if still exceeding (because it's too big)
                scale_fix = 1.0
                if final_max_x - final_min_x > avail_W:
                    scale_fix = min(scale_fix, avail_W / (final_max_x - final_min_x))
                if final_max_y - final_min_y > avail_H:
                    scale_fix = min(scale_fix, avail_H / (final_max_y - final_min_y))
                    
                if scale_fix < 1.0:
                    cx_f = (final_min_x + final_max_x) / 2
                    cy_f = (final_min_y + final_max_y) / 2
                    for f in island:
                        for l in f.loops:
                            l[uv_layer].uv.x = (l[uv_layer].uv.x - cx_f) * scale_fix + cx_f
                            l[uv_layer].uv.y = (l[uv_layer].uv.y - cy_f) * scale_fix + cy_f

        else:
            # Fallback if no trim is found
            shift_x = mat_idx * props.uv_shift
            for island in islands:
                for f in island:
                    for l in f.loops:
                        l[uv_layer].uv.x += shift_x

    bm.to_mesh(mesh)
    bm.free()

    # Ensure all materials are linked to DATA and cleared, then assign target material
    for slot in obj.material_slots:
        slot.link = 'DATA'
    mesh.materials.clear()
    if props.target_material:
        mesh.materials.append(props.target_material)
        
    return obj

def _copy_modifiers(context, props, target_obj):
    if props.modifier_source_object:
        mods_to_copy = [item.name for item in props.modifier_items if item.use]
        if mods_to_copy:
            target_obj.modifiers.clear()
            bpy.ops.object.select_all(action='DESELECT')
            target_obj.select_set(True)
            props.modifier_source_object.select_set(True)
            context.view_layer.objects.active = props.modifier_source_object
            for mod_name in mods_to_copy:
                try:
                    bpy.ops.object.modifier_copy_to_selected(modifier=mod_name)
                except Exception:
                    pass
            bpy.ops.object.select_all(action='DESELECT')
            target_obj.select_set(True)
            context.view_layer.objects.active = target_obj

def _move_to_collection(obj, target_collection):
    if not target_collection: return
    if obj.name not in target_collection.objects:
        target_collection.objects.link(obj)
    for col in obj.users_collection:
        if col != target_collection:
            col.objects.unlink(obj)

def _run_batch_logic(context, props, base_objects, operator):
    if not base_objects:
        operator.report({'WARNING'}, "No base objects found")
        return {'CANCELLED'}
        
    generated_count = 0
    def get_all_children(obj):
        children = []
        for child in obj.children:
            children.append(child)
            children.extend(get_all_children(child))
        return children
        
    for base_obj in base_objects:
        all_children = get_all_children(base_obj)
        valid_children = [c for c in all_children if c.type in {'MESH', 'CURVE', 'SURFACE', 'META', 'FONT'}]
        
        if not valid_children:
            continue
            
        pivot_matrix = base_obj.matrix_world.copy()
        
        # Look for an existing generated object to UPDATE
        existing_obj = None
        for o in context.scene.objects:
            if o.get("arantools_batch_source") == base_obj.name and o.type == 'MESH':
                existing_obj = o
                break
        
        new_obj = _process_primitive_building(context, valid_children, pivot_matrix, props)
        if new_obj:
            new_obj["arantools_batch_source"] = base_obj.name
            
            # Unparent but keep world transform
            world_mat = new_obj.matrix_world.copy()
            new_obj.parent = None
            new_obj.matrix_world = world_mat
            
            if existing_obj:
                # UPDATE mesh instead of replacing object
                old_mesh = existing_obj.data
                existing_obj.data = new_obj.data
                # Remove the temp generated object
                bpy.data.objects.remove(new_obj)
                new_obj = existing_obj
                # Also clean up the old mesh to prevent memory leaks
                if old_mesh.users == 0:
                    bpy.data.meshes.remove(old_mesh)
            else:
                base_name = base_obj.name
                if props.batch_name_replace_old:
                    base_name = base_name.replace(props.batch_name_replace_old, props.batch_name_replace_new)
                new_obj.name = f"{props.batch_name_add_prefix}{base_name}{props.batch_name_add_suffix}"
                
            _copy_modifiers(context, props, new_obj)
            _move_to_collection(new_obj, props.target_collection)
            new_obj.location.z = props.batch_target_z
            generated_count += 1
            
    operator.report({'INFO'}, f"Generated {generated_count} primitive buildings")
    return {'FINISHED'}

class ARANTOOLS_OT_PrimitiveBuildings(Operator):
    bl_idname = "arantools.primitive_buildings_tool"
    bl_label = "Generate from Selection"
    bl_description = "Duplicate, join, Smart UV project, rotate islands to bounding box, and separate UVs by original material"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        props = context.scene.arantools_primitive_buildings
        selected_objs = [o for o in context.selected_objects if o.type in {'MESH', 'CURVE', 'SURFACE', 'META', 'FONT'}]
        
        if not selected_objs:
            self.report({'WARNING'}, "No suitable objects selected")
            return {'CANCELLED'}
            
        # Get active object for pivot matrix
        active_obj = context.active_object
        if not active_obj: active_obj = selected_objs[0]
        pivot_matrix = active_obj.matrix_world.copy()
        
        new_obj = _process_primitive_building(context, selected_objs, pivot_matrix, props)
        
        if new_obj:
            _copy_modifiers(context, props, new_obj)
            _move_to_collection(new_obj, props.target_collection)
            bpy.ops.object.select_all(action='DESELECT')
            new_obj.select_set(True)
            context.view_layer.objects.active = new_obj
            self.report({'INFO'}, "Primitive Building Generated!")
            return {'FINISHED'}
        else:
            self.report({'WARNING'}, "Failed to generate")
            return {'CANCELLED'}

class ARANTOOLS_OT_PrimitiveBuildingsChildren(Operator):
    bl_idname = "arantools.primitive_buildings_children"
    bl_label = "Generate from Children"
    bl_description = "For each selected object, generate a primitive building from its children"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        props = context.scene.arantools_primitive_buildings
        base_objects = context.selected_objects
        return _run_batch_logic(context, props, base_objects, self)

class ARANTOOLS_OT_BatchPrimitiveBuildings(Operator):
    bl_idname = "arantools.batch_primitive_buildings"
    bl_label = "Batch Primitive Building"
    bl_description = "Run Primitive Building on children of objects matching the prefix"
    bl_options = {'REGISTER', 'UNDO'}
    
    def execute(self, context):
        props = context.scene.arantools_primitive_buildings
        prefix = props.batch_name_prefix
        base_objects = [o for o in context.scene.objects if prefix in o.name]
        return _run_batch_logic(context, props, base_objects, self)

class ARANTOOLS_OT_RefreshPrimitiveBuilding(Operator):
    bl_idname = "arantools.refresh_primitive_building"
    bl_label = "Refresh Generation"
    bl_description = "Regenerate this primitive building from its source object"
    bl_options = {'REGISTER', 'UNDO'}
    
    source_name: bpy.props.StringProperty()
    
    def execute(self, context):
        props = context.scene.arantools_primitive_buildings
        source_obj = context.scene.objects.get(self.source_name)
        
        if not source_obj:
            self.report({'WARNING'}, f"Source object '{self.source_name}' not found!")
            return {'CANCELLED'}
            
        return _run_batch_logic(context, props, [source_obj], self)


class ARANTOOLS_OT_PrimitiveBuildingsHelp(Operator):
    bl_idname = "arantools.primitive_buildings_help"
    bl_label = "How to Use Primitive Buildings"
    bl_description = "Show instructions for 3D Artists"
    
    def invoke(self, context, event):
        return context.window_manager.invoke_props_dialog(self, width=450)
        
    def draw(self, context):
        layout = self.layout
        layout.label(text="Workflow for 3D Artists:", icon='INFO')
        layout.label(text="1. Model your primitive pieces using a base object with children.")
        layout.label(text="2. Assign materials to the faces (ZenUV will match these).")
        layout.label(text="3. Select the base object and hit 'Generate from Children'.")
        layout.label(text="   (Or use Batch Generate for multiple bases).")
        layout.separator()
        layout.label(text="What this tool does automatically:", icon='MOD_BUILD')
        layout.label(text="- Duplicates and joins all pieces into a single mesh.")
        layout.label(text="- Automatically Smart UV Projects everything.")
        layout.label(text="- Rotates the UV islands to align the wood grain.")
        layout.label(text="- Mathematically perfectly fits them into your ZenUV trims.")
        layout.label(text="- If you edit the source objects, just click Refresh Generation!")
        
    def execute(self, context):
        return {'FINISHED'}

classes = [
    ARANTOOLS_PG_ModifierItem,
    ARANTOOLS_PG_TrimMapping,
    ARANTOOLS_PG_PrimitiveBuildings,
    ARANTOOLS_OT_UpdateTrimMapping,
    ARANTOOLS_OT_PrimitiveBuildings,
    ARANTOOLS_OT_PrimitiveBuildingsChildren,
    ARANTOOLS_OT_BatchPrimitiveBuildings,
    ARANTOOLS_OT_RefreshPrimitiveBuilding,
    ARANTOOLS_OT_PrimitiveBuildingsHelp,
]

def register():
    for cls in classes:
        bpy.utils.register_class(cls)
    bpy.types.Scene.arantools_primitive_buildings = bpy.props.PointerProperty(type=ARANTOOLS_PG_PrimitiveBuildings)

def unregister():
    del bpy.types.Scene.arantools_primitive_buildings
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
