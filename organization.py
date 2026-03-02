import bpy
import re
from bpy.types import Operator


# ============================================================================
# Batch Rig Transfer
# ============================================================================

class ARANTOOLS_PG_RT_BindingItem(bpy.types.PropertyGroup):
    source_obj: bpy.props.PointerProperty(type=bpy.types.Object, name="Source Mesh")
    gt_obj: bpy.props.PointerProperty(type=bpy.types.Object, name="Ground Truth")


class ARANTOOLS_PG_RT_Props(bpy.types.PropertyGroup):
    source_collection: bpy.props.PointerProperty(
        type=bpy.types.Collection,
        name="Source Collection",
        description="Collection with the raw, unrigged meshes"
    )
    gt_collection: bpy.props.PointerProperty(
        type=bpy.types.Collection,
        name="Ground Truth Collection",
        description="Collection with the correctly rigged reference meshes"
    )
    target_collection: bpy.props.PointerProperty(
        type=bpy.types.Collection,
        name="Target Collection",
        description="Where the final rigged meshes will be placed"
    )
    apply_modifiers: bpy.props.BoolProperty(
        name="Apply Modifiers",
        description="Apply existing modifiers (Mirror, Subsurf) before rigging",
        default=True
    )
    transfer_method: bpy.props.EnumProperty(
        name="Transfer Method",
        items=[
            ('NEAREST', "Nearest Vertex", "Fastest, 1-to-1 vertex matching"),
            ('POLYINTERP_NEAREST', "Nearest Face Interpolated", "Best for general rigging"),
            ('POLYINTERP_VNORPROJ', "Projected Face Interpolated", "Good for clothing layers"),
            ('TOPOLOGY', "Topology", "Requires identical vertex count/order"),
        ],
        default='POLYINTERP_NEAREST'
    )
    binding_list: bpy.props.CollectionProperty(type=ARANTOOLS_PG_RT_BindingItem)
    binding_index: bpy.props.IntProperty()


class ARANTOOLS_UL_RT_BindingList(bpy.types.UIList):
    def draw_item(self, context, layout, data, item, icon, active_data, active_propname):
        split = layout.split(factor=0.45)
        if item.source_obj:
            split.label(text=item.source_obj.name, icon='MESH_DATA')
        else:
            split.label(text="Missing", icon='ERROR')
        split.prop(item, "gt_obj", text="")


class ARANTOOLS_OT_RT_PopulateList(Operator):
    """Load source meshes and auto-match them to ground truth using Reverse Token Priority"""
    bl_idname = "arantools.rt_populate_list"
    bl_label = "Load & Smart Match"

    def execute(self, context):
        props = context.scene.arantools_rt_props
        source_col = props.source_collection
        gt_col = props.gt_collection

        if not source_col:
            self.report({'ERROR'}, "Select a Source Collection first.")
            return {'CANCELLED'}

        props.binding_list.clear()
        gt_objects = [o for o in gt_col.all_objects if o.type == 'MESH'] if gt_col else []

        def get_clean_tokens(name):
            clean = name.lower()
            for ignore in ["_rgt", "_rig", "_rigged", "_baked", "_lod0", "_lod1", "_low", "_high", ".001", ".002"]:
                clean = clean.replace(ignore, "")
            return [t for t in re.split(r'[^a-z0-9]', clean) if t]

        count = matches_found = 0

        for obj in source_col.all_objects:
            if obj.type != 'MESH':
                continue
            item = props.binding_list.add()
            item.source_obj = obj
            count += 1

            if not gt_objects:
                continue

            src_tokens = get_clean_tokens(obj.name)
            found_match = None

            for token in reversed(src_tokens):
                possible = [gt for gt in gt_objects if token in get_clean_tokens(gt.name)]
                if len(possible) == 1:
                    found_match = possible[0]
                    break
                elif len(possible) > 1:
                    src_set = set(src_tokens)
                    found_match = max(possible, key=lambda gt: len(set(get_clean_tokens(gt.name)) & src_set))
                    break

            if not found_match:
                src_set = set(src_tokens)
                best_score = -1
                for gt in gt_objects:
                    score = len(src_set & set(get_clean_tokens(gt.name)))
                    if score > best_score and score > 0:
                        best_score = score
                        found_match = gt

            if found_match:
                item.gt_obj = found_match
                matches_found += 1

        self.report({'INFO'}, f"Loaded {count} mesh(es), matched {matches_found}.")
        return {'FINISHED'}


class ARANTOOLS_OT_RT_ExecuteRigging(Operator):
    """Duplicate → Apply Modifiers → Data Transfer Weights → Bind Armature"""
    bl_idname = "arantools.rt_execute_rigging"
    bl_label = "Batch Rig & Transfer"

    def execute(self, context):
        props = context.scene.arantools_rt_props
        target_col = props.target_collection

        if not target_col:
            self.report({'ERROR'}, "Select a Target Collection.")
            return {'CANCELLED'}

        def find_armature(obj):
            if obj.parent and obj.parent.type == 'ARMATURE':
                return obj.parent
            for mod in obj.modifiers:
                if mod.type == 'ARMATURE' and mod.object:
                    return mod.object
            return None

        bpy.ops.object.select_all(action='DESELECT')
        processed = 0

        for item in props.binding_list:
            src, gt = item.source_obj, item.gt_obj
            if not src or not gt:
                continue

            new_obj = src.copy()
            new_obj.name = f"{src.name}_Rigged"
            new_obj.data = new_obj.data.copy()
            target_col.objects.link(new_obj)

            context.view_layer.objects.active = new_obj
            new_obj.select_set(True)

            if props.apply_modifiers:
                try:
                    bpy.ops.object.convert(target='MESH')
                except RuntimeError:
                    pass

            dt_mod = new_obj.modifiers.new(name="DataTransfer", type='DATA_TRANSFER')
            dt_mod.object = gt
            dt_mod.use_vert_data = True
            dt_mod.data_types_verts = {'VGROUP_WEIGHTS'}
            dt_mod.vert_mapping = props.transfer_method
            context.view_layer.update()

            try:
                with context.temp_override(object=new_obj):
                    bpy.ops.object.datalayout_transfer(modifier=dt_mod.name)
                with context.temp_override(object=new_obj):
                    bpy.ops.object.modifier_apply(modifier=dt_mod.name)
            except Exception as e:
                print(f"Transfer failed on {new_obj.name}: {e}")

            armature = find_armature(gt)
            if armature:
                new_obj.parent = armature
                mod_arm = new_obj.modifiers.new(name="Armature", type='ARMATURE')
                mod_arm.object = armature

            new_obj.select_set(False)
            processed += 1

        self.report({'INFO'}, f"Processed {processed} mesh(es).")
        return {'FINISHED'}


# ============================================================================
# Collection Baker
# ============================================================================

class ARANTOOLS_PG_BakeObject(bpy.types.PropertyGroup):
    target_name: bpy.props.StringProperty(
        name="Target Name",
        description="Meshes with the same Target Name will be joined into one.",
        default=""
    )


class ARANTOOLS_PG_BakeScene(bpy.types.PropertyGroup):
    source_collection: bpy.props.PointerProperty(
        name="Source Collection",
        type=bpy.types.Collection
    )
    target_collection: bpy.props.PointerProperty(
        name="Target Collection",
        type=bpy.types.Collection
    )


class ARANTOOLS_OT_CollectionBake(Operator):
    """Duplicate all source meshes, apply modifiers, then join by Target Name"""
    bl_idname = "arantools.collection_bake"
    bl_label = "Bake & Join"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        props = context.scene.arantools_bake_scene
        source_col = props.source_collection
        target_col = props.target_collection

        if not source_col or not target_col:
            self.report({'ERROR'}, "Select both Source and Target collections.")
            return {'CANCELLED'}
        if source_col == target_col:
            self.report({'ERROR'}, "Source and Target cannot be the same collection.")
            return {'CANCELLED'}

        source_col.hide_viewport = False
        target_col.hide_viewport = False

        for obj in list(target_col.objects):
            bpy.data.objects.remove(obj, do_unlink=True)

        objects_by_name = {}
        bpy.ops.object.select_all(action='DESELECT')

        for obj in source_col.objects:
            if obj.type != 'MESH':
                continue
            obj.select_set(True)
            context.view_layer.objects.active = obj
            bpy.ops.object.duplicate(linked=False)
            new_obj = context.active_object

            for col in new_obj.users_collection:
                col.objects.unlink(new_obj)
            target_col.objects.link(new_obj)
            bpy.ops.object.convert(target='MESH')

            raw_name = obj.arantools_bake_props.target_name.strip()
            key_name = raw_name if raw_name else obj.name
            objects_by_name.setdefault(key_name, []).append(new_obj)

            bpy.ops.object.select_all(action='DESELECT')

        for key_name, obj_list in objects_by_name.items():
            if not obj_list:
                continue
            for o in obj_list:
                o.select_set(True)
            context.view_layer.objects.active = obj_list[0]
            if len(obj_list) > 1:
                bpy.ops.object.join()
            context.active_object.name = f"{key_name}_Baked"
            bpy.ops.object.select_all(action='DESELECT')

        source_col.hide_viewport = True
        self.report({'INFO'}, "Bake complete.")
        return {'FINISHED'}


# ============================================================================
# Registration
# ============================================================================

classes = [
    ARANTOOLS_PG_RT_BindingItem,
    ARANTOOLS_PG_RT_Props,
    ARANTOOLS_UL_RT_BindingList,
    ARANTOOLS_OT_RT_PopulateList,
    ARANTOOLS_OT_RT_ExecuteRigging,
    ARANTOOLS_PG_BakeObject,
    ARANTOOLS_PG_BakeScene,
    ARANTOOLS_OT_CollectionBake,
]


def register():
    for cls in classes:
        bpy.utils.register_class(cls)
    bpy.types.Scene.arantools_rt_props = bpy.props.PointerProperty(type=ARANTOOLS_PG_RT_Props)
    bpy.types.Scene.arantools_bake_scene = bpy.props.PointerProperty(type=ARANTOOLS_PG_BakeScene)
    bpy.types.Object.arantools_bake_props = bpy.props.PointerProperty(type=ARANTOOLS_PG_BakeObject)


def unregister():
    del bpy.types.Object.arantools_bake_props
    del bpy.types.Scene.arantools_bake_scene
    del bpy.types.Scene.arantools_rt_props
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
