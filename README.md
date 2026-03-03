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
  - [Unify Island Weights](#unify-island-weights)
  - [Island Weight Copy](#island-weight-copy)
  - [Contact Weight Flooder](#contact-weight-flooder)
- [Organization](#organization)
  - [Batch Rig Transfer](#batch-rig-transfer)
  - [Collection Baker](#collection-baker)
- [Export](#export)
  - [ARP Batch Export](#arp-batch-export)
- [Naming](#naming)
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

## Export

### ARP Batch Export

Batch-exports selected meshes as individual FBX files using Auto-Rig Pro's export operator, with flexible filename control.

**Properties:**

| Property            | Description                                                             |
| ------------------- | ----------------------------------------------------------------------- |
| **Armature**        | The Auto-Rig Pro armature to export with each mesh                      |
| **Export Folder**   | Destination folder for the FBX files                                    |
| **Remove Text**     | Comma-separated strings to strip from filenames (e.g. `_Rigged, _lod0`) |
| **Prefix / Suffix** | Text to prepend or append to every filename                             |

A **live filename preview** shows the original and final name for the first selected mesh.

**How to use:**

1. Set the **Armature** and **Export Folder**.
2. Configure naming rules.
3. Select the meshes to export in the viewport.
4. Click **Batch Export Rigged Meshes**.

**Requirements:** Auto-Rig Pro.

---

## Naming

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

Adds procedural noise to bone F-curves using Blender's built-in NOISE FCurve modifier. Operates on all selected pose bones.

**Properties:**

| Property           | Description                                          |
| ------------------ | ---------------------------------------------------- |
| **Rot Strength**   | Amplitude of the rotation noise                      |
| **Rot Speed**      | Frequency of the rotation noise                      |
| **Loc Strength**   | Amplitude of the location noise                      |
| **Loc Speed**      | Frequency of the location noise                      |
| **Frame Length**   | Total frame range the noise is restricted to         |
| **Blend Duration** | Number of frames to fade in/out at the start and end |

**How to use:**

1. In Pose Mode, select the bones you want to affect.
2. Adjust the settings.
3. Click **Rotation** to add noise to rotation channels, **Location** for location channels, or both.

Each click re-applies the modifier with new random phase offsets, so clicking again randomises the pattern.

**Requirements:** The armature must have an active Action with existing F-curves on the target bones. Noise is added on top of existing keyframe animation.
