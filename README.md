# Aran Tools

A Blender addon collection for character rigging, weight painting, organization, animation, and bone naming workflows. All tools live in a single **Aran Tools** tab in the N-panel (View3D sidebar), organized into collapsible sections.

**Minimum Blender version:** 3.0

---

## Table of Contents

- [Rigging](#rigging)
  - [Selection](#selection)
  - [Mirror Bones](#mirror-bones)
  - [Feather Rigger](#feather-rigger)
  - [Join & Bind](#join--bind)
  - [Weight from Pointer (ARP)](#weight-from-pointer-arp)
- [Weight Tools](#weight-tools)
  - [Smart Weight Transfer](#smart-weight-transfer)
  - [Sync Vertex Groups](#sync-vertex-groups)
  - [Flatten Island Weights](#flatten-island-weights)
  - [Unify Island Weights](#unify-island-weights)
  - [Island Weight Copy](#island-weight-copy)
  - [Contact Weight Flooder](#contact-weight-flooder)
- [Organization](#organization)
  - [Batch Rig Transfer](#batch-rig-transfer)
  - [Collection Baker](#collection-baker)
  - [Modifier Sync](#modifier-sync)
- [Export](#export)
  - [ARP Batch Export](#arp-batch-export)
- [Naming](#naming)
  - [Object Sequence Namer](#object-sequence-namer)
  - [Bone Renamer](#bone-renamer)
- [Animation](#animation)
  - [Noise on Bones](#noise-on-bones)

---

## Rigging

### Selection

Quick bone selection filters for armatures.

| Button                  | What it does                                 |
| ----------------------- | -------------------------------------------- |
| **Select Deform Bones** | Selects all bones that have _Deform_ enabled |
| **Control Bones**       | Selects all bones prefixed `CTRL_`           |
| **Mech Bones**          | Selects all bones prefixed `MCH_`            |

**Requirements:** An armature must be the active object.

---

### Mirror Bones

Mirrors all `.L` bones to their `.R` counterparts using Blender's built-in Symmetrize.

**How to use:**

1. Select an armature in Object Mode.
2. Click **Mirror Bones L→R**.

Bones must follow the `.L` / `.R` naming convention. Existing `.R` bones will be overwritten.

---

### Feather Rigger

Automatically rigs feathers, hair strands, or any repeated chain-like mesh by detecting mesh islands and binding each one to its nearest bone via Auto-Rig Pro.

**How to use:**

1. In your mesh, mark **sharp edges** to define island boundaries (one island = one feather).
2. In Object Mode, select the **mesh** and the **armature**.
3. Enter Edit Mode on the armature and select the candidate tip bones.
4. Click **Rig Feathers**.

The tool detects each sharp-edge island, finds the closest selected bone tip, selects those vertices, and calls `arp.bind_to_rig`.

**Requirements:** Auto-Rig Pro.

---

### Join & Bind

A two-step workflow for rigging clothing, armor, or accessories that sit on top of a character body.

**Properties:**

| Property              | Description                                                        |
| --------------------- | ------------------------------------------------------------------ |
| **Target Collection** | Optional — moves the joined result into this collection            |
| **Source Mesh**       | The rigged character body to inherit the armature and weights from |
| **Mapping Method**    | Weight interpolation method used during transfer                   |

**Step 1 — Join Selected Targets:**

1. Select all the objects you want to rig (clothing pieces, accessories, etc.).
2. Click **Join Selected Targets**.
   The originals are hidden and a single joined mesh called `Joined_Target` is created.

**Step 2 — Bind and Transfer Weights:**

1. Set **Source Mesh** to the character body.
2. Make `Joined_Target` the active object.
3. Click **Bind and Transfer Weights**.
   The joined mesh is parented to the source's armature and weights are transferred via a Data Transfer modifier.

---

### Weight from Pointer (ARP)

Iterates through every disconnected mesh island, finds the bone tip nearest to a "pointer" location on that island, builds a bone chain of configurable length, and calls Auto-Rig Pro's bind operator — fully automatically.

**Properties:**

| Property         | Description                                                                                                                    |
| ---------------- | ------------------------------------------------------------------------------------------------------------------------------ |
| **Pointer By**   | How to find the pointer location on each island: _Sharp Edge Center_ (center of the first sharp edge) or _Highest UV-Y Vertex_ |
| **Chain Length** | How many parent bones to include above the tip bone when binding                                                               |

**How to use:**

1. Select the **mesh** and the **armature**.
2. In Armature Edit Mode, select all bones that are candidates for chain tips.
3. Back in Object Mode with both selected, click **Weight from Pointer**.

**Direct ARP Bind:**
Skips the automatic island loop — just calls `arp.bind_to_rig` with whatever vertices and bones you have currently selected manually.

**Requirements:** Auto-Rig Pro.

---

## Weight Tools

### Smart Weight Transfer

Transfers vertex weights from a source mesh to one or more target meshes, then binds each target to the source's armature. Pre-creates all vertex groups before transferring to avoid Blender crashes on meshes with no existing groups.

**Properties:**

| Property               | Description                                                    |
| ---------------------- | -------------------------------------------------------------- |
| **Source**             | The rigged mesh to copy weights from                           |
| **Method**             | _Nearest Vertex_, _Nearest Face (Smooth)_, or _Projected Face_ |
| **Clean Empty Groups** | Remove vertex groups with zero influence after transfer        |

**How to use:**

1. Set **Source** to the rigged reference mesh.
2. Select all target meshes in the viewport.
3. Click **Transfer & Bind**.

Meshes with Shape Keys are skipped (Blender limitation).

---

### Sync Vertex Groups

Adds empty vertex groups to the active mesh for any deform bones in its armature that don't already have a corresponding group. Useful before weight painting to ensure the full bone list is visible.

**How to use:**

1. Select a mesh that is bound to an armature.
2. Click **Sync Groups from Armature**.

---

### Flatten Island Weights

For each disconnected mesh island, averages all deform-bone weights across every vertex in that island. The result is that every vertex in the island has identical weights — making the piece bend rigidly as a unit rather than deforming internally.

Designed for assets like feathers, scales, or armor plates where internal deformation is undesirable.

**Properties:**

| Property                    | Description                                                              |
| --------------------------- | ------------------------------------------------------------------------ |
| **Blend**                   | `0` = no change, `1` = fully averaged weights per island                 |
| **Selected Vertices Only**  | Restrict the operation to vertices selected in Edit Mode                 |

**How to use:**

1. Select a mesh that is bound to an armature via an Armature modifier.
2. Adjust the **Blend** factor.
3. Click **Flatten Island Weights**.

---

### Unify Island Weights

For each disconnected mesh island, finds the bone with the highest total influence across that island and assigns the entire island 100% to that bone. Useful for cleaning up automatic weights on assets like feathers or scales where each piece should follow exactly one bone.

**Properties:**

| Property                   | Description                                              |
| -------------------------- | -------------------------------------------------------- |
| **Blend**                  | `0` = no change, `1` = full unification to dominant bone |
| **Only Selected Vertices** | Restrict the operation to vertices selected in Edit Mode |

---

### Island Weight Copy

Copies weights from a single "base" vertex on each mesh island to every other vertex in that island. Useful when you want to manually weight one vertex per piece, then flood it outward.

**Properties:**

| Property                   | Description                                                                               |
| -------------------------- | ----------------------------------------------------------------------------------------- |
| **Base Vertex By**         | How to identify the base vertex per island: _Lowest UV-Y_, _Marked Sharp_, or _Attribute_ |
| **Attribute Name**         | Name of a boolean vertex attribute (only used when _Attribute_ method is selected)        |
| **Blend Factor**           | `0` = no change, `1` = fully replace with base vertex weights                             |
| **Only Selected Vertices** | Restrict the operation to vertices selected in Edit Mode                                  |

---

### Contact Weight Flooder

Transfers weights from a source mesh to secondary meshes by raycasting. For each secondary mesh, it finds the point on the source surface closest to a "contact" vertex, reads the weights at that point, and floods the entire secondary mesh with those weights.

Designed for accessories that are physically touching the character (e.g. a shoulder pad resting on the body) where weights should match the underlying surface.

**Properties:**

| Property               | Description                                            |
| ---------------------- | ------------------------------------------------------ |
| **Source**             | The rigged mesh to sample weights from                 |
| **Blend**              | Blend factor between existing and flooded weights      |
| **Use Selected Verts** | Use only selected vertices to locate the contact point |
| **Use UV Fallback**    | If no vertices are selected, use a UV extreme instead  |
| **UV Map**             | Name of the UV map to use for the fallback             |
| **Axis / Direction**   | Which UV extreme to use (U/V, Min/Max)                 |

**How to use:**

1. Set **Source** to the rigged body mesh.
2. Select all secondary meshes (and optionally select the contact vertices on them).
3. Click **Flood Weights**.

The tool automatically separates loose parts, processes each one, then rejoins them.

---

## Organization

### Batch Rig Transfer

Batch-rigs a collection of unrigged meshes by transferring weights from a collection of already-rigged "ground truth" meshes. Meshes are matched by name automatically using a _Reverse Token Priority_ algorithm.

**Properties:**

| Property                    | Description                                                     |
| --------------------------- | --------------------------------------------------------------- |
| **Source Collection**       | The raw, unrigged meshes to process                             |
| **Ground Truth Collection** | Already-rigged reference meshes to transfer weights from        |
| **Target Collection**       | Where the finished rigged duplicates will be placed             |
| **Apply Modifiers**         | Apply existing modifiers (Mirror, Subsurf, etc.) before rigging |
| **Transfer Method**         | Weight interpolation method                                     |

**How to use:**

1. Set the three collections.
2. Click **Load & Smart Match** — the tool populates a list of source meshes and attempts to pair each one with its ground truth counterpart by name.
3. Review and correct any mismatches in the list.
4. Click **Batch Rig & Transfer**.

**Name matching logic:** Tokens in the source name are compared (in reverse order, prioritising the most specific part) against tokens in ground truth names. Common suffixes like `_rig`, `_baked`, `_lod0` are ignored.

---

### Collection Baker

Duplicates all meshes in a source collection, applies all their modifiers ("Convert to Mesh"), then joins objects that share the same **Target Name** into a single mesh. The source collection is hidden and the result placed in the target collection.

**How to use:**

1. Set **Source Collection** and **Target Collection**.
2. For each mesh in the source list, optionally type a **Target Name**. Meshes sharing the same Target Name will be joined together. Meshes with no Target Name keep their own name.
3. Click **Bake & Join**.

---

### Modifier Sync

Save the modifier stack of one object, choose which modifiers to include, and push them to any number of other objects. Values are updated on modifiers that already exist; missing modifiers are added automatically. Geometry Nodes socket values are fully copied. The relative order of synced modifiers is preserved.

**Typical use-case:** You have a "master" mesh with a final set of modifiers (e.g. Solidify + Weighted Normal + Shrinkwrap + a Geometry Nodes setup) and want every other piece of clothing or geometry to share the same setup.

**Workflow:**

**Step 1 — Save the stack**

1. Pick the **Source Object** from the dropdown.
2. Click **Save Stack** — every modifier on that object appears as a row with a checkbox.

**Step 2 — Choose what to copy**

Tick or untick each modifier row. Only checked modifiers will be copied.

**Step 3 — Set copy mode**

| Option | Behaviour |
| --- | --- |
| **Replace All Modifiers OFF** (default) | Merge into the target's existing stack. Modifiers with the same name have their values updated in-place; missing ones are added. Unrelated modifiers already on the target are left untouched. Synced modifiers are reordered to match source order relative to each other. |
| **Replace All Modifiers ON** | Every existing modifier is removed from each target object first, then the checked modifiers are added fresh in source order. The target ends up with exactly the checked set. |

**Step 4 — Copy to selected objects**

1. Select the target objects in the viewport (the source object itself is always skipped).
2. Click **Copy to Selected**.

The target selection is saved automatically.

**Step 5 — Reapply to last selection**

After copying once, the panel shows a **Last selection** box listing every target object (error icon if any were deleted from the scene). Click **Reapply to Last Selection** to push the current checkbox state, mode, and values to those exact objects again — useful after tweaking modifiers on the source without needing to re-select.

**Geometry Nodes support**

Geometry Nodes socket input values are stored separately from the regular modifier properties in Blender and are not exposed through the standard RNA property list. The tool handles this with a dedicated copy pass that reads socket identifiers from the node group's interface tree and copies each value directly. This covers all input types: numbers, vectors, booleans, colors, and object/collection/material references.

> **Note:** A small set of read-only or type-level properties are intentionally skipped during copying: `rna_type`, `type`, `name`, `is_active`, `show_expanded`.

---

## Export

### ARP Batch Export

Batch-exports meshes or animations as FBX files using Auto-Rig Pro, with flexible filename control. The panel provides two export buttons for separate workflows.

**Properties:**

| Property            | Description                                                             |
| ------------------- | ----------------------------------------------------------------------- |
| **Armature**        | The Auto-Rig Pro armature to export with each mesh                      |
| **Export Folder**   | Destination folder for the FBX files                                    |
| **Remove Text**     | Comma-separated strings to strip from filenames (e.g. `_Rigged, _lod0`) |
| **Prefix / Suffix** | Text to prepend or append to every filename                             |

A **live filename preview** shows the original and final name for the first selected mesh.

---

#### Export Meshes

Exports each selected mesh as a separate FBX file alongside the armature.

Automatically sets **Selected Objects Only = On** and **Bake Animations = Off** in the ARP exporter before running, then restores original settings afterward.

**How to use:**

1. Set the **Armature** and **Export Folder**.
2. Configure naming rules.
3. Select the meshes to export in the viewport.
4. Click **Export Meshes**.

---

#### Export Animations

Exports all actions for the armature as individual FBX files into an `Animations/` subfolder inside the export folder.

Automatically sets **Selected Objects Only = On**, **Bake Animations = On**, **As Multiple FBX = On**, and **Only Active Action = Off** before running, then restores original settings afterward. Only the armature is selected during export — mesh objects are deselected.

**How to use:**

1. Set the **Armature** and **Export Folder**.
2. Click **Export Animations**.

All actions present in the file are exported, each as a separate FBX named after the action.

**Requirements:** Auto-Rig Pro.

---

## Naming

### Object Sequence Namer

Renames selected objects into a numbered sequence such as `Monkey_01`, `Monkey_02`, `Monkey_03`. Respects objects that are already correctly named — only unmatched objects receive new numbers, with gaps in the existing sequence filled first.

**Properties:**

| Property            | Description                                                                                                        |
| ------------------- | ------------------------------------------------------------------------------------------------------------------ |
| **Base Name**       | Root part of the name (e.g. `Monkey` → `Monkey_01`)                                                               |
| **Digits**          | How many digits to pad the index with. `2` → `_01`, `3` → `_001`                                                  |
| **Replace Existing**| When **off** (default): objects that already match `BaseName_NN` keep their number; only unmatched objects are renamed. When **on**: every selected object is renumbered from scratch (matched objects sorted by current number first, then unmatched objects) |

**Default behaviour (Replace Existing OFF):**

1. The entire scene is scanned for objects already named `BaseName_NN`.
2. Their numbers are reserved — nothing will be assigned those slots.
3. Unmatched selected objects receive the lowest available numbers, filling any gaps before extending the sequence.

**Example — gap filling:**

| Before                            | After (2 new objects added)          |
| --------------------------------- | ------------------------------------ |
| `Monkey_01`, `Monkey_03` (scene)  | `Monkey_01`, `Monkey_02` ← new, `Monkey_03`, `Monkey_04` ← new |
| `Cube`, `Sphere` (selected)       | gaps filled before extending sequence |

**How to use:**

1. Type a **Base Name** (e.g. `Monkey`).
2. Select all objects you want to number in the viewport.
3. Click **Apply Sequence Names**.

Already-named objects are untouched unless **Replace Existing** is checked.

> **Note:** Blender name conflicts (`.001` suffixes) are avoided automatically by using temporary names during the rename step.

---

### Bone Renamer

Template-based bone renaming with an auto-incrementing counter. Renames the active bone using a format string and then moves the selection to the next child bone — making it fast to rename entire chains in sequence.

**Format string tokens:**

| Token               | Replaced with                                                 |
| ------------------- | ------------------------------------------------------------- |
| `T1` `T2` `T3` `T4` | The corresponding text field value                            |
| `N1` `N2` `N3`      | The corresponding number field value                          |
| `INC`               | The current counter value (auto-increments after each rename) |

**Default format:** `T1_INC` → e.g. `name_01`

A **live preview** below the fields shows the exact name that will be applied before you click Rename.

**Keyboard shortcuts:**

- `Alt + Shift + R` — Rename active bone
- `Shift + Y` — Reset counter to 1

**Requirements:** Armature must be in Edit Mode with an active bone.

---

## Animation

### Noise on Bones

Adds procedural noise to bone F-curves using Blender's built-in NOISE FCurve modifier. Operates on all selected pose bones. The noise is named `Aran_Noise` so re-applying always updates the same modifier rather than stacking new ones.

**Timing:**

| Property         | Description                                          |
| ---------------- | ---------------------------------------------------- |
| **Last Frame**   | Frame at which the noise fades out completely        |
| **Blend In/Out** | Number of frames to fade in at frame 0 and out at Last Frame |

**Rotation:**

| Property             | Description                                                         |
| -------------------- | ------------------------------------------------------------------- |
| **Strength**         | Overall amplitude of the rotation noise                             |
| **Time Scale**       | Noise pattern scale. Higher = slower, broader oscillation. Lower = faster, tighter oscillation |

**Location:**

| Property             | Description                                                         |
| -------------------- | ------------------------------------------------------------------- |
| **Strength**         | Overall amplitude of the location noise (divided by 100 internally to keep values human-friendly) |
| **Time Scale**       | Noise pattern scale. Higher = slower, broader oscillation. Lower = faster, tighter oscillation |

**Advanced (collapsible):**

Per-axis controls and divisors for fine-tuning.

| Property                    | Description                                                                 |
| --------------------------- | --------------------------------------------------------------------------- |
| **Rot Strength X / Y / Z**  | Per-axis strength multiplier for rotation. `1.0` = full, `0.0` = silenced  |
| **Rot Scale X / Y / Z**     | Per-axis time scale multiplier for rotation. Above `1.0` slows axis down    |
| **Loc Strength X / Y / Z**  | Per-axis strength multiplier for location. `1.0` = full, `0.0` = silenced  |
| **Loc Scale X / Y / Z**     | Per-axis time scale multiplier for location. Above `1.0` slows axis down    |
| **Loc Strength ÷**          | Location strength is divided by this value before reaching the modifier (default 100) |
| **Scale ÷**                 | The Time Scale value is divided by this before reaching the modifier (default 2) |

**Apply buttons:**

| Button           | What it does                                               |
| ---------------- | ---------------------------------------------------------- |
| **Rotation**     | Adds/updates noise on rotation channels only               |
| **Location**     | Adds/updates noise on location channels only               |
| **Both**         | Adds/updates noise on both rotation and location at once   |
| **Remove Noise** | Strips all `Aran_Noise` modifiers from selected bone F-curves |

Each click re-applies the modifier with a new random phase offset, so clicking again randomises the pattern.

**How to use:**

1. In Pose Mode, select the bones you want to affect.
2. Set the timing and strength values.
3. Click **Rotation**, **Location**, or **Both** to apply.

**Requirements:** The armature must have an active Action with existing F-curves on the target bones. Noise is added on top of existing keyframe animation.
