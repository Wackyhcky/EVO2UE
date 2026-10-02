"""
evo_ue_import.py - build an Unreal Engine level from evo_track_export.py output.

Run INSIDE the Unreal Editor (UE 5.3+ recommended, Python Editor Script Plugin enabled):
  1. Open (or create) the level you want the track in.
  2. Edit the CONFIG block below (EXPORT_DIR at minimum).
  3. Tools > Execute Python Script... > pick this file
     (or in the Output Log console, Python mode:  exec(open(r"C:\\path\\evo_ue_import.py").read()) )

What it does
  * imports textures  -> /Game/EVO/<track>/Textures   (normal/mask/sRGB settings applied)
  * builds one master material M_EVO_Master and a Material Instance per EVO material
  * imports meshes    -> /Game/EVO/<track>/Meshes     (+ LODs if exported with --lods)
  * places static meshes as StaticMeshActors, instanced props as HISM components,
    spawn points (PlayerStart + TargetPoints) and splines (centre line, ideal line, limits)
  * sets complex-as-simple collision on track geometry; hides collider-only meshes in game

It is safe to re-run: existing assets are reused unless REIMPORT is True; previously placed
actors in the EVO/<track> outliner folder are deleted first when CLEAR_PREVIOUS is True.
"""
import json, math, os, time
import unreal

# =============================== CONFIG ===================================
EXPORT_DIR      = r"C:\EVO_export\laguna_seca"     # folder that contains track.json
DEST_ROOT       = "/Game/EVO"                       # content folder root
REIMPORT        = False      # re-import assets that already exist
IMPORT_LODS     = True       # use <mesh>_LOD1.glb ... if present
PLACE_ACTORS    = True
PLACE_INSTANCES = True       # tyre walls, crowds, vegetation (can be 100k+ instances)
MAX_INSTANCES_PER_GROUP = 0  # 0 = no limit
PLACE_SPAWNS    = True
PLACE_SPLINES   = True
TRACK_COLLISION = True       # complex-as-simple collision on placed static meshes
CLEAR_PREVIOUS  = True       # delete actors previously placed by this script
FLIP_NORMAL_GREEN = False    # flip if bumps look inverted (EVO is assumed DirectX-style, same as UE)
NEW_LEVEL       = False      # create /Game/EVO/<track>/L_<track> and build the track there
# ==========================================================================
# values sent by the EVO2UE app ("Send to Unreal") override the block above
for _k, _v in (globals().get('EVO_OVERRIDES') or {}).items():
    globals()[_k] = _v

at   = unreal.AssetToolsHelpers.get_asset_tools()
eal  = unreal.EditorAssetLibrary
mel  = unreal.MaterialEditingLibrary
eas  = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)

def log(*a):
    unreal.log("[EVO] " + " ".join(str(x) for x in a))

def warn(*a):
    unreal.log_warning("[EVO] " + " ".join(str(x) for x in a))

# ------------------------------------------------------------------ helpers
def asset_path(folder, name):
    return "%s/%s.%s" % (folder, name, name)

def import_files(files, dest, names=None):
    """Batch AssetImportTask; returns list of lists of imported object paths (one per file)."""
    tasks = []
    for i, f in enumerate(files):
        t = unreal.AssetImportTask()
        t.set_editor_property("filename", f)
        t.set_editor_property("destination_path", dest)
        if names:
            t.set_editor_property("destination_name", names[i])
        t.set_editor_property("automated", True)
        t.set_editor_property("replace_existing", True)
        t.set_editor_property("save", False)
        tasks.append(t)
    at.import_asset_tasks(tasks)
    return [list(t.get_editor_property("imported_object_paths") or []) for t in tasks]

def load(path):
    try:
        return unreal.load_asset(path)
    except Exception:
        return None

def find_existing(folder, names, cls):
    for n in names:
        if eal.does_asset_exist("%s/%s" % (folder, n)):
            a = load(asset_path(folder, n))
            if isinstance(a, cls):
                return a
    return None

def first_of_class(paths, cls):
    for p in paths:
        a = load(p)
        if isinstance(a, cls):
            return a
    return None

class Progress:
    def __init__(self, total, label):
        self.t = unreal.ScopedSlowTask(max(total, 1), label)
        self.t.__enter__()
        self.t.make_dialog(True)
    def step(self, msg=""):
        if self.t.should_cancel():
            raise KeyboardInterrupt("cancelled")
        self.t.enter_progress_frame(1, msg)
    def done(self):
        self.t.__exit__(None, None, None)

# ------------------------------------------------------------ axis convention
class Axis:
    """glTF space -> UE space, measured from the probe mesh (see evo_track_export.write_helpers)."""
    def __init__(self, C, scale):
        self.C, self.s = C, scale            # 3x3 signed permutation, unit scale (usually 100)

    @staticmethod
    def detect(probe_glb, folder):
        paths = import_files([probe_glb], folder, ["_evo_axis_probe"])[0]
        sm = first_of_class(paths, unreal.StaticMesh)
        if sm is None:
            raise RuntimeError("could not import axis probe - is the glTF importer (Interchange) enabled?")
        bb = sm.get_bounding_box()
        mn, mx = [bb.min.x, bb.min.y, bb.min.z], [bb.max.x, bb.max.y, bb.max.z]
        # probe extents along glTF x,y,z are 1,2,3 -> find which UE axis carries each
        ext = [mx[i] - mn[i] for i in range(3)]
        scale = max(ext) / 3.0
        C = [[0, 0, 0] for _ in range(3)]
        for g, length in enumerate((1.0, 2.0, 3.0)):
            ue = min(range(3), key=lambda i: abs(ext[i] - length * scale))
            sign = 1 if abs(mx[ue]) > abs(mn[ue]) else -1
            C[ue][g] = sign
        for p in paths:
            try: eal.delete_asset(p.split(".")[0])
            except Exception: pass
        log("axis conversion glTF->UE:", C, "scale", round(scale, 3))
        return Axis(C, scale)

    def vec(self, v, translate=True):
        C, s = self.C, (self.s if translate else 1.0)
        return [s * sum(C[i][j] * v[j] for j in range(3)) for i in range(3)]

    def matrix(self, M):
        """4x4 glTF matrix (row-major list) -> (location, quat xyzw, scale) in UE."""
        C = self.C
        A = [[M[i][j] for j in range(3)] for i in range(3)]
        # A_ue = C A C^T  (C orthogonal)
        CA = [[sum(C[i][k] * A[k][j] for k in range(3)) for j in range(3)] for i in range(3)]
        Aue = [[sum(CA[i][k] * C[j][k] for k in range(3)) for j in range(3)] for i in range(3)]
        t = self.vec([M[0][3], M[1][3], M[2][3]])
        sc = [math.sqrt(sum(Aue[r][c] ** 2 for r in range(3))) or 1.0 for c in range(3)]
        det = (Aue[0][0] * (Aue[1][1] * Aue[2][2] - Aue[1][2] * Aue[2][1])
               - Aue[0][1] * (Aue[1][0] * Aue[2][2] - Aue[1][2] * Aue[2][0])
               + Aue[0][2] * (Aue[1][0] * Aue[2][1] - Aue[1][1] * Aue[2][0]))
        if det < 0:
            sc[0] = -sc[0]
        R = [[Aue[r][c] / sc[c] for c in range(3)] for r in range(3)]
        return t, rot_to_quat(R), sc

    def tqs(self, v):
        """[tx,ty,tz,qx,qy,qz,qw,sx,sy,sz] (glTF) -> UE (loc, quat, scale)."""
        tx, ty, tz, x, y, z, w, sx, sy, sz = v
        R = [[1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)],
             [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
             [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)]]
        M = [[R[r][c] * (sx, sy, sz)[c] for c in range(3)] + [(tx, ty, tz)[r]] for r in range(3)]
        return self.matrix(M + [[0, 0, 0, 1]])

def rot_to_quat(R):
    tr = R[0][0] + R[1][1] + R[2][2]
    if tr > 0:
        S = math.sqrt(tr + 1.0) * 2; w = 0.25 * S
        x = (R[2][1] - R[1][2]) / S; y = (R[0][2] - R[2][0]) / S; z = (R[1][0] - R[0][1]) / S
    elif R[0][0] > R[1][1] and R[0][0] > R[2][2]:
        S = math.sqrt(1.0 + R[0][0] - R[1][1] - R[2][2]) * 2
        w = (R[2][1] - R[1][2]) / S; x = 0.25 * S; y = (R[0][1] + R[1][0]) / S; z = (R[0][2] + R[2][0]) / S
    elif R[1][1] > R[2][2]:
        S = math.sqrt(1.0 + R[1][1] - R[0][0] - R[2][2]) * 2
        w = (R[0][2] - R[2][0]) / S; x = (R[0][1] + R[1][0]) / S; y = 0.25 * S; z = (R[1][2] + R[2][1]) / S
    else:
        S = math.sqrt(1.0 + R[2][2] - R[0][0] - R[1][1]) * 2
        w = (R[1][0] - R[0][1]) / S; x = (R[0][2] + R[2][0]) / S; y = (R[1][2] + R[2][1]) / S; z = 0.25 * S
    n = math.sqrt(x * x + y * y + z * z + w * w) or 1.0
    return [x / n, y / n, z / n, w / n]

def ue_transform(loc, q, sc):
    quat = unreal.Quat(q[0], q[1], q[2], q[3])
    return unreal.Transform(location=unreal.Vector(*loc), rotation=quat.rotator(), scale=unreal.Vector(*sc))

# ------------------------------------------------------------------ textures
SLOT_KIND = {'base_color': 'color', 'emissive': 'color', 'normal': 'normal',
             'roughness': 'mask', 'metallic': 'mask', 'ao': 'mask', 'opacity': 'mask'}

def import_textures(manifest, folder):
    usage = {}
    for m in manifest['materials']:
        for slot, f in (m['pbr'].get('texture_files') or {}).items():
            if f:
                kind = SLOT_KIND.get(slot, 'color')
                prev = usage.get(f)
                # priority if one texture is used several ways: normal > color > mask
                if prev is None or (kind == 'normal') or (kind == 'color' and prev == 'mask'):
                    usage[f] = kind
    usage['_evo_white_linear'] = 'mask'
    usage['_evo_flat_normal'] = 'normal'
    tdir = os.path.join(EXPORT_DIR, 'textures')
    found, todo = {}, []
    for f in usage:
        ex = None if REIMPORT else find_existing(folder, ['T_' + f, f], unreal.Texture2D)
        if ex:
            found[f] = ex
        elif os.path.isfile(os.path.join(tdir, f + '.png')):
            todo.append(f)
    log("importing %d textures (%d already present)" % (len(todo), len(found)))
    pg = Progress(max(1, (len(todo) + 39) // 40), "EVO: importing textures")
    try:
        for i in range(0, len(todo), 40):
            pg.step()
            chunk = todo[i:i + 40]
            res = import_files([os.path.join(tdir, f + '.png') for f in chunk], folder, ['T_' + f for f in chunk])
            for f, paths in zip(chunk, res):
                t = first_of_class(paths, unreal.Texture2D)
                if t:
                    found[f] = t
    finally:
        pg.done()
    out = {}
    for f, kind in usage.items():
        tex = found.get(f)
        if tex is None:
            continue
        try:
            if kind == 'normal':
                tex.set_editor_property('compression_settings', unreal.TextureCompressionSettings.TC_NORMALMAP)
                tex.set_editor_property('srgb', False)
                tex.set_editor_property('flip_green_channel', FLIP_NORMAL_GREEN)
            elif kind == 'mask':
                tex.set_editor_property('compression_settings', unreal.TextureCompressionSettings.TC_MASKS)
                tex.set_editor_property('srgb', False)
            else:
                tex.set_editor_property('compression_settings', unreal.TextureCompressionSettings.TC_DEFAULT)
                tex.set_editor_property('srgb', True)
        except Exception as e:
            warn("texture settings", f, e)
        out[f] = tex
    eal.save_directory(folder, only_if_is_dirty=True, recursive=False)
    return out

# ------------------------------------------------------------------ material
def build_master(folder, textures):
    path = asset_path(folder, 'M_EVO_Master')
    if eal.does_asset_exist(path.split('.')[0]):
        return load(path)
    mat = at.create_asset('M_EVO_Master', folder, unreal.Material, unreal.MaterialFactoryNew())
    try:
        mat.set_editor_property('translucency_lighting_mode', unreal.TranslucencyLightingMode.TLM_SURFACE)
    except Exception:
        pass
    E = lambda cls, x, y: mel.create_material_expression(mat, cls, x, y)
    def param(cls, name, x, y, **props):
        e = E(cls, x, y); e.set_editor_property('parameter_name', name)
        for k, v in props.items():
            e.set_editor_property(k, v)
        return e
    white = load('/Engine/EngineResources/WhiteSquareTexture.WhiteSquareTexture')
    lin = textures.get('_evo_white_linear')
    flat = textures.get('_evo_flat_normal') or load('/Engine/EngineMaterials/DefaultNormal.DefaultNormal')
    ST = unreal.MaterialSamplerType
    mask_sampler = ST.SAMPLERTYPE_MASKS
    if lin is None:                          # exported with --no-textures
        lin, mask_sampler = white, ST.SAMPLERTYPE_COLOR

    uv = E(unreal.MaterialExpressionTextureCoordinate, -1500, 0)
    uvs = param(unreal.MaterialExpressionVectorParameter, 'UVScale', -1500, 150, default_value=unreal.LinearColor(1, 1, 0, 0))
    msk = E(unreal.MaterialExpressionComponentMask, -1300, 150)
    msk.set_editor_property('r', True); msk.set_editor_property('g', True)
    mel.connect_material_expressions(uvs, '', msk, '')
    uvm = E(unreal.MaterialExpressionMultiply, -1150, 50)
    mel.connect_material_expressions(uv, '', uvm, 'A'); mel.connect_material_expressions(msk, '', uvm, 'B')

    def tex(name, default, sampler, y):
        t = param(unreal.MaterialExpressionTextureSampleParameter2D, name, -900, y, texture=default, sampler_type=sampler)
        mel.connect_material_expressions(uvm, '', t, 'UVs')
        return t
    bc = tex('BaseColor', white, ST.SAMPLERTYPE_COLOR, -400)
    nm = tex('Normal', flat, ST.SAMPLERTYPE_NORMAL, -100)
    rg = tex('Roughness', lin, mask_sampler, 200)
    mt = tex('Metallic', lin, mask_sampler, 500)
    ao = tex('AO', lin, mask_sampler, 800)
    em = tex('Emissive', white, ST.SAMPLERTYPE_COLOR, 1100)

    tint = param(unreal.MaterialExpressionVectorParameter, 'BaseColorTint', -600, -600, default_value=unreal.LinearColor(1, 1, 1, 1))
    m1 = E(unreal.MaterialExpressionMultiply, -400, -500)
    mel.connect_material_expressions(bc, 'RGB', m1, 'A'); mel.connect_material_expressions(tint, '', m1, 'B')
    mel.connect_material_property(m1, '', unreal.MaterialProperty.MP_BASE_COLOR)

    op = E(unreal.MaterialExpressionMultiply, -400, -300)
    mel.connect_material_expressions(bc, 'A', op, 'A'); mel.connect_material_expressions(tint, 'A', op, 'B')
    mel.connect_material_property(op, '', unreal.MaterialProperty.MP_OPACITY)
    mel.connect_material_property(op, '', unreal.MaterialProperty.MP_OPACITY_MASK)

    mel.connect_material_property(nm, 'RGB', unreal.MaterialProperty.MP_NORMAL)

    def scaled(texnode, pname, default, y, prop):
        p = param(unreal.MaterialExpressionScalarParameter, pname, -600, y, default_value=default)
        m = E(unreal.MaterialExpressionMultiply, -400, y)
        mel.connect_material_expressions(texnode, 'R', m, 'A'); mel.connect_material_expressions(p, '', m, 'B')
        mel.connect_material_property(m, '', prop)
    scaled(rg, 'RoughnessScale', 0.7, 250, unreal.MaterialProperty.MP_ROUGHNESS)
    scaled(mt, 'MetallicScale', 0.0, 550, unreal.MaterialProperty.MP_METALLIC)

    one = E(unreal.MaterialExpressionConstant, -600, 850); one.set_editor_property('r', 1.0)
    aos = param(unreal.MaterialExpressionScalarParameter, 'AOStrength', -600, 950, default_value=1.0)
    lerp = E(unreal.MaterialExpressionLinearInterpolate, -400, 850)
    mel.connect_material_expressions(one, '', lerp, 'A'); mel.connect_material_expressions(ao, 'R', lerp, 'B')
    mel.connect_material_expressions(aos, '', lerp, 'Alpha')
    mel.connect_material_property(lerp, '', unreal.MaterialProperty.MP_AMBIENT_OCCLUSION)

    ec = param(unreal.MaterialExpressionVectorParameter, 'EmissiveColor', -600, 1150, default_value=unreal.LinearColor(0, 0, 0, 1))
    m2 = E(unreal.MaterialExpressionMultiply, -400, 1150)
    mel.connect_material_expressions(em, 'RGB', m2, 'A'); mel.connect_material_expressions(ec, '', m2, 'B')
    mel.connect_material_property(m2, '', unreal.MaterialProperty.MP_EMISSIVE_COLOR)

    mel.recompile_material(mat)
    eal.save_loaded_asset(mat)
    log("created master material", mat.get_path_name())
    return mat

def build_instances(manifest, folder, master, textures):
    mis = {}
    pg = Progress(len(manifest['materials']), "EVO: material instances")
    try:
        for m in manifest['materials']:
            pg.step(m['name'])
            name = 'MI_' + m['name']
            path = "%s/%s" % (folder, name)
            if eal.does_asset_exist(path) and not REIMPORT:
                mis[m['name']] = load(asset_path(folder, name)); continue
            mi = load(asset_path(folder, name)) if eal.does_asset_exist(path) else \
                at.create_asset(name, folder, unreal.MaterialInstanceConstant, unreal.MaterialInstanceConstantFactoryNew())
            mel.set_material_instance_parent(mi, master)
            p = m['pbr']
            files = p.get('texture_files') or {}
            for slot, pname in (('base_color', 'BaseColor'), ('normal', 'Normal'), ('roughness', 'Roughness'),
                                ('metallic', 'Metallic'), ('ao', 'AO'), ('emissive', 'Emissive')):
                t = textures.get(files.get(slot) or '')
                if t:
                    mel.set_material_instance_texture_parameter_value(mi, pname, t)
            c = p['base_color_factor']
            mel.set_material_instance_vector_parameter_value(mi, 'BaseColorTint', unreal.LinearColor(c[0], c[1], c[2], c[3]))
            mel.set_material_instance_vector_parameter_value(mi, 'UVScale', unreal.LinearColor(p['uv_scale'][0], p['uv_scale'][1], 0, 0))
            has_r = bool(textures.get(files.get('roughness') or ''))
            mel.set_material_instance_scalar_parameter_value(mi, 'RoughnessScale', 1.0 if has_r else p['roughness'])
            has_m = bool(textures.get(files.get('metallic') or ''))
            mel.set_material_instance_scalar_parameter_value(mi, 'MetallicScale', 1.0 if has_m else p['metallic'])
            e = p.get('emissive_factor') or [0, 0, 0]
            if any(e) or files.get('emissive'):
                ef = e if any(e) else [1, 1, 1]
                mel.set_material_instance_vector_parameter_value(mi, 'EmissiveColor', unreal.LinearColor(ef[0], ef[1], ef[2], 1))
            try:
                o = mi.get_editor_property('base_property_overrides')
                if p['alpha_mode'] == 'MASK':
                    o.set_editor_property('override_blend_mode', True)
                    o.set_editor_property('blend_mode', unreal.BlendMode.BLEND_MASKED)
                    o.set_editor_property('override_opacity_mask_clip_value', True)
                    o.set_editor_property('opacity_mask_clip_value', min(max(p.get('alpha_cutoff', 0.33), 0.05), 0.9))
                elif p['alpha_mode'] == 'BLEND':
                    o.set_editor_property('override_blend_mode', True)
                    o.set_editor_property('blend_mode', unreal.BlendMode.BLEND_TRANSLUCENT)
                if p.get('double_sided'):
                    o.set_editor_property('override_two_sided', True)
                    o.set_editor_property('two_sided', True)
                mi.set_editor_property('base_property_overrides', o)
            except Exception as ex:
                warn("blend/two-sided override failed for", name, ex)
            mel.update_material_instance(mi)
            mis[m['name']] = mi
    finally:
        pg.done()
    return mis

# ------------------------------------------------------------------ meshes
def import_meshes(manifest, folder, mis):
    meshes = {}
    todo = []
    for e in manifest['meshes']:
        ex = None if REIMPORT else find_existing(folder, ['SM_' + e['name'], e['name']], unreal.StaticMesh)
        if ex:
            meshes[e['name']] = ex
        else:
            todo.append(e)
    log("importing %d meshes (%d already present)" % (len(todo), len(meshes)))
    pg = Progress(len(todo), "EVO: importing meshes")
    junk = []
    try:
        for e in todo:
            pg.step(e['name'])
            files = [os.path.join(EXPORT_DIR, l['file']) for l in e['lods']]
            names = ['SM_' + e['name']] + ['SM_%s_LOD%d' % (e['name'], i) for i in range(1, len(files))]
            if not IMPORT_LODS:
                files, names = files[:1], names[:1]
            results = import_files(files, folder, names)
            base = first_of_class(results[0], unreal.StaticMesh)
            if base is None:
                warn("mesh import failed:", e['name']); continue
            # LODs
            for li, res in enumerate(results[1:], start=1):
                src = first_of_class(res, unreal.StaticMesh)
                if src is None:
                    continue
                try:
                    sms = unreal.get_editor_subsystem(unreal.StaticMeshEditorSubsystem)
                    sms.set_lod_from_static_mesh(base, li, src, 0, True)
                except Exception as ex:
                    warn("LOD", li, e['name'], ex)
                junk += res
            # materials: our MIs by slot order
            nslots = len(base.get_editor_property('static_materials'))
            for si, mname in enumerate(e.get('slots') or []):
                mi = mis.get(mname)
                if mi is not None and si < nslots:
                    base.set_material(si, mi)
            for res in results[:1]:
                junk += [p for p in res if not isinstance(load(p), unreal.StaticMesh)]
            if TRACK_COLLISION:
                try:
                    bs = base.get_editor_property('body_setup')
                    bs.set_editor_property('collision_trace_flag', unreal.CollisionTraceFlag.CTF_USE_COMPLEX_AS_SIMPLE)
                except Exception:
                    pass
            meshes[e['name']] = base
    finally:
        pg.done()
    # remove auto-generated materials and LOD source meshes
    for p in set(junk):
        try:
            eal.delete_asset(p.split('.')[0])
        except Exception:
            pass
    return meshes

# ------------------------------------------------------------------ level
def clear_previous(folder_label):
    n = 0
    for a in eas.get_all_level_actors():
        try:
            fp = str(a.get_folder_path())
        except Exception:
            continue
        if fp.startswith(folder_label):
            a.destroy_actor(); n += 1
    if n:
        log("removed %d previously placed actors" % n)

def add_component(actor, cls):
    """Add a component to a placed actor (UE 5.1+ SubobjectDataSubsystem)."""
    sds = unreal.get_engine_subsystem(unreal.SubobjectDataSubsystem)
    root = sds.k2_gather_subobject_data_for_instance(actor)[0]
    params = unreal.AddNewSubobjectParams(parent_handle=root, new_class=cls, blueprint_context=None)
    handle, fail = sds.add_new_subobject(params)
    if not fail.is_empty():
        raise RuntimeError(str(fail))
    data = unreal.SubobjectDataBlueprintFunctionLibrary.get_data(handle)
    return unreal.SubobjectDataBlueprintFunctionLibrary.get_object(data)

def place(manifest, axis, meshes, label_root):
    by_name = {e['name']: e for e in manifest['meshes']}
    if PLACE_ACTORS:
        pls = manifest['placements']
        pg = Progress(len(pls), "EVO: placing meshes")
        try:
            for pl in pls:
                pg.step()
                sm = meshes.get(pl['mesh'])
                if sm is None:
                    continue
                loc, q, sc = axis.matrix(pl['matrix'])
                a = eas.spawn_actor_from_object(sm, unreal.Vector(*loc), unreal.Rotator(0, 0, 0))
                a.set_actor_transform(ue_transform(loc, q, sc), False, True)
                a.set_actor_label(pl['name'] or pl['mesh'])
                coll = by_name[pl['mesh']]['collision_only']
                a.set_folder_path(label_root + ('/Collision' if coll else '/' + pl['group']))
                if coll:
                    a.set_actor_hidden_in_game(True)
                    try:
                        a.get_editor_property('static_mesh_component').set_editor_property('visible', False)
                    except Exception:
                        pass
        finally:
            pg.done()
    if PLACE_INSTANCES:
        groups = manifest['instances']
        pg = Progress(len(groups), "EVO: instanced props")
        try:
            for ig in groups:
                pg.step(ig['mesh'])
                sm = meshes.get(ig['mesh'])
                if sm is None:
                    continue
                ts = ig['transforms']
                if MAX_INSTANCES_PER_GROUP and len(ts) > MAX_INSTANCES_PER_GROUP:
                    step = len(ts) / float(MAX_INSTANCES_PER_GROUP)
                    ts = [ts[int(i * step)] for i in range(MAX_INSTANCES_PER_GROUP)]
                xf = [ue_transform(*axis.tqs(t)) for t in ts]
                a = eas.spawn_actor_from_class(unreal.Actor, unreal.Vector(0, 0, 0), unreal.Rotator(0, 0, 0))
                a.set_actor_label('%s_x%d' % (ig['mesh'], len(xf)))
                a.set_folder_path(label_root + '/Instances/' + ig['group'])
                try:
                    comp = add_component(a, unreal.HierarchicalInstancedStaticMeshComponent)
                    comp.set_static_mesh(sm)
                    coll = 'collider' in ig['mesh'].lower() or by_name[ig['mesh']]['collision_only']
                    comp.set_collision_enabled(unreal.CollisionEnabled.QUERY_AND_PHYSICS if coll else unreal.CollisionEnabled.NO_COLLISION)
                    if coll:
                        comp.set_editor_property('visible', False)
                    comp.add_instances(xf, False)      # actor sits at the origin, so local == world
                except Exception as ex:
                    warn("HISM failed for %s (%s) - falling back to individual actors" % (ig['mesh'], ex))
                    a.destroy_actor()
                    for t in xf[:500]:
                        s = eas.spawn_actor_from_object(sm, t.translation, unreal.Rotator(0, 0, 0))
                        s.set_actor_transform(t, False, True)
                        s.set_folder_path(label_root + '/Instances/' + ig['group'])
        finally:
            pg.done()
    if PLACE_SPAWNS:
        first = True
        for sp in manifest['start_positions']:
            loc, q, sc = axis.matrix(sp['matrix'])
            cls = unreal.PlayerStart if (first and sp['group'] in ('hotlap_start', 'spawnpoints_grid')) else unreal.TargetPoint
            a = eas.spawn_actor_from_class(cls, unreal.Vector(*loc), unreal.Rotator(0, 0, 0))
            # EVO spawn forward is +Z (glTF); UE actors face +X -> rotate the frame so +X = EVO forward
            fwd = axis.vec([sp['matrix'][0][2], sp['matrix'][1][2], sp['matrix'][2][2]], translate=False)
            yaw = math.degrees(math.atan2(fwd[1], fwd[0]))
            a.set_actor_rotation(unreal.Rotator(0, 0, yaw), False)
            a.set_actor_location(unreal.Vector(loc[0], loc[1], loc[2] + 50), False, True)
            a.set_actor_label('%s_%s' % (sp['group'], sp['name']))
            a.set_folder_path(label_root + '/Spawns/' + sp['group'])
            if cls is unreal.PlayerStart:
                first = False
    if PLACE_SPLINES:
        for spl in manifest['splines']:
            pts = [unreal.Vector(*axis.vec(p)) for p in spl['points']]
            if len(pts) < 2:
                continue
            a = eas.spawn_actor_from_class(unreal.Actor, pts[0], unreal.Rotator(0, 0, 0))
            a.set_actor_label('Spline_' + spl['name'])
            a.set_folder_path(label_root + '/Splines')
            try:
                sc = add_component(a, unreal.SplineComponent)
                sc.set_spline_points(pts, unreal.SplineCoordinateSpace.WORLD, True)
                closed = (spl['points'][0][0] - spl['points'][-1][0]) ** 2 + (spl['points'][0][2] - spl['points'][-1][2]) ** 2 < 25
                sc.set_closed_loop(closed, True)
            except Exception as ex:
                warn("spline", spl['name'], ex)

# ------------------------------------------------------------------ main
def main():
    t0 = time.time()
    with open(os.path.join(EXPORT_DIR, 'track.json')) as f:
        manifest = json.load(f)
    track = manifest['track']
    root = "%s/%s" % (DEST_ROOT, track)
    f_tex, f_mat, f_mesh = root + "/Textures", root + "/Materials", root + "/Meshes"
    for d in (f_tex, f_mat, f_mesh):
        eal.make_directory(d)
    log("EVO import: %s  (%d meshes, %d materials, %d placements, %d instance groups)" % (
        track, len(manifest['meshes']), len(manifest['materials']), len(manifest['placements']), len(manifest['instances'])))
    axis = Axis.detect(os.path.join(EXPORT_DIR, '_evo_axis_probe.glb'), root)
    textures = import_textures(manifest, f_tex)
    master = build_master(f_mat, textures)
    mis = build_instances(manifest, f_mat, master, textures)
    meshes = import_meshes(manifest, f_mesh, mis)
    label_root = 'EVO/' + track
    level_path = None
    if NEW_LEVEL:
        level_path = "%s/L_%s" % (root, track)
        les = unreal.get_editor_subsystem(unreal.LevelEditorSubsystem)
        try:
            if eal.does_asset_exist(level_path):
                les.load_level(level_path)
            else:
                les.new_level(level_path)
            sky = eas.spawn_actor_from_class(unreal.DirectionalLight, unreal.Vector(0, 0, 50000), unreal.Rotator(-40, 0, 30))
            sky.set_folder_path(label_root + '/Lighting')
            for cls in (unreal.SkyLight, unreal.SkyAtmosphere, unreal.ExponentialHeightFog):
                a = eas.spawn_actor_from_class(cls, unreal.Vector(0, 0, 0), unreal.Rotator(0, 0, 0))
                a.set_folder_path(label_root + '/Lighting')
            try:
                sl = [a for a in eas.get_all_level_actors() if isinstance(a, unreal.SkyLight)][0]
                sl.get_component_by_class(unreal.SkyLightComponent).set_editor_property('real_time_capture', True)
            except Exception:
                pass
            log("building track in new level", level_path)
        except Exception as ex:
            warn("could not create level (%s) - using the current level" % ex)
            level_path = None
    if CLEAR_PREVIOUS:
        clear_previous(label_root)
    place(manifest, axis, meshes, label_root)
    eal.save_directory(root, only_if_is_dirty=True, recursive=True)
    if level_path:
        try:
            unreal.get_editor_subsystem(unreal.LevelEditorSubsystem).save_current_level()
        except Exception as ex:
            warn("save level:", ex)
    log("done in %.0fs - actors are in the Outliner folder %s" % (time.time() - t0, label_root))
    return "EVO_IMPORT_OK %s %d placements" % (track, len(manifest['placements']))

try:
    _evo_result = main()
    log(_evo_result)
except KeyboardInterrupt:
    warn("import cancelled")
except Exception:
    import traceback
    unreal.log_error("[EVO] import failed:\n" + traceback.format_exc())
    raise
