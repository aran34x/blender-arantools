"""
Tree Branch Skeleton â€” author a tree as a vertex-only mesh.

The artist draws a "stick figure" of branches with vertices + edges.
This module walks the mesh from a user-selected root vertex, partitions
it into branches (the most-aligned neighbor at each junction is the
"continuation", everything else spawns a child branch), and writes
attributes split across domains:

  EDGE  : branch_id, branch_depth, is_branch_entry
          (categorical â€” each edge belongs to exactly one branch, so
          downstream geonodes can group/separate cleanly without the
          junction-vertex ambiguity of point-domain storage)
  POINT : branch_t, radius, tilt, is_root, branch_base_z, branch_top_z,
          is_underground
          (smoothly varying or per-vertex; survive Meshâ†’Curveâ†’Mesh)

A downstream geometry node setup then reads those attributes to sweep
real bark geometry along the skeleton.
"""

import bpy
import bmesh
from mathutils import Vector
from bpy.types import Operator


# ============================================================================
# Branch hierarchy analysis
# ============================================================================

def _build_branches(bm, root_vert):
    """Walk from root_vert, partition all reachable vertices into branches.

    Each junction picks one neighbor as the 'continuation' (smallest
    direction change) and the rest as 'child branches'. At the root we
    have no incoming direction, so we pick the most-upward edge as the
    trunk continuation. Returns a list of dicts:
        {id, parent_id, depth, vert_indices}
    """
    visited = set()
    branches = []

    def walk(start_vert, incoming_dir, parent_id, depth, entry_vert_idx):
        branch_id = len(branches)
        branches.append(None)  # reserve slot before recursing

        verts = [start_vert.index]
        visited.add(start_vert.index)
        current = start_vert
        prev_dir = incoming_dir

        while True:
            cands = [e.other_vert(current) for e in current.link_edges
                     if e.other_vert(current).index not in visited]

            if not cands:
                break

            if len(cands) == 1:
                nxt = cands[0]
            else:
                # At junction: continuation = best-aligned with incoming.
                # At root (no incoming) = most upward edge.
                if prev_dir is None:
                    cands.sort(key=lambda v: (v.co - current.co).z,
                               reverse=True)
                else:
                    def alignment(v):
                        d = v.co - current.co
                        if d.length_squared < 1e-12:
                            return -1.0
                        return d.normalized().dot(prev_dir)
                    cands.sort(key=alignment, reverse=True)

                continuation = cands[0]
                for child in cands[1:]:
                    d = child.co - current.co
                    if d.length_squared > 1e-12:
                        child_dir = d.normalized()
                    else:
                        child_dir = Vector((0.0, 0.0, 1.0))
                    walk(child, child_dir, branch_id, depth + 1,
                         current.index)
                nxt = continuation

            d = nxt.co - current.co
            if d.length_squared > 1e-12:
                prev_dir = d.normalized()
            current = nxt
            verts.append(current.index)
            visited.add(current.index)

        branches[branch_id] = {
            "id": branch_id,
            "parent_id": parent_id,
            "depth": depth,
            "vert_indices": verts,
            # Vertex in the parent branch where this branch enters (-1 for
            # the root). The edge (entry_vert_idx â†’ verts[0]) is the
            # "bridge edge" that connects parent's continuation point to
            # this branch's first interior vertex.
            "entry_vert_idx": entry_vert_idx,
        }

    walk(root_vert, None, -1, 0, -1)
    return branches


# ============================================================================
# Attribute writing
# ============================================================================

# Names match Blender's built-in curve attributes where possible â€” when the
# skeleton is later converted Meshâ†’Curve in geonodes, `radius` and `tilt`
# carry over to the curve's native sockets.
_ATTR_SPEC = [
    # branch_id / branch_depth live on EDGE domain â€” each edge belongs to
    # exactly one branch, so geonodes can group/separate by branch without
    # the junction-vertex ambiguity that comes with point-domain storage.
    ("branch_id",     'INT',     'EDGE'),
    ("branch_depth",  'INT',     'EDGE'),
    # True on the single "bridge" edge that connects each child branch's
    # first vertex to its junction in the parent branch. Downstream
    # geonodes delete these to split the skeleton into per-branch chains.
    ("is_branch_entry", 'BOOLEAN', 'EDGE'),
    # Smoothly varying per-vertex values that need to interpolate along
    # the branch and survive Meshâ†’Curveâ†’Mesh round trips.
    ("branch_t",      'FLOAT',   'POINT'),
    ("radius",        'FLOAT',   'POINT'),
    ("tilt",          'FLOAT',   'POINT'),
    ("is_root",       'BOOLEAN', 'POINT'),
    # Per-branch pivot, stamped to every vertex of that branch. The
    # pivot is the X/Y/Z of the branch's lowest-Z vertex (its "base"),
    # which the wood shader uses as the per-branch wind sway origin.
    # The UV geonode writes:
    #   UVMap2.U = branch_base_z
    #   UVMap1 = (branch_base_x, 1 + branch_base_y)   (Y pre-baked
    #     with the +1 the shader undoes via its "1 - V" axis flip)
    ("branch_base_x",  'FLOAT',   'POINT'),
    ("branch_base_y",  'FLOAT',   'POINT'),
    ("branch_base_z",  'FLOAT',   'POINT'),
    ("branch_top_z",   'FLOAT',   'POINT'),
    # Any vertex whose Z sits below the root vertex's Z is an underground
    # root and the wind UV geonode masks it out (zero amplitude).
    ("is_underground", 'BOOLEAN', 'POINT'),
    # tree_root_z used to be stamped for the wind-mask geonode; the
    # current UV geonode doesn't write wind to vertex color anymore, so
    # this attribute is unused. Kept commented out so re-baking doesn't
    # leave a stale entry â€” re-add if a future geonode needs it.
    # ("tree_root_z",  'FLOAT',   'POINT'),
]


def _ensure_attribute(mesh, name, data_type, domain):
    """Get the attribute, or remove + recreate if its type/domain has drifted."""
    attr = mesh.attributes.get(name)
    if attr is not None and (attr.data_type != data_type or attr.domain != domain):
        mesh.attributes.remove(attr)
        attr = None
    if attr is None:
        attr = mesh.attributes.new(name=name, type=data_type, domain=domain)
    return attr


# ============================================================================
# Operators
# ============================================================================

class ARANTOOLS_OT_TreeNewSkeleton(Operator):
    """Create a fresh single-vertex mesh at the 3D cursor and drop into
edit mode so the artist can start extruding branches."""
    bl_idname  = "arantools.tree_new_skeleton"
    bl_label   = "New Tree Skeleton"
    bl_options = {'REGISTER', 'UNDO'}

    name: bpy.props.StringProperty(
        name="Name", default="TreeSkeleton",
        description="Object name for the new skeleton",
    )

    def execute(self, context):
        mesh = bpy.data.meshes.new(self.name)
        mesh.from_pydata([(0, 0, 0)], [], [])
        mesh.update()
        obj = bpy.data.objects.new(self.name, mesh)
        context.collection.objects.link(obj)
        obj.location = context.scene.cursor.location

        for o in context.selected_objects:
            o.select_set(False)
        obj.select_set(True)
        context.view_layer.objects.active = obj
        if obj.mode != 'EDIT':
            bpy.ops.object.mode_set(mode='EDIT')
        return {'FINISHED'}


# ============================================================================
# Per-category taper curves (Trunk / Branch / Root)
# ============================================================================
#
# Each category gets a Float Curve the user can edit in the N-panel. The
# X axis runs 0 (branch base) â†’ 1 (branch tip); the Y output is the
# radius multiplier applied on top of that category's base_radius * the
# global depth_falloff^depth term.
#
# CurveMappings can't live directly on a PropertyGroup, so we stash them
# inside a hidden ShaderNodeTree with three Float Curve nodes â€” same
# pattern Blender itself uses for things like the brush falloff curve.

CURVES_NODEGROUP = "AranTools_TreeBranchCurves"
CURVE_TRUNK  = "trunk_taper"
CURVE_BRANCH = "branch_taper"
CURVE_ROOT   = "root_taper"
_CURVE_LABELS = (CURVE_TRUNK, CURVE_BRANCH, CURVE_ROOT)


def _ensure_curves_node_group():
    """Create the hidden node group holding our three taper curves, or
    return it if it already exists. Missing curves are added on the fly
    so the group survives partial corruption."""
    ng = bpy.data.node_groups.get(CURVES_NODEGROUP)
    if ng is None:
        ng = bpy.data.node_groups.new(CURVES_NODEGROUP, 'ShaderNodeTree')
        # Fake user so it survives "save & reload" even with no references.
        ng.use_fake_user = True
    for label in _CURVE_LABELS:
        if label not in ng.nodes:
            n = ng.nodes.new('ShaderNodeFloatCurve')
            n.name  = label
            n.label = label
            # Default profile: full radius at base, zero at tip.
            cm = n.mapping
            curve = cm.curves[0]
            curve.points[0].location = (0.0, 1.0)
            curve.points[1].location = (1.0, 0.0)
            cm.update()
    return ng


def get_taper_curve_node(label):
    """Read-only lookup of one taper curve's owning Float Curve node, safe
    to call from UI draw (which forbids touching bpy.data). Returns None
    if the node group hasn't been created yet â€” the UI handles that."""
    ng = bpy.data.node_groups.get(CURVES_NODEGROUP)
    if ng is None:
        return None
    return ng.nodes.get(label)


def _initialize_taper_curves():
    """Initialize all three curves once and return a {label: (mapping,
    curve)} dict for fast per-vertex evaluation in the operator."""
    ng = _ensure_curves_node_group()
    out = {}
    for label in _CURVE_LABELS:
        node = ng.nodes.get(label)
        if node is None:
            out[label] = None
            continue
        m = node.mapping
        m.initialize()
        out[label] = (m, m.curves[0])
    return out


def _eval_taper(curves, label, t):
    """Sample one of the prepared curves at t. Returns 1.0 fallback if
    the curve is somehow missing."""
    pair = curves.get(label)
    if pair is None:
        return max(0.0, 1.0 - t)
    mapping, curve = pair
    return mapping.evaluate(curve, t)


class ARANTOOLS_TreeBranch_Props(bpy.types.PropertyGroup):
    """Persistent N-panel settings for the Branch Skeleton tool.

    Kept on the Scene so the values survive between runs of the operator
    (and across Blender sessions). The operator reads from here at
    execute time â€” there are no F6 redo properties anymore."""

    # The ONLY radius knob: the radius at the base of the trunk. Every
    # other radius in the tree is derived â€” child branches inherit their
    # starting radius from the parent's radius at the junction vertex,
    # then their assigned taper curve scales it along the branch length.
    # Falloff between generations is implicit in the curve shape (e.g. a
    # curve ending at y=0.6 means each child is 60% of the parent at the
    # junction; ending at y=0 means it tapers to a point).
    base_radius: bpy.props.FloatProperty(
        name="Base Radius",
        description="Radius at the very base of the trunk. Every other "
                    "radius in the tree is inferred from this plus the "
                    "taper curves",
        default=1.0, min=0.001, soft_max=10.0,
    )
    preserve_radius: bpy.props.BoolProperty(
        name="Preserve Manual Radius",
        description="Keep existing per-vertex radius values from a previous "
                    "run (anything > 0). Newly added vertices still get the "
                    "computed default. Turn off to let the radius sliders "
                    "fully re-stamp the skeleton",
        default=True,
    )
    preserve_tilt: bpy.props.BoolProperty(
        name="Preserve Manual Tilt",
        description="Keep existing per-vertex tilt values from a previous "
                    "run. Turn off to reset all tilts to 0",
        default=True,
    )
    show_settings: bpy.props.BoolProperty(
        name="Show Settings",
        description="Show/hide the Base Radius + Taper Curves + Preserve "
                    "options. Collapse this section once you're happy "
                    "with the curves so the panel stays tidy",
        default=False,
    )


# ============================================================================
# Root vertex storage
# ============================================================================
#
# We store the trunk-base vertex index as a mesh-data custom property so it
# travels with the object (and survives file save/load). This is the single
# source of truth â€” Setup reads it, "Select Root" highlights it, the UI
# displays it. The current selection is irrelevant to Setup.

ROOT_VERT_KEY = "arantools_root_vert"


def get_root_vert_index(mesh):
    """Return the stored root vertex index, or None if unset."""
    if mesh is None:
        return None
    v = mesh.get(ROOT_VERT_KEY)
    if v is None:
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def set_root_vert_index(mesh, index):
    mesh[ROOT_VERT_KEY] = int(index)


def clear_root_vert_index(mesh):
    if ROOT_VERT_KEY in mesh.keys():
        del mesh[ROOT_VERT_KEY]


class ARANTOOLS_OT_TreeSetRootFromSelection(Operator):
    """Designate the currently selected vertex as the persistent root
(trunk base) of this tree skeleton. Stored on the mesh data so Setup
Branch Skeleton can run repeatedly without re-selection. Requires
exactly one selected vertex."""
    bl_idname  = "arantools.tree_set_root_from_selection"
    bl_label   = "Set Root From Selection"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        obj = context.active_object
        return (obj is not None and obj.type == 'MESH'
                and obj.mode == 'EDIT')

    def execute(self, context):
        obj = context.active_object
        bm = bmesh.from_edit_mesh(obj.data)
        bm.verts.ensure_lookup_table()
        selected = [v for v in bm.verts if v.select]
        if len(selected) != 1:
            self.report({'ERROR'},
                        f"Select exactly one vertex (got {len(selected)}).")
            return {'CANCELLED'}
        # Mesh custom-prop writes don't need an object-mode switch, but
        # bmesh changes must be flushed first to keep indices stable.
        bmesh.update_edit_mesh(obj.data)
        set_root_vert_index(obj.data, selected[0].index)
        self.report({'INFO'},
                    f"Root vertex set to #{selected[0].index}.")
        return {'FINISHED'}


class ARANTOOLS_OT_TreeSelectRoot(Operator):
    """Select the stored root vertex on the active mesh (and deselect
everything else). Useful when you've lost track of which vertex was
designated the trunk base."""
    bl_idname  = "arantools.tree_select_root"
    bl_label   = "Select Root"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        obj = context.active_object
        if obj is None or obj.type != 'MESH':
            return False
        return get_root_vert_index(obj.data) is not None

    def execute(self, context):
        obj = context.active_object
        idx = get_root_vert_index(obj.data)
        # Ensure we're in edit mode so the user can see the selection.
        if obj.mode != 'EDIT':
            bpy.ops.object.mode_set(mode='EDIT')
        bm = bmesh.from_edit_mesh(obj.data)
        bm.verts.ensure_lookup_table()
        if not (0 <= idx < len(bm.verts)):
            self.report({'ERROR'},
                        f"Stored root index {idx} is out of range.")
            return {'CANCELLED'}
        # Vertex selection mode + clear, then select + make active.
        context.tool_settings.mesh_select_mode = (True, False, False)
        for v in bm.verts:
            v.select = False
        for e in bm.edges:
            e.select = False
        for f in bm.faces:
            f.select = False
        target = bm.verts[idx]
        target.select = True
        bm.select_history.clear()
        bm.select_history.add(target)
        bmesh.update_edit_mesh(obj.data)
        return {'FINISHED'}


class ARANTOOLS_OT_TreeEnsureTaperCurves(Operator):
    """Create (or restore) the hidden Float Curve nodes that drive the
Trunk / Branch / Root taper. Normally done automatically on addon
register; this is a one-click recovery if the node group ever goes
missing."""
    bl_idname  = "arantools.tree_ensure_taper_curves"
    bl_label   = "Create Taper Curves"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        _ensure_curves_node_group()
        self.report({'INFO'}, "Taper curves ready.")
        return {'FINISHED'}


class ARANTOOLS_OT_TreeClearRoot(Operator):
    """Forget the stored root vertex. The Setup button will be disabled
until a new root is designated."""
    bl_idname  = "arantools.tree_clear_root"
    bl_label   = "Clear Root"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        obj = context.active_object
        if obj is None or obj.type != 'MESH':
            return False
        return get_root_vert_index(obj.data) is not None

    def execute(self, context):
        clear_root_vert_index(context.active_object.data)
        self.report({'INFO'}, "Root vertex cleared.")
        return {'FINISHED'}


class ARANTOOLS_OT_TreeSetupBranchSkeleton(Operator):
    """Walk the skeleton from the stored root vertex, partition it into
branches (continuation chosen by smallest direction change at junctions;
upward edge at the root), and stamp branch_id / branch_depth / branch_t /
radius / tilt / is_root onto every reachable vertex. The root is the
single vertex designated via 'Set Root From Selection' â€” Setup never
reads the current selection. Run again after edits to re-analyze."""
    bl_idname  = "arantools.tree_setup_branch_skeleton"
    bl_label   = "Setup Branch Skeleton"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        obj = context.active_object
        if obj is None or obj.type != 'MESH' or obj.mode != 'EDIT':
            return False
        return get_root_vert_index(obj.data) is not None

    def execute(self, context):
        obj = context.active_object
        props = context.scene.arantools_tree_branch
        bm = bmesh.from_edit_mesh(obj.data)
        bm.verts.ensure_lookup_table()

        # The root is stored on the mesh as a single integer (custom prop
        # ROOT_VERT_KEY). We NEVER infer it from the current selection â€”
        # the artist designates it once via "Set Root From Selection", and
        # Setup just looks it up. If unset or out of range, refuse to run.
        stored = get_root_vert_index(obj.data)
        if stored is None:
            self.report({'ERROR'},
                        "No root vertex set. Select one and click "
                        "'Set Root From Selection'.")
            return {'CANCELLED'}
        if not (0 <= stored < len(bm.verts)):
            self.report({'ERROR'},
                        f"Stored root index {stored} is out of range "
                        f"(mesh has {len(bm.verts)} verts). Re-set the root.")
            return {'CANCELLED'}
        root = bm.verts[stored]

        branches = _build_branches(bm, root)

        # Build a (min,max)â†’edge-index lookup for stamping edge-domain attrs.
        bm.edges.ensure_lookup_table()
        edge_lookup = {}
        for e in bm.edges:
            a, b = e.verts[0].index, e.verts[1].index
            edge_lookup[(min(a, b), max(a, b))] = e.index

        def _edge_idx(va, vb):
            return edge_lookup.get((min(va, vb), max(va, vb)))

        # Per-vertex (POINT) value arrays. Unreachable verts stay at defaults.
        n_v = len(bm.verts)
        branch_t       = [0.0]   * n_v
        radius         = [0.0]   * n_v
        tilt           = [0.0]   * n_v
        is_root        = [False] * n_v
        is_root[root.index] = True
        branch_base_x  = [0.0]   * n_v
        branch_base_y  = [0.0]   * n_v
        branch_base_z  = [0.0]   * n_v
        branch_top_z   = [0.0]   * n_v
        # Use the root vertex's Z as the ground threshold â€” anything strictly
        # below that is a root/underground vertex and gets wind-masked.
        root_z = root.co.z
        is_underground = [bm.verts[i].co.z < root_z for i in range(n_v)]

        # Per-edge (EDGE) value arrays. Edges not part of any branch (loose
        # or in unreachable components) keep branch_id = -1.
        n_e = len(bm.edges)
        edge_branch_id    = [-1]    * n_e
        edge_branch_depth = [-1]    * n_e
        edge_is_entry     = [False] * n_e

        # Classify each branch as TRUNK (the root branch itself), ROOT (a
        # branch that descends below the trunk-base Z, plus all its
        # descendants), or BRANCH (everything else). Branches are appended
        # in DFS order so parent's category is always resolved first.
        for b in branches:
            if b["parent_id"] == -1:
                b["category"] = 'TRUNK'
                continue
            parent_cat = branches[b["parent_id"]]["category"]
            if parent_cat == 'ROOT':
                b["category"] = 'ROOT'
                continue
            first_z = bm.verts[b["vert_indices"][0]].co.z
            b["category"] = 'ROOT' if first_z < root_z else 'BRANCH'

        # Pre-initialize the three taper curves for fast per-vertex lookup.
        taper_curves = _initialize_taper_curves()
        category_curve = {
            'TRUNK':  CURVE_TRUNK,
            'BRANCH': CURVE_BRANCH,
            'ROOT':   CURVE_ROOT,
        }

        # Process branches parent-first (which is the natural DFS order
        # of the list: parent_id is always < self.id). Each branch reads
        # its starting radius from the parent's already-filled radius at
        # the junction vertex, so radii are continuous across branchings.
        max_depth = 0
        for b in branches:
            verts_in_branch = b["vert_indices"]
            count = len(verts_in_branch)
            cat   = b["category"]
            max_depth = max(max_depth, b["depth"])

            if b["parent_id"] == -1:
                # Root of the walk = trunk. Starts at the configured base.
                start_r = props.base_radius
            else:
                # Inherit from the parent's radius at the junction vertex
                # â€” already computed since we iterate parent-first.
                start_r = radius[b["entry_vert_idx"]]

            # Per-branch pivot: the X/Y/Z of the branch's lowest-Z
            # vertex (its base). Stamped on every vert of the branch so
            # the UV geonode can write UVMap1 / UVMap2 from it.
            cos = [(bm.verts[vi].co.x,
                    bm.verts[vi].co.y,
                    bm.verts[vi].co.z) for vi in verts_in_branch]
            base_x, base_y, b_min_z = min(cos, key=lambda c: c[2])
            b_max_z = max(c[2] for c in cos)

            for i, vi in enumerate(verts_in_branch):
                t = i / max(1, count - 1)
                # Per-VERTEX curve selection: any vertex below root_z
                # uses the Root curve, regardless of which branch it sits
                # in. This handles a trunk whose base dips underground.
                v_z = bm.verts[vi].co.z
                if v_z < root_z or cat == 'ROOT':
                    curve_label = CURVE_ROOT
                elif cat == 'TRUNK':
                    curve_label = CURVE_TRUNK
                else:
                    curve_label = CURVE_BRANCH
                multiplier = _eval_taper(taper_curves, curve_label, t)
                branch_t[vi]      = t
                radius[vi]        = start_r * max(0.0, multiplier)
                tilt[vi]          = 0.0
                branch_base_x[vi] = base_x
                branch_base_y[vi] = base_y
                branch_base_z[vi] = b_min_z
                branch_top_z[vi]  = b_max_z

            # Internal edges: consecutive verts inside this branch.
            for i in range(count - 1):
                ei = _edge_idx(verts_in_branch[i], verts_in_branch[i + 1])
                if ei is not None:
                    edge_branch_id[ei]    = b["id"]
                    edge_branch_depth[ei] = b["depth"]

            # Bridge edge: parent's junction vert â†’ this branch's first vert.
            # Belongs to the child branch and is flagged so geonodes can cut.
            if b["entry_vert_idx"] >= 0:
                ei = _edge_idx(b["entry_vert_idx"], verts_in_branch[0])
                if ei is not None:
                    edge_branch_id[ei]    = b["id"]
                    edge_branch_depth[ei] = b["depth"]
                    edge_is_entry[ei]     = True

        # mesh.attributes is on the object data; safest to write from
        # Object Mode so bmesh and mesh-data stay in sync.
        bmesh.update_edit_mesh(obj.data)
        bpy.ops.object.mode_set(mode='OBJECT')

        mesh = obj.data

        # Overlay manually-edited values from the previous bake before we
        # stamp the computed arrays back. Radius uses a > 0 sentinel since
        # 0 is meaningless (and what Blender fills for newly added verts);
        # tilt has no sentinel so we preserve every existing entry.
        if props.preserve_radius:
            old = mesh.attributes.get("radius")
            if (old is not None and old.domain == 'POINT'
                    and old.data_type == 'FLOAT'):
                for i in range(min(len(old.data), n_v)):
                    v = old.data[i].value
                    if v > 0.0:
                        radius[i] = v
        if props.preserve_tilt:
            old = mesh.attributes.get("tilt")
            if (old is not None and old.domain == 'POINT'
                    and old.data_type == 'FLOAT'):
                for i in range(min(len(old.data), n_v)):
                    tilt[i] = old.data[i].value

        values_map = {
            "branch_id":       edge_branch_id,
            "branch_depth":    edge_branch_depth,
            "is_branch_entry": edge_is_entry,
            "branch_t":        branch_t,
            "radius":          radius,
            "tilt":            tilt,
            "is_root":         is_root,
            "branch_base_x":   branch_base_x,
            "branch_base_y":   branch_base_y,
            "branch_base_z":   branch_base_z,
            "branch_top_z":    branch_top_z,
            "is_underground":  is_underground,
        }
        for name, dtype, domain in _ATTR_SPEC:
            attr = _ensure_attribute(mesh, name, dtype, domain)
            vals = values_map[name]
            for i, v in enumerate(vals):
                attr.data[i].value = v

        bpy.ops.object.mode_set(mode='EDIT')

        unreachable_edges = sum(1 for bid in edge_branch_id if bid < 0)
        n_trunk  = sum(1 for b in branches if b["category"] == 'TRUNK')
        n_branch = sum(1 for b in branches if b["category"] == 'BRANCH')
        n_root   = sum(1 for b in branches if b["category"] == 'ROOT')
        msg = (f"Analyzed: {n_trunk} trunk + {n_branch} branch(es) + "
               f"{n_root} root(s), max depth {max_depth}.")
        if unreachable_edges:
            msg += f" {unreachable_edges} edge(s) unreachable from root."
        self.report({'INFO'}, msg)
        return {'FINISHED'}


class ARANTOOLS_OT_TreeClearBranchSkeleton(Operator):
    """Remove all branch-skeleton attributes from the active mesh
(EDGE: branch_id, branch_depth, is_branch_entry; POINT: branch_t, radius,
tilt, is_root, branch_base_z, branch_top_z, is_underground). Useful
before re-authoring from scratch."""
    bl_idname  = "arantools.tree_clear_branch_skeleton"
    bl_label   = "Clear Skeleton Attributes"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        obj = context.active_object
        return obj is not None and obj.type == 'MESH'

    def execute(self, context):
        mesh = context.active_object.data
        removed = 0
        for name, _dtype, _domain in _ATTR_SPEC:
            attr = mesh.attributes.get(name)
            if attr is not None:
                mesh.attributes.remove(attr)
                removed += 1
        self.report({'INFO'}, f"Removed {removed} attribute(s).")
        return {'FINISHED'}


# ============================================================================
# Registration
# ============================================================================

classes = [
    ARANTOOLS_TreeBranch_Props,
    ARANTOOLS_OT_TreeNewSkeleton,
    ARANTOOLS_OT_TreeSetRootFromSelection,
    ARANTOOLS_OT_TreeSelectRoot,
    ARANTOOLS_OT_TreeClearRoot,
    ARANTOOLS_OT_TreeEnsureTaperCurves,
    ARANTOOLS_OT_TreeSetupBranchSkeleton,
    ARANTOOLS_OT_TreeClearBranchSkeleton,
]


def register():
    for cls in classes:
        bpy.utils.register_class(cls)
    bpy.types.Scene.arantools_tree_branch = bpy.props.PointerProperty(
        type=ARANTOOLS_TreeBranch_Props
    )
    # NOTE: we deliberately do NOT create the taper-curve node group here.
    # Blender disallows writes to bpy.data during addon registration; the
    # group is created lazily on first Setup run, or via the panel's
    # "Create Taper Curves" recovery button.


def unregister():
    del bpy.types.Scene.arantools_tree_branch
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
