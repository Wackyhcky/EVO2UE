# EVO → Unreal track importer

These tools take an Assetto Corsa EVO track from the unpacked game files and turn it into an
Unreal Engine level. There are two steps:

1. **`EVO2UE` app** (`evo2ue_gui.py`) or **`evo_track_export.py`** (command line): run on your PC. It converts the
   EVO files to glTF meshes, PNG textures and a `track.json` manifest. You can also inspect
   the result in Blender.
2. **`evo_ue_import.py`**: a Python script you run inside the Unreal Editor. It imports
   everything, builds the materials and places the track.

These are for personal or private use. Don't redistribute the converted Kunos assets.

---

## The app (easiest)

Double-click **`Start_EVO2UE.bat`**. It needs Python 3.9+ from python.org with
*Add to PATH* ticked, and it installs `numpy` and `pillow` the first time.

1. **Game files**: click *Auto-detect* (it searches your Steam libraries) or *Browse…* to
   the game's `content` folder. The game must already be unpacked.
2. **Tracks**: pick one or several (Ctrl/Shift-click).
3. **Options**: texture size (2048 is a good default), LODs, preview `.glb`.
4. **Output folder**: each track gets its own sub-folder.
5. Click **Export**, then **Send to Unreal** (panel 5), which builds the track straight
   into your open Unreal Editor. See *Send to Unreal* below.
6. Every exported track folder also contains `import_into_unreal.py`,
   already set up for that folder, for running the import by hand (the console command is also copied to your
   clipboard).

### Send to Unreal

* **Editor already open:** in your project, enable the *Python Editor Script Plugin* once
  (Edit → Plugins). Then tick **Edit → Project Settings → Plugins → Python → Enable Remote
  Execution**. The app shows "Connected: <project>" within a few seconds. Click **Send to
  Unreal** and follow progress in the app's log. The import runs inside Unreal, so keep the
  editor open.
* **No editor open:** click **Launch project…** and pick your `.uproject`. The app starts the
  right Unreal version and runs the import when it has loaded. The project needs the Python
  plugin enabled, but Remote Execution isn't needed for this.
* **Build in a new level** creates `/Game/EVO/<track>/L_<track>` with a sun, sky and fog.
  Turn it off to build into whatever level is open.
* If Windows Firewall asks about Unreal or Python, allow access. Everything stays on this PC
  (127.0.0.1).

To get a standalone **`EVO2UE.exe`** that runs without Python, run `build_exe.bat` once.
It uses PyInstaller, and the exe ends up in `dist\`.

The command-line tool below does the same thing, for scripting.

## 1. Export from the command line

You need Python 3.9 or newer.

```bat
pip install numpy pillow
python evo_track_export.py "K:\SteamLibrary\steamapps\common\Assetto Corsa EVO\content" laguna_seca C:\EVO_export\laguna_seca --lods --preview
```
You can also edit and double-click `run_export.bat`.

| option | what it does |
|---|---|
| `--lods` | also exports LOD1+ meshes (Unreal uses them as LODs) |
| `--preview` | writes `<track>_preview.glb`, the whole track in one textured file for Blender or Unreal's *Import Into Level* |
| `--max-texture 2048` | caps texture size using the game's own mips (faster, much smaller output) |
| `--no-textures` | geometry only |
| `--all-containers` | also includes event props stored in `common_assets` (academy cones, etc.) |
| `--raw-params` | puts every raw material parameter into `track.json` |
| `--no-recenter` | keeps EVO's world-space pivots (by default each static mesh pivot moves to its centre) |

Laguna Seca takes a few minutes and roughly 2 GB with full-resolution textures (`--max-texture 2048` cuts that a lot).
Track folder names: `brands_hatch cota donington fuji imola kyalami laguna_seca monza
mount_panorama nurburgring oulton_park paul_ricard redbull_ring road_atlanta sebring spa suzuka watkins_glen`.

**Quick look:** in Blender, choose File → Import → glTF and pick `<track>_preview.glb`.

## 2. Import into Unreal (UE 5.3+ recommended)

1. Enable the **Python Editor Script Plugin** (Edit → Plugins) and restart the editor.
   glTF import through Interchange is on by default in UE 5.3+.
2. Open an empty level. Open World or World Partition both work.
3. Run Tools → Execute Python Script → `import_into_unreal.py` from the track's export folder.
   It already points at that folder. You can also paste the `py "…"` command the app copied
   into the Output Log console. If you exported from the command line, set `EXPORT_DIR` at
   the top of the script yourself. The other options in its `CONFIG` block are optional.

You get:

* `/Game/EVO/<track>/Textures`, `/Materials` (`M_EVO_Master` plus one `MI_` per EVO material)
  and `/Meshes`.
* An Outliner folder `EVO/<track>/…` containing static mesh actors, HISM actors for tyre
  walls, crowds and vegetation, invisible collision walls, a PlayerStart on pole, and
  TargetPoints for all grid and pit spawns. Splines are included for the centre line, ideal
  line, pit lane and track limits.

The script measures Unreal's glTF axis conversion with a tiny probe mesh
(`_evo_axis_probe.glb`), so placements are correct whatever convention your engine version
uses. Re-running is safe: existing assets are reused, and the actors it placed before are
replaced.

**Fallback if the script errors on your engine version:** use File → Import Into Level →
`<track>_preview.glb`. That gives you the whole track with basic glTF materials and no
scripting. Instance counts are capped at 2000 per group (change with `--preview-max-instances`).

## Known limitations (v0.1)

* **Materials:** only the Base PBR layer is used. EVO's splat or dirt layers, rubber and
  wetness, biplanar projection and interior-mapping windows are not reproduced, so road and
  terrain blends look simpler than in game. The raw parameters are in `track.json`
  (`--raw-params`) if you want to extend `M_EVO_Master`.
* Decals (112 on Laguna), lights, reflection and irradiance probes, marshals and animated
  NPCs are skipped.
* Spectators and pit crew are exported as their static bind-pose meshes. There is no
  skeleton or animation.
* Physics surfaces (grip per surface) aren't mapped. All visible track meshes get
  complex-as-simple collision, and the EVO wall colliders are placed as hidden actors.
* LOD switch distances are left to Unreal's auto screen size.
* Tested end-to-end on Laguna Seca: the export, the glTF validation and a Blender
  import/render all pass. The Unreal script's transform maths is unit-tested, but the script
  hasn't been run inside an actual editor yet. Expect small API fixes on some engine versions.

Format documentation: see `FORMAT_NOTES.md`.
