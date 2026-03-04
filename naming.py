import bpy
import re
from bpy.types import Operator, Panel


# ============================================================================
# Object Sequence Namer
# ============================================================================

class ARANTOOLS_PG_SeqNamer(bpy.types.PropertyGroup):
    base_name: bpy.props.StringProperty(
        name="Base Name",
        description=(
            "Root name for the numbered sequence.\n"
            "e.g. 'Monkey' produces 'Monkey_01', 'Monkey_02', …"
        ),
        default="Object",
    )
    padding: bpy.props.IntProperty(
        name="Digits",
        description=(
            "How many digits to use for the index.\n"
            "2 → '_01'   3 → '_001'"
        ),
        default=2,
        min=1,
        max=6,
    )
    replace_existing: bpy.props.BoolProperty(
        name="Replace Existing",
        description=(
            "When OFF: objects that already match 'BaseName_NN' keep their number — "
            "only un-named objects are assigned new numbers (filling gaps first).\n"
            "When ON: every selected object is renumbered from scratch regardless of "
            "its current name. Matched objects are sorted by their current number "
            "before renaming so their relative order is preserved."
        ),
        default=False,
    )


class ARANTOOLS_OT_SequenceNameObjects(Operator):
    """Name selected objects as a numbered sequence.

Respects existing names: objects already called BaseName_NN keep their
number (gaps in the sequence are filled first with un-named objects).
Enable 'Replace Existing' to renumber everything from scratch."""
    bl_idname = "arantools.sequence_name_objects"
    bl_label = "Apply Sequence Names"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        props   = context.scene.arantools_seq_namer
        base    = props.base_name.strip()
        padding = props.padding
        replace = props.replace_existing

        if not base:
            self.report({'ERROR'}, "Base Name cannot be empty.")
            return {'CANCELLED'}

        selected = list(context.selected_objects)
        if not selected:
            self.report({'WARNING'}, "No objects selected.")
            return {'CANCELLED'}

        # Regex: exactly  <base>_<digits>  and nothing else
        pattern = re.compile(r'^' + re.escape(base) + r'_(\d+)$')

        # ── Scene-wide existing assignments ──────────────────────────────────
        # Maps  number (int) → object  for every object in the scene
        scene_matches = {}
        for obj in context.scene.objects:
            m = pattern.match(obj.name)
            if m:
                scene_matches[int(m.group(1))] = obj

        # ── Classify selected objects ────────────────────────────────────────
        matched_selected   = {}   # int → object  (already named correctly)
        unmatched_selected = []   # objects with non-conforming names

        for obj in selected:
            m = pattern.match(obj.name)
            if m:
                matched_selected[int(m.group(1))] = obj
            else:
                unmatched_selected.append(obj)

        # ── Build the ordered list of objects to rename ──────────────────────
        if replace:
            # Renumber everyone; matched ones go first sorted by current number
            all_to_rename = (
                [obj for _, obj in sorted(matched_selected.items())]
                + unmatched_selected
            )
            # Numbers held only by objects outside the selection block new slots
            taken_nums = {
                num for num, obj in scene_matches.items()
                if obj not in selected
            }
        else:
            # Only rename objects without a conforming name
            all_to_rename = unmatched_selected
            # All scene matches block number slots (including selected ones that
            # are already correctly named — we must not steal their numbers)
            taken_nums = set(scene_matches.keys())

        if not all_to_rename:
            self.report(
                {'INFO'},
                "Nothing to rename — all selected objects are already in the sequence."
            )
            return {'FINISHED'}

        # ── Find available numbers (fill gaps, then extend) ──────────────────
        available = []
        n = 1
        while len(available) < len(all_to_rename):
            if n not in taken_nums:
                available.append(n)
            n += 1

        # ── Apply names via temp names to avoid Blender's ".001" conflict ────
        TEMP = "__ARANTOOLS_SEQTMP_"
        for i, obj in enumerate(all_to_rename):
            obj.name = f"{TEMP}{i}"

        for obj, num in zip(all_to_rename, available):
            obj.name = f"{base}_{str(num).zfill(padding)}"

        self.report(
            {'INFO'},
            f"Renamed {len(all_to_rename)} object(s) → '{base}_NN' sequence."
        )
        return {'FINISHED'}


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
    ARANTOOLS_PG_SeqNamer,
    ARANTOOLS_OT_SequenceNameObjects,
    ARANTOOLS_OT_Reset_Counter,
    ARANTOOLS_OT_Rename_Bone,
]


def register():
    for cls in classes:
        bpy.utils.register_class(cls)

    bpy.types.Scene.arantools_seq_namer = bpy.props.PointerProperty(
        type=ARANTOOLS_PG_SeqNamer
    )

    # Scene properties
    bpy.types.Scene.arantools_format = bpy.props.StringProperty(
        name='Format', default='T1_INC')
    bpy.types.Scene.arantools_n1 = bpy.props.IntProperty(name='N1', default=0)
    bpy.types.Scene.arantools_n2 = bpy.props.IntProperty(name='N2', default=0)
    bpy.types.Scene.arantools_n3 = bpy.props.IntProperty(name='N3', default=0)
    bpy.types.Scene.arantools_t1 = bpy.props.StringProperty(name='T1', default='name')
    bpy.types.Scene.arantools_t2 = bpy.props.StringProperty(name='T2', default='')
    bpy.types.Scene.arantools_t3 = bpy.props.StringProperty(name='T3', default='')
    bpy.types.Scene.arantools_t4 = bpy.props.StringProperty(name='T4', default='')
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
    del bpy.types.Scene.arantools_seq_namer
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
