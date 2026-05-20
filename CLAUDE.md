# Tree Tools — Architecture Notes

This document describes the tree-authoring pipeline in `arantools`. It's
the reference for anyone (human or AI) extending or debugging the tree
tools. The non-tree tools in the addon are unrelated to this doc.

## Pipeline overview

```
   author skeleton            sweep tubes              bake identification
   (vertex+edge mesh)         (geonode modifier)       UVs / vertex color
   ┌──────────────┐   Setup   ┌──────────────────┐    ┌──────────────────┐
   │ Branch       │  Skeleton │ Branch Tubes     │    │ Branch UV        │  →  wood
   │ Skeleton     │  ───────→ │ Geonode          │ →  │ Geonode          │     mesh
   └──────────────┘           └──────────────────┘    └──────────────────┘
                                                           │
                                  ┌───── (leaves geonode samples nearest
                                  │       trunk tip on the wood mesh)
                                  ↓
                              ┌──────────────────┐
                              │ Leaves UV        │  →  leaves mesh
                              │ Geonode          │
                              └──────────────────┘
```

The artist authors a "stick figure" of branches as a vertex+edge mesh
(no faces). The Branch Skeleton tool walks that mesh from a designated
root vertex and stamps per-branch attributes. The Tubes geonode sweeps
a circular profile along each branch chain to make the actual wood
geometry. The Branch UV geonode bakes the identification UVs and vertex
color encoding that the SpeedTree-style Unreal master material reads.
Leaves are a separate mesh (their own generator) and get their own UV
geonode that samples the nearest trunk tip for pivot data.

## Modules (tree-related)

| File | Purpose |
|---|---|
| [tree_branch.py](tree_branch.py) | Skeleton authoring + per-branch attribute stamping. Owns the Setup operator, the radius / taper-curve PropertyGroup, root vertex management. |
| [tree_tubes_geonode.py](tree_tubes_geonode.py) | Builds the Mesh→Curve→Mesh tube pipeline. Deletes "bridge" edges so each branch becomes its own connected component before Curve to Mesh. |
| [tree_uv_geonode.py](tree_uv_geonode.py) | Both the wood UV/color geonode and the leaves UV/color geonode. |
| [tree_inspect.py](tree_inspect.py) | JSON-dump report builder + the "Validate Tree UVs" operator that checks the final evaluated mesh matches the expected encoding. |

## Skeleton attribute spec (what Setup writes)

| Attribute | Type | Domain | Meaning |
|---|---|---|---|
| `branch_id` | INT | EDGE | Unique per branch; each edge belongs to exactly one branch — no junction-vertex ambiguity. |
| `branch_depth` | INT | EDGE | 0 = trunk, 1 = primary, 2 = secondary, etc. |
| `is_branch_entry` | BOOL | EDGE | True on each "bridge" edge — the single edge connecting a child branch's first vertex to its junction in the parent. The Tubes geonode deletes these to disconnect branches. |
| `branch_t` | FLOAT | POINT | 0 at branch base → 1 at branch tip. Drives alpha ramp in the wood color encoding. |
| `radius` | FLOAT | POINT | Per-vertex radius. Inherited at junctions: each child branch starts at the parent's radius at the junction vertex; the category's taper curve scales it along the branch. |
| `tilt` | FLOAT | POINT | Curve tilt. Currently always 0 unless the user manually edits. |
| `is_root` | BOOL | POINT | True on the single designated trunk-base vertex. |
| `branch_base_x` | FLOAT | POINT | X of the branch's lowest-Z vertex (per-branch pivot). Mesh-local. |
| `branch_base_y` | FLOAT | POINT | Y of the branch's lowest-Z vertex. Mesh-local. |
| `branch_base_z` | FLOAT | POINT | Z of the branch's lowest-Z vertex. Mesh-local. |
| `branch_top_z` | FLOAT | POINT | Max-Z of the branch (used by some downstream geonodes). |
| `is_underground` | BOOL | POINT | True if vertex Z is below the root vertex's Z (root-side geometry). |

Why split categorical attrs onto EDGE and smoothly-varying ones onto POINT:
Each edge unambiguously belongs to ONE branch, so `branch_id` on EDGE has
no junction-vertex ambiguity. Smoothly-varying attrs (`radius`, `branch_t`,
the pivots) need to interpolate or survive Mesh→Curve→Mesh, so they go on
POINT.

## Root vertex (sticky)

The root vertex (trunk base) is stored as a mesh custom property
`mesh["arantools_root_vert"]` — a single integer index. Setup reads only
this; it never looks at the current selection. Designate via "Set Root
From Selection"; clear via the X button. The Select Root button
selects-and-frames the stored vertex from anywhere.

This was a deliberate UX decision after an earlier iteration that read
the selected vertex — that forced the artist to re-select before every
re-bake, which is annoying once the skeleton is large.

## Taper curves (radius shaping)

Three Float Curves live in a hidden ShaderNodeTree called
`AranTools_TreeBranchCurves`, with three Float Curve nodes named
`trunk_taper`, `branch_taper`, `root_taper`. The N-panel exposes them
via `template_curve_mapping` so the artist can edit each curve directly
in the panel. X axis = 0 base → 1 tip; Y axis = radius multiplier
applied on top of the per-branch starting radius.

The node group is created lazily on first Setup run (or via the
"Create Taper Curves" recovery button if the user opened a .blend before
the group existed). It is NOT created during `register()` because
Blender forbids `bpy.data` writes during addon registration.

Branch radius rule:
- Trunk starts at `props.base_radius`.
- Every child branch starts at the parent's radius at the junction
  vertex (looked up in the already-filled `radius` array — branches are
  processed parent-first via the DFS order from `_build_branches`).
- Along each branch, radius = `start_r * curve(t)` where `curve` is the
  category's Float Curve evaluated at `t = i / (count - 1)`.
- Per-vertex category override: any vertex with `Z < root_z` uses the
  Root curve regardless of which branch it lives in — so a trunk that
  dips underground naturally gets the root profile down there.

## Tubes geonode

Pipeline inside `AranTools_TreeBranchTubes`:

1. Read `is_branch_entry` (EDGE BOOL). Delete those edges. The mesh
   becomes N disconnected per-branch chains.
2. Mesh to Curve → N curves, one per branch. Point attributes survive
   (radius, branch_t, branch_base_z, etc.).
3. Stamp `is_start = True` on each curve's first point via Endpoint
   Selection. This is used later to identify cap N-gons.
4. Set Curve Radius from `radius` (× Radius Multiplier input).
5. Curve to Mesh with Fill Caps = True and a profile circle.
6. Capture `is_start` as a FLOAT on FACE domain — averages the boolean
   across each face's corners. Base cap N-gons (all corners on the start
   point) get avg = 1.0; sidewall quads get 0.5; tip caps get 0.0.
7. Delete faces where the captured avg > 0.99 → base caps removed,
   leaving open bases (ready to dock onto the parent branch) and closed
   tips.

The bridge-edge delete (step 1) is critical — if it doesn't run (e.g.
`is_branch_entry` is missing because Setup wasn't re-run after the
attribute was introduced), branches stay connected at junctions. Mesh
to Curve then splits ambiguously and the resulting tubes have leaked
attributes from the parent branch on their first cross-section.

## Wood UV / color encoding

What the Branch UV geonode writes onto the wood tube mesh (all
`FACE_CORNER` domain):

| Attribute | Value |
|---|---|
| `UVMap2` (Float2) | `(branch_base_z, tree_max_z)` |
| `UVMap3` (Float2) | `(0, 1)` constant placeholder |
| `UVMap1` (Float2) | `(branch_base_x, branch_base_y)` — per-branch pivot X / Y in mesh-local meters. The Unreal pivot MF reconstructs the world pivot as `(UVMap1.R, UVMap1.G - 1, UVMap2.R) * 100`, then `TransformPosition` to absolute world space. (We pass Y straight, not `Y + 1`.) |
| `Attribute` (Color) | See table below |

Wood color is depth-conditional, matching the artist's measured spec:

| Layer | R | G | B | A |
|---|---|---|---|---|
| Trunk (depth 0) | 0.0001 | 0 | 0 | 1 |
| Any branch (depth ≥ 1) | 0.001 | 0 | 0 | `branch_t` (0 at base → 1 at tip) |

Why the tiny non-zero values: the reference asset's wood R isn't exactly
0 either — it's a byte-quantized very-small float. The Unreal
`MF_VertexColorID.R` multiplies by 255 and rounds, so anything < 0.5/255
becomes ID 0. We match the reference's "tiny but distinct" values so
when both meshes go through MF_VertexColorID, trunk and branches map to
different IDs (rounded down to 0 for trunk, ~0.255 → 0 for 0.001 too,
but they're distinguishable in float form for any custom shader paths).

Alpha-at-junctions: each child branch starts at α=0 at its first vertex
(right where it sprouts from the parent) and ramps to α=1 at its tip.
So every "blend point" gets a dark band in the alpha channel.

**Implementation note**: Blender 4.5's `FunctionNodeCombineColor` (RGB
mode) has no Alpha input socket. To author per-element alpha we use
`ShaderNodeMix` in RGBA mode and set the A/B socket `default_value` as
full RGBA tuples — that gives us static colors with explicit alpha
authoring, which we then lerp between via the per-element `branch_t`.

## Leaves UV / color encoding

The Leaves UV geonode runs on the LEAF mesh (a separate object from the
wood). Each leaf vertex samples the **closest tip on a referenced trunk
object** (verts where `branch_t ≥ 0.99` on the post-tubes wood mesh) and
inherits that tip's pivot. So a leaf sways with its parent branch.

Modifier inputs:
- `Trunk` (Object pointer) — point at the wood mesh.
- `Random Blue Seed` (Int) — seed for the per-face random in Color.B.

Outputs (all CORNER):

| Attribute | Value |
|---|---|
| `UVMap2` | `(tip.branch_base_z, tree_max_z)` |
| `UVMap3` | `(0, 1)` |
| `UVMap1` | `(tip.branch_base_x, tip.branch_base_y)` |
| `Attribute` | `(0.001, 0.001, random², 1)` — see B note below |

B channel:
```
Face of Corner.Face Index → Random Value (Float, 0..1) → Math(power=2) → Mix.Factor
                                                                         (Color)
Mix.A = (0.001, 0.001, 0, 1)
Mix.B = (0.001, 0.001, 1, 1)
```
The Random ID is the *face* index (not vertex/corner index), so every
corner of a face shares one B value. The `^2` bias compresses most
leaves toward low B (gentle flutter) with a few outliers near 1.

## Validator

`tree_inspect.py` provides:
1. **Tree Inspector → Inspect & Dump Report** — full JSON dump of the
   targeted meshes (UV layers, color channels, materials, modifiers,
   per-island analysis, per-material analysis). Used to reverse-engineer
   the reference asset.
2. **UV Encoding Validator** — runs on the final evaluated mesh (post
   modifiers) of the selected object. Reports OK / WARN / FAIL per check
   to the info bar and copies a full text report to the clipboard.

Key validator checks:
- Skeleton has `is_branch_entry` flagged (catches "didn't re-run Setup"
  after the attribute was introduced).
- `UVMap2.V` is constant and equals mesh max-Z.
- `UVMap2.U` matches `branch_base_z` per loop.
- `UVMap2.U` is constant within each `branch_id` island. **If this
  fails, the cause is almost always bridge edges not being cut** — the
  validator reports connected-component count vs island count to make
  that immediately visible.
- `UVMap3` is `(0, 1)`.
- `UVMap1` exists with the right number of unique values.
- Color R has only the expected values (≈0.0001 for trunk, ≈0.001 for
  branches). Tolerates byte-quantization slack (1.5/255).
- Color A range matches branch_t ramp expectation.

## Common failure modes & debugging

| Symptom | Likely cause | Fix |
|---|---|---|
| `UVMap2.U varies within branch_id islands` (validator) | Bridge edges not deleted. `is_branch_entry` missing or only partially flagged on the skeleton. | Re-run Setup Branch Skeleton on the original vertex+edge mesh (NOT the tube mesh — Setup needs the bare skeleton in Edit Mode). |
| Wood Color.R / Color.A all zero | Old `AranTools_TreeBranchUV` group cached, hasn't been rebuilt since the encoding changed. | Branch UV Geonode → Rebuild Group from Source. |
| Leaves modifier shows no effect | `Trunk` object input on the modifier is unset. | Set it to your wood mesh in the modifier properties. |
| Panel suddenly disappears after an edit | A draw function raised during register/draw. Common: writing to `bpy.data` during register (forbidden) or referencing a renamed socket. | Toggle the addon off+on, check System Console for the traceback. The "Create Taper Curves" recovery button handles the curve-group-missing case explicitly. |
| Mojibake (`â€"`, `â€¢`) in code or UI labels | A bulk rename through PowerShell's `Set-Content -Encoding UTF8` cp1252-double-encoded the non-ASCII chars. | Round-trip the file: read as utf-8, encode as cp1252, decode as utf-8, save as utf-8. (See git history for the one-time fix.) |

## Conventions to keep

- **`bpy.data` writes never happen during `register()` or UI draw.**
  Lazy-create on first user action (Setup operator run, recovery button
  click). Blender enforces this and silently nukes the panel if violated.
- **Always pass `encoding='utf-8'` when reading/writing files via
  Python**, and never round-trip through Windows-default encodings. The
  source files contain bullets, em-dashes, and arrows in comments;
  encoding hops will corrupt them.
- **Stable topbar/header registrations**: any `bpy.types.*.append()`
  call needs the function reference to survive script reload, otherwise
  duplicates pile up. Stash the reference in `bpy.app.driver_namespace`
  with a unique key (see `_install_topbar_button` for the pattern).
- **Operators that need a clean skeleton state should `poll()` against
  the stored root vertex**, not the live selection — root is sticky
  and the artist shouldn't have to re-select before every action.
- **When in doubt about a non-trivial change, ask the user before
  rewriting.** The encoding spec has been iterated several times based
  on the artist's inspection of the reference asset; the values aren't
  guessable from first principles.
