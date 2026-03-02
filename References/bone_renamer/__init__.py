# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful, but
# WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTIBILITY or FITNESS FOR A PARTICULAR PURPOSE. See the GNU
# General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program. If not, see <http://www.gnu.org/licenses/>.

bl_info = {
    "name" : "Bone Renamer",
    "author" : "Aran", 
    "description" : "Rename Bones Easily",
    "blender" : (3, 0, 0),
    "version" : (1, 0, 0),
    "location" : "",
    "warning" : "",
    "doc_url": "", 
    "tracker_url": "", 
    "category" : "3D View" 
}


import bpy
import bpy.utils.previews


addon_keymaps = {}
_icons = None
class SNA_PT_BONE_RENAMER_6AE01(bpy.types.Panel):
    bl_label = 'Bone Renamer'
    bl_idname = 'SNA_PT_BONE_RENAMER_6AE01'
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_context = ''
    bl_category = 'Bone Renamer'
    bl_order = 0
    bl_ui_units_x=0

    @classmethod
    def poll(cls, context):
        return not (False)

    def draw_header(self, context):
        layout = self.layout

    def draw(self, context):
        layout = self.layout


class SNA_OT_Reset_Counter_B506B(bpy.types.Operator):
    bl_idname = "sna.reset_counter_b506b"
    bl_label = "Reset Counter"
    bl_description = ""
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        if bpy.app.version >= (3, 0, 0) and True:
            cls.poll_message_set('')
        return not False

    def execute(self, context):
        bpy.context.scene.sna_inc = 1
        return {"FINISHED"}

    def invoke(self, context, event):
        return self.execute(context)


class SNA_OT_Renamebone_4Fca7(bpy.types.Operator):
    bl_idname = "sna.renamebone_4fca7"
    bl_label = "RenameBone"
    bl_description = ""
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        if bpy.app.version >= (3, 0, 0) and True:
            cls.poll_message_set('')
        return not False

    def execute(self, context):
        bpy.context.active_bone.name = bpy.context.scene.sna_format.replace('N1', '0' + str(bpy.context.scene.sna_n1)).replace('N2', '0' + str(bpy.context.scene.sna_n2)).replace('N3', '0' + str(bpy.context.scene.sna_n3)).replace('T1', bpy.context.scene.sna_t1).replace('T2', bpy.context.scene.sna_t2).replace('T3', bpy.context.scene.sna_t3).replace('T4', bpy.context.scene.sna_t4).replace('INC', '0' + str(bpy.context.scene.sna_inc))
        bpy.context.scene.sna_inc = int(bpy.context.scene.sna_inc + 1.0)
        bpy.ops.armature.select_hierarchy('INVOKE_DEFAULT', direction='CHILD')
        return {"FINISHED"}

    def invoke(self, context, event):
        return self.execute(context)


class SNA_PT_CONTROLS_D0E56(bpy.types.Panel):
    bl_label = 'Controls'
    bl_idname = 'SNA_PT_CONTROLS_D0E56'
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_context = ''
    bl_order = 0
    bl_parent_id = 'SNA_PT_BONE_RENAMER_6AE01'
    bl_ui_units_x=0

    @classmethod
    def poll(cls, context):
        return not (False)

    def draw_header(self, context):
        layout = self.layout

    def draw(self, context):
        layout = self.layout
        layout.prop(bpy.context.scene, 'sna_format', text='Format', icon_value=0, emboss=True)
        layout.prop(bpy.context.scene, 'sna_n1', text='N1', icon_value=0, emboss=True)
        layout.prop(bpy.context.scene, 'sna_n2', text='N2', icon_value=0, emboss=True)
        layout.prop(bpy.context.scene, 'sna_n3', text='N3', icon_value=0, emboss=True)
        layout.prop(bpy.context.scene, 'sna_t1', text='T1', icon_value=0, emboss=True)
        layout.prop(bpy.context.scene, 'sna_t2', text='T2', icon_value=0, emboss=True)
        layout.prop(bpy.context.scene, 'sna_t3', text='T3', icon_value=0, emboss=True)
        layout.prop(bpy.context.scene, 'sna_t4', text='T4', icon_value=0, emboss=True)
        layout.prop(bpy.context.scene, 'sna_inc', text='INC', icon_value=0, emboss=True)
        op = layout.operator('sna.reset_counter_b506b', text='Reset Counter', icon_value=0, emboss=True, depress=False)
        op = layout.operator('sna.renamebone_4fca7', text='Rename', icon_value=701, emboss=True, depress=False)
        layout.label(text='Composed Name: ' + bpy.context.scene.sna_format.replace('N1', '0' + str(bpy.context.scene.sna_n1)).replace('N2', '0' + str(bpy.context.scene.sna_n2)).replace('N3', '0' + str(bpy.context.scene.sna_n3)).replace('T1', bpy.context.scene.sna_t1).replace('T2', bpy.context.scene.sna_t2).replace('T3', bpy.context.scene.sna_t3).replace('T4', bpy.context.scene.sna_t4).replace('INC', '0' + str(bpy.context.scene.sna_inc)), icon_value=0)


def register():
    global _icons
    _icons = bpy.utils.previews.new()
    bpy.types.Scene.sna_format = bpy.props.StringProperty(name='Format', description='', default='T1_N1_T2_N2_T3_N3_T4', subtype='NONE', maxlen=0)
    bpy.types.Scene.sna_n1 = bpy.props.IntProperty(name='N1', description='', default=0, subtype='NONE')
    bpy.types.Scene.sna_n2 = bpy.props.IntProperty(name='N2', description='', default=0, subtype='NONE')
    bpy.types.Scene.sna_n3 = bpy.props.IntProperty(name='N3', description='', default=0, subtype='NONE')
    bpy.types.Scene.sna_t1 = bpy.props.StringProperty(name='T1', description='', default='finger', subtype='NONE', maxlen=0)
    bpy.types.Scene.sna_t2 = bpy.props.StringProperty(name='T2', description='', default='index', subtype='NONE', maxlen=0)
    bpy.types.Scene.sna_t3 = bpy.props.StringProperty(name='T3', description='', default='', subtype='NONE', maxlen=0)
    bpy.types.Scene.sna_t4 = bpy.props.StringProperty(name='T4', description='', default='l', subtype='NONE', maxlen=0)
    bpy.types.Scene.sna_inc = bpy.props.IntProperty(name='INC', description='', default=1, subtype='NONE')
    bpy.utils.register_class(SNA_PT_BONE_RENAMER_6AE01)
    bpy.utils.register_class(SNA_OT_Reset_Counter_B506B)
    bpy.utils.register_class(SNA_OT_Renamebone_4Fca7)
    bpy.utils.register_class(SNA_PT_CONTROLS_D0E56)
    kc = bpy.context.window_manager.keyconfigs.addon
    km = kc.keymaps.new(name='Window', space_type='EMPTY')
    kmi = km.keymap_items.new('sna.renamebone_4fca7', 'R', 'PRESS',
        ctrl=False, alt=True, shift=True, repeat=False)
    addon_keymaps['6492A'] = (km, kmi)
    kc = bpy.context.window_manager.keyconfigs.addon
    km = kc.keymaps.new(name='Window', space_type='EMPTY')
    kmi = km.keymap_items.new('sna.reset_counter_b506b', 'Y', 'PRESS',
        ctrl=False, alt=False, shift=True, repeat=False)
    addon_keymaps['2213D'] = (km, kmi)


def unregister():
    global _icons
    bpy.utils.previews.remove(_icons)
    wm = bpy.context.window_manager
    kc = wm.keyconfigs.addon
    for km, kmi in addon_keymaps.values():
        km.keymap_items.remove(kmi)
    addon_keymaps.clear()
    del bpy.types.Scene.sna_inc
    del bpy.types.Scene.sna_t4
    del bpy.types.Scene.sna_t3
    del bpy.types.Scene.sna_t2
    del bpy.types.Scene.sna_t1
    del bpy.types.Scene.sna_n3
    del bpy.types.Scene.sna_n2
    del bpy.types.Scene.sna_n1
    del bpy.types.Scene.sna_format
    bpy.utils.unregister_class(SNA_PT_BONE_RENAMER_6AE01)
    bpy.utils.unregister_class(SNA_OT_Reset_Counter_B506B)
    bpy.utils.unregister_class(SNA_OT_Renamebone_4Fca7)
    bpy.utils.unregister_class(SNA_PT_CONTROLS_D0E56)
