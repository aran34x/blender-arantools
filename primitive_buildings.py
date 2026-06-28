import bpy
import bmesh
import math
import mathutils
from collections import defaultdict
from bpy.types import Operator, PropertyGroup

class ARANTOOLS_PG_PrimitiveBuildings(PropertyGroup):
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
    uv_shift: bpy.props.FloatProperty(
        name="UV Shift per Material",
        default=1.0,
        description="Distance to shift UVs (in U) for each material index"
    )

class ARANTOOLS_OT_PrimitiveBuildings(Operator):
    bl_idname = "arantools.primitive_buildings_tool"
    bl_label = "Generate Primitive Building"
    bl_description = "Duplicate, join, Smart UV project, rotate islands to bounding box, and separate UVs by original material"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        props = context.scene.arantools_primitive_buildings
        selected_objs = [o for o in context.selected_objects if o.type in {'MESH', 'CURVE', 'SURFACE', 'META', 'FONT'}]
        
        if not selected_objs:
            self.report({'WARNING'}, "No suitable objects selected")
            return {'CANCELLED'}
            
        # 1. Duplicate
        bpy.ops.object.mode_set(mode='OBJECT')
        bpy.ops.object.duplicate_move()
        dup_objs = context.selected_objects
        
        # 2. Convert to mesh
        bpy.ops.object.convert(target='MESH')
        
        # 3. Join
        context.view_layer.objects.active = dup_objs[0]
        bpy.ops.object.join()
        
        obj = context.active_object
        
        # 4. Smart UV Project
        bpy.ops.object.mode_set(mode='EDIT')
        bpy.ops.mesh.select_all(action='SELECT')
        bpy.ops.uv.smart_project()
        
        # 5. Process UVs (Rotate & Shift)
        bpy.ops.object.mode_set(mode='OBJECT')
        
        mesh = obj.data
        bm = bmesh.new()
        bm.from_mesh(mesh)
        uv_layer = bm.loops.layers.uv.verify()
        
        # Find islands based on UV sharing
        parent = {}
        def find(i):
            if parent[i] == i: return i
            parent[i] = find(parent[i])
            return parent[i]
        def union(i, j):
            root_i = find(i)
            root_j = find(j)
            if root_i != root_j:
                parent[root_i] = root_j
                
        for f in bm.faces:
            parent[f] = f
            
        uv_dict = defaultdict(list)
        for f in bm.faces:
            for l in f.loops:
                uv = l[uv_layer].uv
                key = (l.vert.index, round(uv.x, 4), round(uv.y, 4))
                uv_dict[key].append(f)
                
        for faces in uv_dict.values():
            first = faces[0]
            for other in faces[1:]:
                union(first, other)
                
        islands = defaultdict(list)
        for f in bm.faces:
            islands[find(f)].append(f)
            
        islands = list(islands.values())
        
        # Rotate and Shift
        for island in islands:
            # Bounding box of the island in 3D
            verts = set(l.vert for f in island for l in f.loops)
            xs = [v.co.x for v in verts]
            ys = [v.co.y for v in verts]
            zs = [v.co.z for v in verts]
            dx = max(xs) - min(xs) if xs else 0
            dy = max(ys) - min(ys) if ys else 0
            dz = max(zs) - min(zs) if zs else 0
            
            longest_axis_idx = max(range(3), key=lambda i: (dx, dy, dz)[i])
            
            best_edge_uv = None
            max_dot = -1
            
            for f in island:
                for l in f.loops:
                    v1 = l.vert.co
                    v2 = l.link_loop_next.vert.co
                    edge_3d = (v2 - v1)
                    length = edge_3d.length
                    if length > 1e-6:
                        edge_3d = edge_3d / length
                        dot = abs(edge_3d[longest_axis_idx])
                        if dot > max_dot:
                            uv1 = l[uv_layer].uv
                            uv2 = l.link_loop_next[uv_layer].uv
                            uv_edge = (uv2 - uv1)
                            if uv_edge.length > 1e-6:
                                max_dot = dot
                                best_edge_uv = uv_edge.normalized()
                                
            if best_edge_uv:
                alpha = math.atan2(best_edge_uv.y, best_edge_uv.x)
                if props.grain_axis == 'V':
                    rot = math.pi / 2 - alpha
                else:
                    rot = 0.0 - alpha
                    
                uvs = [l[uv_layer].uv for f in island for l in f.loops]
                if uvs:
                    center_x = sum(u.x for u in uvs) / len(uvs)
                    center_y = sum(u.y for u in uvs) / len(uvs)
                    center = mathutils.Vector((center_x, center_y))
                    
                    cos_r = math.cos(rot)
                    sin_r = math.sin(rot)
                    
                    for f in island:
                        for l in f.loops:
                            u = l[uv_layer].uv - center
                            nx = u.x * cos_r - u.y * sin_r
                            ny = u.x * sin_r + u.y * cos_r
                            l[uv_layer].uv = mathutils.Vector((nx, ny)) + center
                            
        # Shift UVs based on material index
        for f in bm.faces:
            mat_idx = f.material_index
            shift_x = mat_idx * props.uv_shift
            for l in f.loops:
                l[uv_layer].uv.x += shift_x
                
        bm.to_mesh(mesh)
        bm.free()
        
        # 6. Assign single material
        mesh.materials.clear()
        if props.target_material:
            mesh.materials.append(props.target_material)
            
        self.report({'INFO'}, "Primitive Building Generated!")
        return {'FINISHED'}

classes = [
    ARANTOOLS_PG_PrimitiveBuildings,
    ARANTOOLS_OT_PrimitiveBuildings,
]

def register():
    for cls in classes:
        bpy.utils.register_class(cls)
    bpy.types.Scene.arantools_primitive_buildings = bpy.props.PointerProperty(type=ARANTOOLS_PG_PrimitiveBuildings)

def unregister():
    del bpy.types.Scene.arantools_primitive_buildings
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
