import bpy
from bpy.types import Operator, Panel


# ============================================================================
# Operators
# ============================================================================

class ARANTOOLS_OT_Reset_Counter(Operator):
    """Reset bone naming counter"""
    bl_idname = "arantools.reset_counter"
    bl_label = "Reset Counter"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        context.scene.arantools_inc = 1
        return {'FINISHED'}


class ARANTOOLS_OT_Rename_Bone(Operator):
    """Rename active bone using format template"""
    bl_idname = "arantools.rename_bone"
    bl_label = "Rename Bone"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return context.mode == 'EDIT' and context.active_bone is not None

    def execute(self, context):
        scene = context.scene
        bone = context.active_bone

        # Build the name from format string
        name = scene.arantools_format
        name = name.replace('N1', '0' + str(scene.arantools_n1))
        name = name.replace('N2', '0' + str(scene.arantools_n2))
        name = name.replace('N3', '0' + str(scene.arantools_n3))
        name = name.replace('T1', scene.arantools_t1)
        name = name.replace('T2', scene.arantools_t2)
        name = name.replace('T3', scene.arantools_t3)
        name = name.replace('T4', scene.arantools_t4)
        name = name.replace('INC', '0' + str(scene.arantools_inc))

        bone.name = name
        scene.arantools_inc += 1

        # Select hierarchy
        bpy.ops.armature.select_hierarchy('INVOKE_DEFAULT', direction='CHILD')

        return {'FINISHED'}


# ============================================================================
# Registration
# ============================================================================

classes = [
    ARANTOOLS_OT_Reset_Counter,
    ARANTOOLS_OT_Rename_Bone,
]


def register():
    for cls in classes:
        bpy.utils.register_class(cls)

    # Scene properties
    bpy.types.Scene.arantools_format = bpy.props.StringProperty(
        name='Format', default='T1_N1_T2_N2_T3_N3_T4')
    bpy.types.Scene.arantools_n1 = bpy.props.IntProperty(name='N1', default=0)
    bpy.types.Scene.arantools_n2 = bpy.props.IntProperty(name='N2', default=0)
    bpy.types.Scene.arantools_n3 = bpy.props.IntProperty(name='N3', default=0)
    bpy.types.Scene.arantools_t1 = bpy.props.StringProperty(name='T1', default='finger')
    bpy.types.Scene.arantools_t2 = bpy.props.StringProperty(name='T2', default='index')
    bpy.types.Scene.arantools_t3 = bpy.props.StringProperty(name='T3', default='')
    bpy.types.Scene.arantools_t4 = bpy.props.StringProperty(name='T4', default='l')
    bpy.types.Scene.arantools_inc = bpy.props.IntProperty(name='INC', default=1)

    # Keymaps
    wm = bpy.context.window_manager
    kc = wm.keyconfigs.addon
    km = kc.keymaps.new(name='Window', space_type='EMPTY')
    kmi = km.keymap_items.new('arantools.rename_bone', 'R', 'PRESS',
                               ctrl=False, alt=True, shift=True)
    km = kc.keymaps.new(name='Window', space_type='EMPTY')
    kmi = km.keymap_items.new('arantools.reset_counter', 'Y', 'PRESS',
                               ctrl=False, alt=False, shift=True)


def unregister():
    del bpy.types.Scene.arantools_inc
    del bpy.types.Scene.arantools_t4
    del bpy.types.Scene.arantools_t3
    del bpy.types.Scene.arantools_t2
    del bpy.types.Scene.arantools_t1
    del bpy.types.Scene.arantools_n3
    del bpy.types.Scene.arantools_n2
    del bpy.types.Scene.arantools_n1
    del bpy.types.Scene.arantools_format

    # Unregister keymaps
    wm = bpy.context.window_manager
    kc = wm.keyconfigs.addon
    for km in kc.keymaps:
        for kmi in list(km.keymap_items):
            if kmi.idname in ('arantools.rename_bone', 'arantools.reset_counter'):
                km.keymap_items.remove(kmi)

    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
