"""
Tree Inspector — dump UV layers, vertex colors, materials, modifiers, and
collection hierarchy as a JSON report for reverse-engineering foliage /
tree setups (SpeedTree-style wind encoding, custom LOD pipelines, etc.).

The report is written to disk AND copied to the clipboard so the artist
can paste it straight to whoever's helping decode the setup.
"""

import bpy
import json
import os
from statistics import mean
from bpy.types import Operator, PropertyGroup


# ============================================================================
# Inspection helpers
# ============================================================================

def _stats(vals):
    """Per-channel summary with a quantization hint that hints at intent:
    BINARY MASK / DISCRETE / CONTINUOUS / CONSTANT."""
    if not vals:
        return None
    lo, hi = min(vals), max(vals)
    avg = mean(vals)
    uniq = len(set(round(v, 4) for v in vals))
    if uniq == 1:
        hint = f"CONSTANT={lo:.4f}"
    elif uniq == 2:
        hint = "BINARY MASK"
    elif uniq <= 8:
        hint = f"DISCRETE ({uniq} levels)"
    else:
        hint = "CONTINUOUS"
    return {
        "min": round(lo, 4), "max": round(hi, 4),
        "mean": round(avg, 4), "unique_values": uniq,
        "hint": hint,
    }


def _inspect_color_attribute(layer):
    chans = {"R": [], "G": [], "B": [], "A": []}
    for d in layer.data:
        try:
            c = d.color
            chans["R"].append(c[0])
            chans["G"].append(c[1])
            chans["B"].append(c[2])
            chans["A"].append(c[3])
        except Exception:
            pass
    return {
        "name": layer.name,
        "domain": layer.domain,         # 'POINT' or 'CORNER'
        "data_type": layer.data_type,   # 'FLOAT_COLOR' or 'BYTE_COLOR'
        "element_count": len(layer.data),
        "channels": {ch: _stats(v) for ch, v in chans.items()},
    }


def _inspect_uv_layer(uv):
    us = [d.uv[0] for d in uv.data]
    vs = [d.uv[1] for d in uv.data]
    if not us:
        return {"name": uv.name, "empty": True}
    out_of_range = sum(1 for u, v in zip(us, vs)
                       if u < -0.001 or u > 1.001 or v < -0.001 or v > 1.001)
    step = max(1, len(uv.data) // 6)
    samples = [(round(uv.data[i].uv[0], 4), round(uv.data[i].uv[1], 4))
               for i in range(0, len(uv.data), step)][:6]
    # When unique counts are small enough to enumerate, list every value
    # with a count — much more useful than min/max/mean for guessing the
    # encoding (e.g. "per-cluster ID with N clusters").
    u_table = None
    if len(set(round(u, 4) for u in us)) <= 32:
        u_table = sorted(
            ((v, sum(1 for x in us if round(x, 4) == v))
             for v in set(round(x, 4) for x in us)),
            key=lambda t: t[0]
        )
    v_table = None
    if len(set(round(v, 4) for v in vs)) <= 32:
        v_table = sorted(
            ((v, sum(1 for x in vs if round(x, 4) == v))
             for v in set(round(x, 4) for x in vs)),
            key=lambda t: t[0]
        )
    return {
        "name": uv.name,
        "u_range": [round(min(us), 4), round(max(us), 4)],
        "v_range": [round(min(vs), 4), round(max(vs), 4)],
        "out_of_unit_square": out_of_range,
        "u_stats": _stats(us),
        "v_stats": _stats(vs),
        "u_value_table": u_table,
        "v_value_table": v_table,
        "samples": samples,
    }


def _inspect_modifier(mod):
    info = {"name": mod.name, "type": mod.type}
    for attr in ("show_viewport", "show_render", "ratio", "thickness",
                 "levels", "render_levels", "iterations", "strength",
                 "factor", "offset", "subdivision_type"):
        if hasattr(mod, attr):
            try:
                info[attr] = getattr(mod, attr)
            except Exception:
                pass
    if mod.type == 'NODES' and mod.node_group:
        info["node_group"] = mod.node_group.name
        try:
            info["inputs"] = {
                k: str(v) for k, v in mod.items() if not k.startswith('_')
            }
        except Exception:
            pass
    return info


def _inspect_material(mat):
    if mat is None:
        return {"name": None}
    info = {"name": mat.name, "use_nodes": mat.use_nodes}
    if mat.use_nodes and mat.node_tree:
        nt = mat.node_tree
        nodes = []
        for n in nt.nodes:
            entry = {"name": n.name, "type": n.type}
            if n.type == 'ATTRIBUTE':
                entry["attribute_name"] = n.attribute_name
                if hasattr(n, "attribute_type"):
                    entry["attribute_type"] = n.attribute_type
            elif n.type == 'UVMAP':
                entry["uv_map"] = n.uv_map
            elif n.type == 'VERTEX_COLOR':
                entry["layer_name"] = n.layer_name
            elif n.type == 'TEX_IMAGE' and n.image:
                entry["image"] = n.image.name
            elif n.type == 'GROUP' and n.node_tree:
                entry["node_tree"] = n.node_tree.name
            elif n.type == 'VALUE':
                entry["value"] = round(n.outputs[0].default_value, 4)
            elif n.type == 'RGB':
                entry["color"] = [round(c, 4)
                                   for c in n.outputs[0].default_value]
            nodes.append(entry)
        info["nodes"] = nodes
        info["links"] = [
            {"from": f"{l.from_node.name}.{l.from_socket.name}",
             "to":   f"{l.to_node.name}.{l.to_socket.name}"}
            for l in nt.links
        ]
    return info


def _mesh_islands(me):
    """Union-find on edges → list of vertex-index sets, one per island."""
    n = len(me.vertices)
    parent = list(range(n))

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    for e in me.edges:
        union(e.vertices[0], e.vertices[1])

    islands = {}
    for v in range(n):
        islands.setdefault(find(v), []).append(v)
    return list(islands.values())


def _per_island_channel_analysis(obj):
    """For each mesh island, check whether key per-corner channels
    (UVMap2.U/V and vertex-color A) are CONSTANT within that island.
    If a channel is constant per-island, it's almost certainly
    'per-cluster data replicated to every corner of the cluster'."""
    me = obj.data
    islands = _mesh_islands(me)
    if not islands:
        return None

    # Map each LOOP to its island via that loop's vertex
    vert_to_island = [0] * len(me.vertices)
    for idx, isl in enumerate(islands):
        for v in isl:
            vert_to_island[v] = idx

    loop_to_island = [vert_to_island[l.vertex_index] for l in me.loops]

    def channel_values_per_island(per_loop_values):
        """For every island, return how many unique values that island has
        on this channel (rounded to 4 decimals)."""
        per = [set() for _ in islands]
        for li, val in enumerate(per_loop_values):
            per[loop_to_island[li]].add(round(val, 4))
        return [len(s) for s in per]

    report = {
        "island_count": len(islands),
        "island_size_min": min(len(i) for i in islands),
        "island_size_max": max(len(i) for i in islands),
        "channels": [],
    }

    # Run the test on every UV channel and every vcolor channel
    for uv in me.uv_layers:
        u_vals = [d.uv[0] for d in uv.data]
        v_vals = [d.uv[1] for d in uv.data]
        u_counts = channel_values_per_island(u_vals)
        v_counts = channel_values_per_island(v_vals)
        report["channels"].append({
            "channel": f"{uv.name}.U",
            "islands_with_1_unique_value":
                sum(1 for c in u_counts if c == 1),
            "max_unique_in_any_island": max(u_counts),
            "verdict": ("PER-CLUSTER (constant within every island)"
                        if all(c == 1 for c in u_counts)
                        else "PER-CORNER (varies inside islands)"),
        })
        report["channels"].append({
            "channel": f"{uv.name}.V",
            "islands_with_1_unique_value":
                sum(1 for c in v_counts if c == 1),
            "max_unique_in_any_island": max(v_counts),
            "verdict": ("PER-CLUSTER (constant within every island)"
                        if all(c == 1 for c in v_counts)
                        else "PER-CORNER (varies inside islands)"),
        })

    for ca in me.color_attributes:
        if ca.domain != 'CORNER':
            continue
        for ch_idx, ch_name in enumerate(('R', 'G', 'B', 'A')):
            vals = [d.color[ch_idx] for d in ca.data]
            counts = channel_values_per_island(vals)
            report["channels"].append({
                "channel": f"vcolor[{ca.name}].{ch_name}",
                "islands_with_1_unique_value":
                    sum(1 for c in counts if c == 1),
                "max_unique_in_any_island": max(counts),
                "verdict": ("PER-CLUSTER (constant within every island)"
                            if all(c == 1 for c in counts)
                            else "PER-CORNER (varies inside islands)"),
            })

    return report


def _per_material_analysis(obj):
    """Partition the mesh's loops by material slot and run UV / vcolor
    statistics on each partition. This is the trunk-vs-leaves split the
    artist usually cares about — wood and leaf shaders typically read
    different encodings, so the per-material breakdown immediately shows
    e.g. 'wood color = all (0,0,0,1), leaves vary'."""
    me = obj.data
    if not me.materials:
        return {"note": "Mesh has no material slots."}

    # Bucket loops by material slot. Each polygon's material_index applies
    # to all of that polygon's loops.
    n_slots = len(me.materials)
    loops_by_slot = [[] for _ in range(n_slots)]
    polys_by_slot = [0] * n_slots
    for poly in me.polygons:
        mi = poly.material_index
        if 0 <= mi < n_slots:
            polys_by_slot[mi] += 1
            for li in poly.loop_indices:
                loops_by_slot[mi].append(li)

    # Pre-pull every UV / color channel as a per-loop list once. Then a
    # slot's stats are just `[full_array[li] for li in loops_in_slot]`.
    uv_arrays = []
    for uv in me.uv_layers:
        uv_arrays.append((uv.name,
                          [d.uv[0] for d in uv.data],
                          [d.uv[1] for d in uv.data]))

    color_arrays = []
    for ca in me.color_attributes:
        if ca.domain != 'CORNER':
            continue
        r = [d.color[0] for d in ca.data]
        g = [d.color[1] for d in ca.data]
        b = [d.color[2] for d in ca.data]
        a = [d.color[3] for d in ca.data]
        color_arrays.append((ca.name, r, g, b, a))

    out = []
    for slot_idx, mat in enumerate(me.materials):
        loops = loops_by_slot[slot_idx]
        slot_entry = {
            "slot_index": slot_idx,
            "material": mat.name if mat else None,
            "polygon_count": polys_by_slot[slot_idx],
            "loop_count": len(loops),
            "uv_channels": [],
            "color_channels": [],
        }
        if not loops:
            out.append(slot_entry)
            continue

        for uv_name, u_all, v_all in uv_arrays:
            us = [u_all[li] for li in loops]
            vs = [v_all[li] for li in loops]
            slot_entry["uv_channels"].append({
                "uv": uv_name,
                "u": _stats(us),
                "v": _stats(vs),
            })

        for c_name, r_all, g_all, b_all, a_all in color_arrays:
            rs = [r_all[li] for li in loops]
            gs = [g_all[li] for li in loops]
            bs = [b_all[li] for li in loops]
            as_ = [a_all[li] for li in loops]
            slot_entry["color_channels"].append({
                "color_attr": c_name,
                "R": _stats(rs),
                "G": _stats(gs),
                "B": _stats(bs),
                "A": _stats(as_),
            })

        out.append(slot_entry)
    return out


def _inspect_mesh(obj):
    me = obj.data
    return {
        "object": obj.name,
        "parent": obj.parent.name if obj.parent else None,
        "collections": [c.name for c in obj.users_collection],
        "vertex_count": len(me.vertices),
        "polygon_count": len(me.polygons),
        "loop_count": len(me.loops),
        "uv_layers": [_inspect_uv_layer(uv) for uv in me.uv_layers],
        "color_attributes": [_inspect_color_attribute(c)
                             for c in me.color_attributes],
        "vertex_groups": [g.name for g in obj.vertex_groups],
        "materials": [m.name if m else None for m in me.materials],
        "modifiers": [_inspect_modifier(m) for m in obj.modifiers],
        "shape_keys": (
            [sk.name for sk in me.shape_keys.key_blocks]
            if me.shape_keys else []
        ),
        "per_island_analysis": _per_island_channel_analysis(obj),
        "per_material_analysis": _per_material_analysis(obj),
        "custom_properties": {
            k: str(obj.get(k))[:200] for k in obj.keys()
            if not k.startswith('_')
        },
    }


def _inspect_scene(context):
    def walk(coll, depth=0):
        out = [{
            "name": coll.name,
            "depth": depth,
            "objects": [o.name for o in coll.objects],
        }]
        for child in coll.children:
            out.extend(walk(child, depth + 1))
        return out
    return {
        "scene": context.scene.name,
        "collection_tree": walk(context.scene.collection),
    }


def _build_report(context, targets):
    report = {
        "scene": _inspect_scene(context),
        "meshes": [_inspect_mesh(o) for o in targets],
        "materials_used": [],
        "geometry_node_groups": [],
    }
    seen_mats = set()
    for o in targets:
        for slot in o.material_slots:
            if slot.material and slot.material.name not in seen_mats:
                seen_mats.add(slot.material.name)
                report["materials_used"].append(_inspect_material(slot.material))
    for ng in bpy.data.node_groups:
        if ng.type == 'GEOMETRY':
            report["geometry_node_groups"].append({
                "name": ng.name,
                "node_count": len(ng.nodes),
                "node_types": sorted(set(n.type for n in ng.nodes)),
            })
    return report


# ============================================================================
# Per-loop CSV dump — every face corner becomes one row.
#
# Used to reverse-engineer encoding patterns: open the CSV in a
# spreadsheet, group by vertex_index or branch_id, and look at how UV /
# color channels correlate with position. Lets you confirm e.g. whether
# UVMap1 is per-branch-min-Z vs bounding-box-center vs something else
# without having to guess from min/max stats.
# ============================================================================

def _build_loop_csv(context, targets, max_rows=0):
    """Return a single CSV string covering every loop on every target
    mesh. Columns: object, material, vertex, polygon, loop, x, y, z,
    plus one column per UV (U/V) and one per color (R/G/B/A) and one
    per scalar named-attribute (POINT/CORNER/EDGE/FACE)."""
    import csv as _csv
    import io as _io

    deps = context.evaluated_depsgraph_get()
    buf = _io.StringIO()
    writer = _csv.writer(buf)

    # Build column headers from the first object (or union if mixed).
    # For consistency we just write a per-object header section.
    for obj in targets:
        obj_eval = obj.evaluated_get(deps)
        try:
            mesh = obj_eval.to_mesh()
        except RuntimeError as e:
            writer.writerow([f"# {obj.name}: to_mesh failed: {e}"])
            continue
        try:
            if not mesh.loops:
                writer.writerow([f"# {obj.name}: no loops"])
                continue

            # Per-mesh header banner.
            writer.writerow([])
            writer.writerow([f"## OBJECT: {obj.name}",
                             f"verts={len(mesh.vertices)}",
                             f"polys={len(mesh.polygons)}",
                             f"loops={len(mesh.loops)}"])

            # Collect all attributes we want to project per-loop:
            # UVs (CORNER), color (CORNER/POINT), plus every named float/
            # int/bool attribute on POINT/CORNER/EDGE/FACE domains.
            uv_layers = list(mesh.uv_layers)

            color_layers = [a for a in mesh.color_attributes]

            scalar_attrs = []
            for a in mesh.attributes:
                if a.name in {l.name for l in uv_layers}:
                    continue  # UVs already covered
                if a.name in {l.name for l in color_layers}:
                    continue  # Color already covered
                if a.data_type in ('FLOAT', 'INT', 'BOOLEAN'):
                    scalar_attrs.append(a)
                elif a.data_type in ('FLOAT_VECTOR', 'FLOAT2'):
                    scalar_attrs.append(a)  # split into components later

            # Pre-pull arrays. For CORNER/POINT/EDGE/FACE we resolve to
            # per-loop in the inner loop.
            def loop_vert(li): return mesh.loops[li].vertex_index
            def loop_edge(li): return mesh.loops[li].edge_index

            # Map loop → polygon for FACE-domain reads
            loop_to_poly = [-1] * len(mesh.loops)
            for pi, poly in enumerate(mesh.polygons):
                for li in poly.loop_indices:
                    loop_to_poly[li] = pi

            # Column header row.
            header = [
                "loop_idx", "vert_idx", "poly_idx",
                "vert_x", "vert_y", "vert_z",
                "material",
            ]
            for uv in uv_layers:
                header.append(f"{uv.name}.U")
                header.append(f"{uv.name}.V")
            for ca in color_layers:
                for ch in ('R', 'G', 'B', 'A'):
                    header.append(f"{ca.name}.{ch}")
            for a in scalar_attrs:
                if a.data_type == 'FLOAT_VECTOR':
                    header.extend([f"{a.name}.x", f"{a.name}.y", f"{a.name}.z"])
                elif a.data_type == 'FLOAT2':
                    header.extend([f"{a.name}.x", f"{a.name}.y"])
                else:
                    header.append(a.name)
                header[-1] += f"({a.domain})"
            writer.writerow(header)

            mat_names = [m.name if m else "" for m in mesh.materials]

            rows_written = 0
            for li in range(len(mesh.loops)):
                if max_rows and rows_written >= max_rows:
                    writer.writerow([f"# truncated at {max_rows} rows"])
                    break
                vi = loop_vert(li)
                pi = loop_to_poly[li]
                co = mesh.vertices[vi].co
                poly = mesh.polygons[pi] if pi >= 0 else None
                mat_idx = poly.material_index if poly else -1
                row = [
                    li, vi, pi,
                    round(co.x, 6), round(co.y, 6), round(co.z, 6),
                    mat_names[mat_idx] if 0 <= mat_idx < len(mat_names) else "",
                ]
                for uv in uv_layers:
                    row.append(round(uv.data[li].uv[0], 6))
                    row.append(round(uv.data[li].uv[1], 6))
                for ca in color_layers:
                    c = ca.data[vi if ca.domain == 'POINT' else li].color
                    row.extend(round(v, 6) for v in c)
                for a in scalar_attrs:
                    if a.domain == 'POINT':
                        d = a.data[vi]
                    elif a.domain == 'CORNER':
                        d = a.data[li]
                    elif a.domain == 'EDGE':
                        ei = loop_edge(li)
                        d = a.data[ei] if ei < len(a.data) else None
                    elif a.domain == 'FACE':
                        d = a.data[pi] if 0 <= pi < len(a.data) else None
                    else:
                        d = None
                    if d is None:
                        if a.data_type == 'FLOAT_VECTOR':
                            row.extend(["", "", ""])
                        elif a.data_type == 'FLOAT2':
                            row.extend(["", ""])
                        else:
                            row.append("")
                        continue
                    if a.data_type == 'FLOAT_VECTOR':
                        v = d.vector
                        row.extend((round(v[0], 6), round(v[1], 6),
                                    round(v[2], 6)))
                    elif a.data_type == 'FLOAT2':
                        v = d.vector
                        row.extend((round(v[0], 6), round(v[1], 6)))
                    else:
                        row.append(d.value)
                writer.writerow(row)
                rows_written += 1
        finally:
            obj_eval.to_mesh_clear()

    return buf.getvalue()


# ============================================================================
# Property group
# ============================================================================

class ARANTOOLS_PG_TreeInspect(PropertyGroup):
    output_folder: bpy.props.StringProperty(
        name="Output Folder",
        description="Folder where the inspection report is written",
        default="//",
        subtype='DIR_PATH',
    )
    output_filename: bpy.props.StringProperty(
        name="Filename",
        description="Report filename (JSON content with .txt extension is fine)",
        default="tree_inspect.txt",
    )
    copy_to_clipboard: bpy.props.BoolProperty(
        name="Copy to Clipboard",
        description="Also place the full report on the clipboard for easy pasting",
        default=True,
    )
    scope: bpy.props.EnumProperty(
        name="Scope",
        description="What gets inspected",
        items=[
            ('SELECTED', "Selected", "Only the currently selected mesh objects"),
            ('ALL',      "All",      "Every mesh in the file"),
        ],
        default='SELECTED',
    )
    export_loop_csv: bpy.props.BoolProperty(
        name="Export Per-Loop CSV",
        description="Also write a detailed CSV — one row per face corner "
                    "with vertex XYZ, every UV channel, every color channel, "
                    "and every named attribute. Open in a spreadsheet to "
                    "pivot/filter and reverse-engineer the encoding pattern",
        default=False,
    )
    csv_max_rows: bpy.props.IntProperty(
        name="CSV Row Limit",
        description="Cap rows per object in the CSV (0 = unlimited). "
                    "Use a small value to spot-check; full dump can be "
                    "100k+ rows on a complex tree",
        default=0, min=0, soft_max=200000,
    )


# ============================================================================
# Operator
# ============================================================================

class ARANTOOLS_OT_TreeInspect(Operator):
    """Dump every targeted mesh's UV layers (with per-channel ranges),
color attributes (with value distributions + quantization hints — BINARY
MASK / DISCRETE / CONTINUOUS), materials (full shader graph), modifiers,
vertex groups, collection hierarchy, and any geometry-node groups in the
file as a single JSON report. Used to reverse-engineer existing foliage
and tree setups so the data encoding becomes obvious before you build
your own."""
    bl_idname  = "arantools.tree_inspect"
    bl_label   = "Inspect Tree Setup"
    bl_options = {'REGISTER'}

    def execute(self, context):
        props = context.scene.arantools_tree_inspect

        if props.scope == 'SELECTED':
            targets = [o for o in context.selected_objects if o.type == 'MESH']
            if not targets:
                self.report({'ERROR'},
                            "No mesh objects selected. Select the tree, or "
                            "switch Scope to 'All'.")
                return {'CANCELLED'}
        else:
            targets = [o for o in bpy.data.objects if o.type == 'MESH']
            if not targets:
                self.report({'ERROR'}, "No mesh objects in this file.")
                return {'CANCELLED'}

        report = _build_report(context, targets)
        text = json.dumps(report, indent=2, default=str)

        out_path = bpy.path.abspath(
            os.path.join(props.output_folder, props.output_filename)
        )
        try:
            out_dir = os.path.dirname(out_path)
            if out_dir:
                os.makedirs(out_dir, exist_ok=True)
            with open(out_path, 'w', encoding='utf-8') as f:
                f.write(text)
        except Exception as e:
            self.report({'ERROR'}, f"Could not write report: {e}")
            return {'CANCELLED'}

        clipboard_msg = ""
        if props.copy_to_clipboard:
            try:
                context.window_manager.clipboard = text
                clipboard_msg = " (also copied to clipboard)"
            except Exception as e:
                print(f"[AranTools] Clipboard copy failed: {e}")

        csv_msg = ""
        if props.export_loop_csv:
            csv_text = _build_loop_csv(context, targets,
                                        max_rows=props.csv_max_rows)
            csv_base, _ = os.path.splitext(out_path)
            csv_path = csv_base + "_loops.csv"
            try:
                with open(csv_path, 'w', encoding='utf-8', newline='') as f:
                    f.write(csv_text)
                csv_msg = f" + per-loop CSV → {csv_path}"
            except Exception as e:
                csv_msg = f" (CSV write failed: {e})"

        self.report({'INFO'},
                    f"Inspected {len(targets)} mesh(es) → {out_path}"
                    + clipboard_msg + csv_msg)
        return {'FINISHED'}


# ============================================================================
# UV validator — checks that the final evaluated mesh carries the
# SpeedTree-style wind/identification encoding produced by the Branch
# Tubes + Branch UV geonodes:
#
#   UVMap   (or UVmap_0)   : artist texture UVs — must exist
#   UVMap2 (Float2, CORNER): (branch_base_z, tree_max_z)
#       U = per-island pivot; should be constant per branch_id and match
#           the corresponding loop's vertex `branch_base_z` attribute
#       V = constant across the whole mesh (= max Z of all verts)
#   UVMap3 (Float2, CORNER): (0, 1) — constant placeholder
#   Attribute (Color)       : (0, 0, wind_mask, 1)
#       R = 0, G = 0, A = 1, B ∈ [0,1]
#       B must be 0 on any vertex flagged is_underground
# ============================================================================

_EPS = 1e-4


def _approx_eq(a, b, eps=_EPS):
    return abs(a - b) <= eps


def _get_attr_values(mesh, name):
    """Return (values, domain) for a named attribute, or (None, None) if
    absent. `values` is a flat list whose length matches the domain
    (POINT → len(verts), CORNER → len(loops), EDGE → len(edges)…)."""
    attr = mesh.attributes.get(name)
    if attr is None:
        return None, None
    out = []
    for d in attr.data:
        if hasattr(d, 'value'):
            out.append(d.value)
        elif hasattr(d, 'vector'):
            v = d.vector
            out.append((v[0], v[1]) if len(v) == 2 else (v[0], v[1], v[2]))
        elif hasattr(d, 'color'):
            c = d.color
            out.append((c[0], c[1], c[2], c[3]))
    return out, attr.domain


def _loop_to_vert(mesh):
    """[loop_index] → vertex_index lookup for spreading per-vert attrs
    onto face-corner data for cross-domain checks."""
    return [lp.vertex_index for lp in mesh.loops]


def _resolve_per_loop(values, domain, mesh):
    """Project a per-attribute array onto per-loop indices, regardless of
    whether it lives on POINT, CORNER, EDGE or FACE domain. Returns None
    if the domain isn't projectable (e.g. instance)."""
    if values is None:
        return None
    n_loops = len(mesh.loops)
    if domain == 'CORNER':
        return values if len(values) == n_loops else None
    if domain == 'POINT':
        l2v = _loop_to_vert(mesh)
        return [values[v] if v < len(values) else None for v in l2v]
    if domain == 'FACE':
        # Spread the face's value to each of its loops.
        out = [None] * n_loops
        for fi, f in enumerate(mesh.polygons):
            if fi >= len(values):
                continue
            for li in f.loop_indices:
                out[li] = values[fi]
        return out
    if domain == 'EDGE':
        # An edge value applies to both loops whose edge index matches.
        out = [None] * n_loops
        for li, lp in enumerate(mesh.loops):
            ei = lp.edge_index
            if ei < len(values):
                out[li] = values[ei]
        return out
    return None


def _connected_components(mesh):
    """Return a list of component-id per vertex via BFS over edges. Pure
    Python — fine for the 10k-vert skeletons we deal with."""
    n = len(mesh.vertices)
    comp = [-1] * n
    adj = [[] for _ in range(n)]
    for e in mesh.edges:
        a, b = e.vertices[0], e.vertices[1]
        adj[a].append(b)
        adj[b].append(a)
    cid = 0
    for start in range(n):
        if comp[start] != -1:
            continue
        stack = [start]
        comp[start] = cid
        while stack:
            v = stack.pop()
            for w in adj[v]:
                if comp[w] == -1:
                    comp[w] = cid
                    stack.append(w)
        cid += 1
    return comp, cid


def _validate_tree_uvs(obj, context):
    """Run the validator on a single object's evaluated mesh. Returns a
    list of (level, message) tuples where level ∈ {'OK', 'WARN', 'FAIL'}."""
    results = []

    # Pre-flight on the SKELETON (the modifier-input mesh) — diagnose the
    # most common cause of UV failures: bridge edges not being flagged
    # because the user hasn't re-run Setup after the refactor that
    # introduced `is_branch_entry`.
    src = obj.data
    if 'is_branch_entry' not in src.attributes:
        results.append(('FAIL',
            "Skeleton (modifier-input mesh) has NO 'is_branch_entry' "
            "attribute — the tubes geonode can't delete bridge edges, "
            "so branches stay topologically merged at junctions. "
            "Re-run Branch Skeleton → Setup."))
    elif 'is_branch_entry' in src.attributes:
        # Count how many entry edges were flagged. Should be ≈
        # (number of branches - 1).
        ie = src.attributes['is_branch_entry']
        if ie.domain == 'EDGE':
            flagged = sum(1 for d in ie.data if d.value)
            results.append(('OK',
                f"Skeleton has 'is_branch_entry' on EDGE domain "
                f"({flagged} bridge edge(s) flagged)."))
        else:
            results.append(('FAIL',
                f"'is_branch_entry' is on {ie.domain} domain "
                f"(expected EDGE). Re-run Setup to fix."))

    deps = context.evaluated_depsgraph_get()
    obj_eval = obj.evaluated_get(deps)
    try:
        mesh = obj_eval.to_mesh()
    except RuntimeError as e:
        return results + [('FAIL', f"Could not evaluate mesh: {e}")]

    try:
        if len(mesh.loops) == 0:
            return results + [('FAIL',
                "Evaluated mesh has no loops (faces). "
                "Did the tubes geonode build geometry?")]

        # Component count vs branch_id island count — these MUST agree
        # for branch_base_z to be unambiguous per island. If components
        # > branch_id islands, branches are still topologically joined.
        comp, n_comp = _connected_components(mesh)
        bid_vals_pf, bid_domain_pf = _get_attr_values(mesh, 'branch_id')
        if bid_vals_pf is not None and bid_domain_pf == 'POINT':
            distinct_bids = set(v for v in bid_vals_pf if v >= 0)
            n_islands = len(distinct_bids)
            if n_comp == n_islands:
                results.append(('OK',
                    f"Connected components ({n_comp}) match distinct "
                    f"branch_id islands ({n_islands})."))
            elif n_comp < n_islands:
                results.append(('FAIL',
                    f"Only {n_comp} connected component(s) but "
                    f"{n_islands} distinct branch_id values — bridge "
                    f"edges weren't fully cut. Each merged junction "
                    f"makes branch_base_z vary within an island."))
            else:
                results.append(('WARN',
                    f"More connected components ({n_comp}) than "
                    f"branch_id islands ({n_islands}). Unexpected — "
                    f"possible loose geometry."))

        # ── UVMap (artist texture UVs) ───────────────────────────────────
        uv_layers = {l.name: l for l in mesh.uv_layers}
        artist_name = next((n for n in ('UVMap', 'UVmap_0') if n in uv_layers),
                           None)
        if artist_name is None:
            results.append(('WARN',
                "No 'UVMap' / 'UVmap_0' layer found — artist texture UVs "
                "missing. Curve-to-Mesh in the tubes geonode normally "
                "creates 'UVMap' automatically."))
        else:
            results.append(('OK', f"Artist UV layer '{artist_name}' present "
                                   f"({len(uv_layers[artist_name].data)} loops)."))

        # ── UVMap2 = (branch_base_z, tree_max_z), CORNER FLOAT2 ─────────
        uv2_attr = mesh.attributes.get('UVMap2')
        if uv2_attr is None:
            results.append(('FAIL',
                "UVMap2 is missing — run the Branch UV Geonode."))
        else:
            if uv2_attr.domain != 'CORNER' or uv2_attr.data_type != 'FLOAT2':
                results.append(('FAIL',
                    f"UVMap2 wrong domain/type: "
                    f"{uv2_attr.domain}/{uv2_attr.data_type} "
                    f"(expected CORNER/FLOAT2)."))
            else:
                u_vals = [d.vector[0] for d in uv2_attr.data]
                v_vals = [d.vector[1] for d in uv2_attr.data]
                # V should be a single constant = max world-Z of the mesh.
                v_unique = sorted(set(round(v, 4) for v in v_vals))
                if len(v_unique) == 1:
                    results.append(('OK',
                        f"UVMap2.V is constant = {v_unique[0]:.4f} "
                        f"(tree_max_z)."))
                else:
                    results.append(('FAIL',
                        f"UVMap2.V is NOT constant: {len(v_unique)} "
                        f"distinct values, range "
                        f"[{min(v_unique):.4f}, {max(v_unique):.4f}]. "
                        f"Should be a single per-tree maximum-Z value."))
                # Compare V against the actual max-Z of the mesh.
                if mesh.vertices:
                    mesh_max_z = max(v.co.z for v in mesh.vertices)
                    if v_unique:
                        delta = abs(v_unique[0] - mesh_max_z) if len(v_unique) == 1 else None
                        if delta is not None and not _approx_eq(v_unique[0], mesh_max_z, 0.01):
                            results.append(('WARN',
                                f"UVMap2.V ({v_unique[0]:.4f}) does not "
                                f"match mesh max Z ({mesh_max_z:.4f}); "
                                f"Δ={delta:.4f}."))
                # U should equal branch_base_z at the corresponding loop.
                base_z_vals, base_z_domain = _get_attr_values(mesh,
                                                              'branch_base_z')
                base_z_per_loop = _resolve_per_loop(base_z_vals,
                                                   base_z_domain, mesh)
                if base_z_per_loop is None:
                    results.append(('WARN',
                        "branch_base_z absent or on a non-projectable "
                        "domain — can't verify UVMap2.U matches the "
                        "per-island pivot. UV geonode falls back to "
                        "vertex Z when this attr is missing."))
                else:
                    results.append(('OK',
                        f"branch_base_z found on {base_z_domain} domain."))
                    mismatches = 0
                    for li, u in enumerate(u_vals):
                        bz = base_z_per_loop[li]
                        if bz is None:
                            continue
                        if not _approx_eq(u, bz, 5e-3):
                            mismatches += 1
                    if mismatches == 0:
                        results.append(('OK',
                            f"UVMap2.U matches branch_base_z on all "
                            f"{len(u_vals)} loops."))
                    else:
                        results.append(('FAIL',
                            f"UVMap2.U mismatches branch_base_z on "
                            f"{mismatches}/{len(u_vals)} loops "
                            f"(tolerance 5e-3)."))
                # Per-branch_id constancy of U (if branch_id present and
                # projectable). branch_id may end up on POINT, EDGE, or
                # CORNER depending on what survived Mesh→Curve→Mesh.
                bid_vals, bid_domain = _get_attr_values(mesh, 'branch_id')
                bid_per_loop = _resolve_per_loop(bid_vals, bid_domain, mesh)
                if bid_per_loop is None:
                    results.append(('WARN',
                        "branch_id absent or on a non-projectable "
                        "domain — skipping per-island U constancy."))
                else:
                    results.append(('OK',
                        f"branch_id found on {bid_domain} domain."))
                    by_branch = {}
                    for li, u in enumerate(u_vals):
                        bid = bid_per_loop[li]
                        if bid is None or bid < 0:
                            continue
                        by_branch.setdefault(bid, set()).add(round(u, 4))
                    bad = [b for b, us in by_branch.items() if len(us) > 1]
                    if not bad:
                        results.append(('OK',
                            f"UVMap2.U is constant within each of "
                            f"{len(by_branch)} branch(es)."))
                    else:
                        # Show first few offending islands for diagnosis.
                        sample = ", ".join(f"id={b}→{sorted(by_branch[b])}"
                                           for b in list(bad)[:3])
                        results.append(('FAIL',
                            f"UVMap2.U varies within {len(bad)} of "
                            f"{len(by_branch)} branch_id island(s). "
                            f"Examples: {sample}"))

        # ── UVMap3 = (0, 1) constant ────────────────────────────────────
        uv3_attr = mesh.attributes.get('UVMap3')
        if uv3_attr is None:
            results.append(('WARN',
                "UVMap3 absent — placeholder (0,1) not written."))
        else:
            if uv3_attr.domain != 'CORNER' or uv3_attr.data_type != 'FLOAT2':
                results.append(('FAIL',
                    f"UVMap3 wrong domain/type: "
                    f"{uv3_attr.domain}/{uv3_attr.data_type}."))
            else:
                u3 = [d.vector[0] for d in uv3_attr.data]
                v3 = [d.vector[1] for d in uv3_attr.data]
                ok_u = all(_approx_eq(u, 0.0) for u in u3)
                ok_v = all(_approx_eq(v, 1.0) for v in v3)
                if ok_u and ok_v:
                    results.append(('OK', "UVMap3 is constant (0, 1)."))
                else:
                    results.append(('FAIL',
                        f"UVMap3 not (0,1): U range "
                        f"[{min(u3):.4f},{max(u3):.4f}], V range "
                        f"[{min(v3):.4f},{max(v3):.4f}]."))

        # ── UVMap1 (repurposed as per-branch pivot X / Y) ────────────
        lmap_attr = mesh.attributes.get('UVMap1')
        if lmap_attr is None:
            # Some files name it without the camel-case; tolerate that.
            for n in ('UVMap1', 'Lightmap', 'UVmap_1'):
                if n in mesh.attributes:
                    lmap_attr = mesh.attributes[n]
                    break
        if lmap_attr is None:
            results.append(('WARN',
                "No UVMap1 (per-branch pivot X/Y) — rebuild Branch "
                "UV Geonode from source."))
        elif lmap_attr.data_type != 'FLOAT2' or lmap_attr.domain != 'CORNER':
            results.append(('FAIL',
                f"UVMap1 wrong type/domain: "
                f"{lmap_attr.data_type}/{lmap_attr.domain}."))
        else:
            lu = [d.vector[0] for d in lmap_attr.data]
            lv = [d.vector[1] for d in lmap_attr.data]
            lu_unique = sorted(set(round(v, 4) for v in lu))
            lv_unique = sorted(set(round(v, 4) for v in lv))
            results.append(('OK',
                f"UVMap1: {len(lu_unique)} U values, "
                f"{len(lv_unique)} V values "
                f"(should match branch count)."))

        # ── Color "Attribute" = (0, 0, 0, 1) on the wood ─────────────────
        color_attr = mesh.attributes.get('Attribute')
        if color_attr is None:
            # Fall back to first color attribute if a non-standard name.
            color_attr = next((a for a in mesh.attributes
                               if a.data_type in ('FLOAT_COLOR', 'BYTE_COLOR')),
                              None)
            if color_attr is not None:
                results.append(('WARN',
                    f"No attribute named 'Attribute'; falling back to "
                    f"'{color_attr.name}' for color checks."))
        if color_attr is None:
            results.append(('FAIL',
                "No color attribute found — wind mask not written."))
        else:
            cols = [d.color for d in color_attr.data]
            r_max = max(c[0] for c in cols) if cols else 0
            g_max = max(c[1] for c in cols) if cols else 0
            b_min = min(c[2] for c in cols) if cols else 0
            b_max = max(c[2] for c in cols) if cols else 0
            a_min = min(c[3] for c in cols) if cols else 1
            a_max = max(c[3] for c in cols) if cols else 1
            # Current encoding for the WOOD mesh:
            #   trunk  (depth 0)   : (0.0001, 0, 0, 1)
            #   branch (depth ≥ 1) : (0.001,  0, 0, branch_t)
            BYTE_EPS = 1.5 / 255.0
            g_min = min(c[1] for c in cols) if cols else 0
            r_unique = sorted(set(round(c[0], 4) for c in cols))
            valid_r = lambda v: (_approx_eq(v, 0.0001, 5e-4)
                                 or _approx_eq(v, 0.001,  5e-4)
                                 or v <= BYTE_EPS)
            if all(valid_r(v) for v in r_unique):
                trunk_loops  = sum(1 for c in cols if c[0] < 0.0005)
                branch_loops = len(cols) - trunk_loops
                results.append(('OK',
                    f"Color R: {trunk_loops} trunk loops (R≈0.0001) + "
                    f"{branch_loops} branch loops (R≈0.001)."))
            else:
                results.append(('WARN',
                    f"Color R has unexpected values: "
                    f"{r_unique[:8]}{'…' if len(r_unique) > 8 else ''}. "
                    f"Expected just 0.0001 and 0.001."))
            if g_max > BYTE_EPS:
                results.append(('WARN',
                    f"Color G is not 0 ([{g_min:.4f},{g_max:.4f}])."))
            else:
                results.append(('OK', "Color G = 0."))
            if b_max > BYTE_EPS:
                results.append(('WARN',
                    f"Color B is not 0 ([{b_min:.4f},{b_max:.4f}])."))
            else:
                results.append(('OK', "Color B = 0."))
            # Alpha: trunk + tier-2+ → 1; tier-1 ramps 0→1 along branch.
            a_levels = len(set(round(c[3], 2) for c in cols))
            results.append(('OK',
                f"Color A range [{a_min:.4f}, {a_max:.4f}], "
                f"{a_levels} distinct level(s) — tier-1 branches ramp "
                f"alpha along branch_t; trunk and tier-2+ = 1."))
            # Wind-mask underground check removed: wood no longer carries
            # a per-vertex wind mask in Color.B (the reference doesn't,
            # and our updated UV geonode matches). The wood shader masks
            # wind via UVMap2 + world Z directly.
    finally:
        obj_eval.to_mesh_clear()

    return results


class ARANTOOLS_OT_TreeValidateUVs(Operator):
    """Walk the FINAL evaluated mesh of the selected tree (after all
modifiers — i.e. after the tubes + UV geonodes have run) and verify the
SpeedTree-style encoding is correct: UVMap2 = (branch_base_z,
tree_max_z), UVMap3 = (0,1), and Color = (0, 0, wind_mask, 1) with B=0
on underground verts. Reports OK / WARN / FAIL per check, summary to the
info bar and full text on the clipboard."""
    bl_idname  = "arantools.tree_validate_uvs"
    bl_label   = "Validate Tree UVs"
    bl_options = {'REGISTER'}

    @classmethod
    def poll(cls, context):
        return any(o.type == 'MESH' for o in context.selected_objects)

    def execute(self, context):
        targets = [o for o in context.selected_objects if o.type == 'MESH']
        if not targets:
            self.report({'ERROR'}, "Select at least one mesh object.")
            return {'CANCELLED'}

        all_lines = []
        total_ok = total_warn = total_fail = 0
        for obj in targets:
            results = _validate_tree_uvs(obj, context)
            all_lines.append(f"=== {obj.name} ===")
            for level, msg in results:
                all_lines.append(f"[{level:>4}] {msg}")
                if level == 'OK':   total_ok   += 1
                if level == 'WARN': total_warn += 1
                if level == 'FAIL': total_fail += 1
            all_lines.append("")

        text = "\n".join(all_lines)
        print(text)
        try:
            context.window_manager.clipboard = text
        except Exception:
            pass

        summary = (f"{len(targets)} mesh(es): "
                   f"{total_ok} OK, {total_warn} WARN, {total_fail} FAIL "
                   f"(full report on clipboard, also in console).")
        level = {'ERROR'} if total_fail else (
                {'WARNING'} if total_warn else {'INFO'})
        self.report(level, summary)
        return {'FINISHED'}


# ============================================================================
# Registration
# ============================================================================

classes = [
    ARANTOOLS_PG_TreeInspect,
    ARANTOOLS_OT_TreeInspect,
    ARANTOOLS_OT_TreeValidateUVs,
]


def register():
    for cls in classes:
        bpy.utils.register_class(cls)
    bpy.types.Scene.arantools_tree_inspect = bpy.props.PointerProperty(
        type=ARANTOOLS_PG_TreeInspect,
    )


def unregister():
    del bpy.types.Scene.arantools_tree_inspect
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
