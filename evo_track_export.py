#!/usr/bin/env python3
"""
evo_track_export.py - Assetto Corsa EVO track -> glTF / PNG / JSON exporter.

Reads an *unpacked* EVO track (the loose files under content\\tracks\\<track>)
and writes everything Unreal (or Blender) needs:

  <out>/meshes/<name>.glb          one per mesh asset, LOD0 (+ <name>_LOD1.glb ...)
  <out>/textures/<name>.png        decoded textures (BC1/BC4/BC5/BC7, tiled)
  <out>/track.json                 manifest: materials, placements, instances,
                                   spawn points, splines - used by the UE script
  <out>/<track>_preview.glb        (optional) whole track in one file, textured,
                                   for Blender or UE "Import Into Level"

Requirements:  Python 3.9+,  pip install numpy pillow
Usage:
  python evo_track_export.py "K:\\SteamLibrary\\steamapps\\common\\Assetto Corsa EVO\\content" laguna_seca out_laguna --preview

All formats were reverse-engineered from game files; see FORMAT_NOTES.md.
For personal / private use only - don't redistribute Kunos assets.
"""
import argparse, json, math, os, re, struct, sys, time
from collections import defaultdict, Counter

try:
    import numpy as np
    from PIL import Image
except ImportError:
    sys.exit("Missing dependency. Run:  pip install numpy pillow")

VERSION = "0.1.0"

# --------------------------------------------------------------------------
# protobuf wire format (EVO stores everything as schema-less protobuf)
# --------------------------------------------------------------------------
def _varint(b, i):
    r = s = 0
    while True:
        c = b[i]; i += 1
        r |= (c & 0x7F) << s; s += 7
        if c < 0x80:
            return r, i

def pb_parse(b):
    """bytes -> list of (field, wiretype, value). Raises ValueError if not protobuf."""
    out, i, n = [], 0, len(b)
    try:
        while i < n:
            k, i = _varint(b, i)
            f, w = k >> 3, k & 7
            if f == 0:
                raise ValueError
            if w == 0:
                v, i = _varint(b, i)
            elif w == 1:
                v = b[i:i + 8]; i += 8
            elif w == 2:
                l, i = _varint(b, i); v = b[i:i + l]; i += l
            elif w == 5:
                v = b[i:i + 4]; i += 4
            else:
                raise ValueError
            out.append((f, w, v))
    except IndexError:
        raise ValueError
    if i != n:
        raise ValueError
    return out

def pb(b):
    """bytes -> {field: [values...]}"""
    d = {}
    if b:
        for f, w, v in pb_parse(b):
            d.setdefault(f, []).append(v)
    return d

def pb1(d, f, default=None):
    v = d.get(f)
    return v[0] if v else default

def f32(v, default=0.0):
    if v is None:
        return default
    if isinstance(v, int):          # proto3 default-zero encoded oddly; shouldn't happen
        return float(v)
    return struct.unpack('<f', v)[0]

def vec(b, n=3, default=0.0):
    d = pb(b) if b else {}
    return [f32(pb1(d, i + 1), default) for i in range(n)]

def varints(b):
    out, i = [], 0
    while i < len(b):
        x, i = _varint(b, i); out.append(x)
    return out

def s(b):
    return b.decode('utf-8', 'replace') if isinstance(b, (bytes, bytearray)) else str(b)

# --------------------------------------------------------------------------
# paths
# --------------------------------------------------------------------------
class Resolver:
    """Resolves EVO-internal paths ('content\\tracks\\x\\y.mesh', 'editor/z.material')
    to real files, case-insensitively (EVO data mixes case freely)."""
    def __init__(self, content_dir):
        self.content = os.path.abspath(content_dir)
        self._dircache = {}

    def _listing(self, d):
        if d not in self._dircache:
            try:
                self._dircache[d] = {e.lower(): e for e in os.listdir(d)}
            except OSError:
                self._dircache[d] = {}
        return self._dircache[d]

    def resolve(self, p):
        if not p:
            return None
        parts = [x for x in re.split(r'[\\/]+', p.strip()) if x]
        if parts and parts[0].lower() == 'content':
            parts = parts[1:]
        cur = self.content
        for part in parts:
            real = self._listing(cur).get(part.lower())
            if real is None:
                return None
            cur = os.path.join(cur, real)
        return cur if os.path.isfile(cur) else None

def stem(p):
    return re.split(r'[\\/]', p)[-1].rsplit('.', 1)[0]

def safe_name(n):
    return re.sub(r'[^A-Za-z0-9_\-]', '_', n)

# --------------------------------------------------------------------------
# transforms  (EVO: right-handed, Y up, metres -> identical to glTF)
# --------------------------------------------------------------------------
def euler_matrix(rx, ry, rz):
    """EVO stores Euler angles in degrees. Order verified against spawn-point
    headings: R = Rz * Ry * Rx (X applied first)."""
    ax, ay, az = (math.radians(a) for a in (rx, ry, rz))
    cx, sx, cy, sy, cz, sz = math.cos(ax), math.sin(ax), math.cos(ay), math.sin(ay), math.cos(az), math.sin(az)
    Rx = np.array([[1, 0, 0], [0, cx, -sx], [0, sx, cx]])
    Ry = np.array([[cy, 0, sy], [0, 1, 0], [-sy, 0, cy]])
    Rz = np.array([[cz, -sz, 0], [sz, cz, 0], [0, 0, 1]])
    return EULER_ORDER(Rx, Ry, Rz)

EULER_ORDER = lambda Rx, Ry, Rz: Rz @ Ry @ Rx

def trs_matrix(pos, rot, scl):
    M = np.eye(4)
    M[:3, :3] = euler_matrix(*rot) * np.array(scl)[None, :]
    M[:3, 3] = pos
    return M

def read_transform(b):
    d = pb(b)
    pos = vec(pb1(d, 1)) if 1 in d else [0.0, 0.0, 0.0]
    rot = vec(pb1(d, 2)) if 2 in d else [0.0, 0.0, 0.0]
    scl = vec(pb1(d, 3)) if 3 in d else [1.0, 1.0, 1.0]
    return pos, rot, scl

def mat_to_trs(M):
    """4x4 -> (translation, quaternion xyzw, scale) for glTF / UE."""
    t = M[:3, 3].tolist()
    A = M[:3, :3]
    sc = np.linalg.norm(A, axis=0)
    if np.linalg.det(A) < 0:
        sc[0] = -sc[0]
    R = A / np.where(sc == 0, 1, sc)[None, :]
    q = rot_to_quat(R)
    return t, q, sc.tolist()

def tqs_to_matrix(v):
    tx, ty, tz, x, y, z, w, sx, sy, sz = v
    R = np.array([[1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)],
                  [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
                  [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)]])
    M = np.eye(4); M[:3, :3] = R * np.array([sx, sy, sz])[None, :]; M[:3, 3] = [tx, ty, tz]
    return M

def rot_to_quat(R):
    tr = R[0, 0] + R[1, 1] + R[2, 2]
    if tr > 0:
        S = math.sqrt(tr + 1.0) * 2
        w = 0.25 * S; x = (R[2, 1] - R[1, 2]) / S; y = (R[0, 2] - R[2, 0]) / S; z = (R[1, 0] - R[0, 1]) / S
    elif R[0, 0] > R[1, 1] and R[0, 0] > R[2, 2]:
        S = math.sqrt(1.0 + R[0, 0] - R[1, 1] - R[2, 2]) * 2
        w = (R[2, 1] - R[1, 2]) / S; x = 0.25 * S; y = (R[0, 1] + R[1, 0]) / S; z = (R[0, 2] + R[2, 0]) / S
    elif R[1, 1] > R[2, 2]:
        S = math.sqrt(1.0 + R[1, 1] - R[0, 0] - R[2, 2]) * 2
        w = (R[0, 2] - R[2, 0]) / S; x = (R[0, 1] + R[1, 0]) / S; y = 0.25 * S; z = (R[1, 2] + R[2, 1]) / S
    else:
        S = math.sqrt(1.0 + R[2, 2] - R[0, 0] - R[1, 1]) * 2
        w = (R[1, 0] - R[0, 1]) / S; x = (R[0, 2] + R[2, 0]) / S; y = (R[1, 2] + R[2, 1]) / S; z = 0.25 * S
    n = math.sqrt(x * x + y * y + z * z + w * w) or 1
    return [x / n, y / n, z / n, w / n]

# --------------------------------------------------------------------------
# textures
# --------------------------------------------------------------------------
# header field 11.1 -> (pillow bcn id, srgb, kind)
TEX_FORMATS = {
    0: (7, True,  'BC7'),
    1: (7, False, 'BC7'),
    2: (4, False, 'BC4'),
    3: (5, False, 'BC5'),
    5: (1, False, 'BC1'),
    6: (3, False, 'BC3'),      # inferred (linear twin of 7)
    7: (3, True,  'BC3'),
    8: (1, True,  'BC1'),
}
_UNKNOWN_SEEN = set()
TILE_BYTES = 65536

def read_texture_header(path):
    h = pb(open(path, 'rb').read())
    t = pb(pb1(h, 12, b''))
    e = pb(pb1(h, 11, b''))
    return {
        'width': pb1(h, 1), 'height': pb1(h, 2), 'mips': pb1(h, 3, 1),
        'format': pb1(e, 1, 0),
        'tile_w': pb1(t, 1), 'tile_h': pb1(t, 2),
        'mip_tile_start': list(pb1(t, 4, b'')), 'mip_tile_count': list(pb1(t, 5, b'')),
        'source': s(pb1(pb(pb1(h, 10, b'')), 1, b'')),
    }

def _noise(img):
    a = np.asarray(img.convert('RGB'), dtype=np.int16)
    return float(np.abs(np.diff(a, axis=0)).mean() + np.abs(np.diff(a, axis=1)).mean())

def _guess_format(hdr, first_tile=None):
    """Unknown format code: pick a codec from bytes-per-pixel; for 1 B/px try BC3/BC7/BC5
    on the first tile and keep the least noisy decode."""
    bpp = TILE_BYTES / float(hdr['tile_w'] * hdr['tile_h'])
    if abs(bpp - 0.5) < 1e-6:
        return (1, True, 'BC1?')
    if abs(bpp - 1.0) < 1e-6:
        if first_tile is None:
            return (7, True, 'BC7?')
        best = None
        for bcn, mode in ((3, 'RGBA'), (7, 'RGBA'), (5, 'RGB')):
            try:
                n = _noise(Image.frombytes(mode, (hdr['tile_w'], hdr['tile_h']), first_tile, 'bcn', bcn))
            except Exception:
                continue
            if best is None or n < best[0]:
                best = (n, bcn)
        return (best[1], best[1] != 5, 'BC%d?' % best[1]) if best else None
    return None

def decode_texture(path, max_size=0):
    """Return (PIL.Image RGBA, info). Picks the largest mip <= max_size (0 = full)."""
    hdr = read_texture_header(path)
    fmt = TEX_FORMATS.get(hdr['format'])
    if fmt is None:
        with open(path + 'mips', 'rb') as fh:
            fmt = _guess_format(hdr, fh.read(TILE_BYTES))
        if hdr['format'] not in _UNKNOWN_SEEN:
            _UNKNOWN_SEEN.add(hdr['format'])
            print('  ? unknown texture format code %s (%s) - guessed %s' % (hdr['format'], os.path.basename(path), fmt and fmt[2]))
    if fmt is None:
        raise ValueError('unsupported texture format %s' % hdr['format'])
    bcn, srgb, kind = fmt
    W, H = hdr['width'], hdr['height']
    starts = hdr['mip_tile_start']
    mip = 0
    if max_size:
        while mip + 1 < len(starts) and max(W >> mip, H >> mip) > max_size:
            mip += 1
    w, h = max(W >> mip, 1), max(H >> mip, 1)
    TW, TH = hdr['tile_w'], hdr['tile_h']
    ntx, nty = max(1, -(-w // TW)), max(1, -(-h // TH))
    mode = {4: 'L', 5: 'RGB'}.get(bcn, 'RGBA')
    with open(path + 'mips', 'rb') as fh:
        data = fh.read()
    img = Image.new(mode, (ntx * TW, nty * TH))
    for ty in range(nty):
        for tx in range(ntx):
            idx = starts[mip] + ty * ntx + tx
            tile = data[idx * TILE_BYTES:(idx + 1) * TILE_BYTES]
            if len(tile) < TILE_BYTES:
                tile = tile + b'\0' * (TILE_BYTES - len(tile))
            img.paste(Image.frombytes(mode, (TW, TH), tile, 'bcn', bcn), (tx * TW, ty * TH))
    img = img.crop((0, 0, w, h))
    if bcn == 5:                       # BC5 normal: rebuild Z
        a = np.asarray(img, dtype=np.float32) / 255.0
        x, y = a[..., 0] * 2 - 1, a[..., 1] * 2 - 1
        z = np.sqrt(np.clip(1 - x * x - y * y, 0, 1))
        a[..., 2] = z * 0.5 + 0.5
        img = Image.fromarray((a * 255 + 0.5).astype(np.uint8), 'RGB')
    return img, {'width': w, 'height': h, 'srgb': srgb, 'codec': kind,
                 'has_alpha': img.mode == 'RGBA' and img.getchannel('A').getextrema()[0] < 250}

# --------------------------------------------------------------------------
# meshes
# --------------------------------------------------------------------------
def read_mesh(path):
    """-> list of LODs: {'distance', 'pos','nrm','uv0','uv1','tan', 'idx', 'sections':[(start,count,material)]}"""
    top = pb(open(path, 'rb').read())
    lods = []
    for lb in top.get(5, []):
        d = pb(lb)
        pos = np.frombuffer(pb1(d, 5, b''), '<f4').reshape(-1, 3)
        nv = len(pos)
        def arr(f, n):
            b = pb1(d, f)
            if not b or len(b) != nv * n * 4:
                return None
            return np.frombuffer(b, '<f4').reshape(-1, n)
        idx = np.array(varints(pb1(d, 11, b'')), dtype=np.uint32)
        secs = []
        for sb in d.get(4, []):
            sd = pb(sb)
            start = pb1(sd, 2, 0)
            count = pb1(sd, 3, len(idx) - start)
            secs.append((start, count, s(pb1(sd, 4, b'')), s(pb1(sd, 1, b''))))
        if not secs:
            secs = [(0, len(idx), '', 'default')]
        lods.append({
            'distance': f32(pb1(d, 3)) if 3 in d else 0.0,
            'pos': pos, 'nrm': arr(6, 3), 'uv0': arr(7, 2), 'tan': arr(8, 4),
            'uv1': arr(14, 2), 'idx': idx, 'sections': secs,
        })
    return lods

# --------------------------------------------------------------------------
# materials
# --------------------------------------------------------------------------
def _param_value(b):
    d = pb(b)
    if 1 in d:
        return f32(d[1][0])
    for f, n in ((2, 2), (3, 3), (4, 4)):
        if f in d:
            return vec(d[f][0], n)
    return None

def read_material(path):
    d = pb(open(path, 'rb').read())
    params, textures = {}, {}
    for pbm in d.get(4, []):
        p = pb(pbm)
        name = s(pb1(p, 1, b''))
        v = _param_value(pb1(p, 2, b'')) if 2 in p else None
        if name:
            params[name] = v
    for tb in d.get(5, []):
        p = pb(tb)
        name = s(pb1(p, 1, b''))
        tp = pb(pb1(p, 2, b''))
        path_ = s(pb1(tp, 2, b''))
        if name and path_:
            textures[name] = path_
    return {'shader': s(pb1(d, 1, b'')), 'params': params, 'textures': textures}

SLOT_CANDIDATES = {
    'base_color': ['Base_BaseColorMap', 'BaseColorMap', 'txDiffuse', 'txAlbedo', 'AlbedoMap'],
    'normal':     ['Base_NormalMap', 'NormalMap', 'txNormal'],
    'roughness':  ['Base_RoughnessMap', 'RoughnessMap', 'txRoughness'],
    'metallic':   ['Base_MetalnessMap', 'MetalnessMap', 'txMetalness'],
    'ao':         ['Base_AmbientOcclusionMap', 'AmbientOcclusionMap', 'txAO'],
    'emissive':   ['Base_EmissiveMap', 'EmissiveMap', 'txEmissive'],
    'opacity':    ['Base_OpacityMap', 'OpacityMap', 'txOpacity'],
}
PLACEHOLDER_TEX = re.compile(r'editor[\\/]textures[\\/]default_material', re.I)

def _first(params, names, default=None):
    for n in names:
        v = params.get(n)
        if v is not None and v != 0 and v != [0, 0] and v != [0, 0, 0] and v != [0, 0, 0, 0]:
            return v
    return default

def simplify_material(m):
    """Collapse EVO's layered shader to a single PBR layer (the 'Base' layer)."""
    P, T = m['params'], m['textures']
    slots = {}
    dyn = m['shader'].lower().startswith('dynamictrack')
    for slot, names in SLOT_CANDIDATES.items():
        if dyn:
            names = [n for n in names if n.startswith('tx')] + [n for n in names if not n.startswith('tx')]
        for n in names:
            if T.get(n) and not PLACEHOLDER_TEX.search(T[n]):
                slots[slot] = T[n]; break
    col = _first(P, ['Base_Basecolor', 'Basecolor', 'ksBaseColor'], None)
    if 'base_color' in slots:
        factor = [1, 1, 1, 1]
    else:
        factor = (list(col) + [1, 1, 1, 1])[:4] if col else [0.6, 0.6, 0.6, 1]
        factor[3] = 1.0
    rough = _first(P, ['Base_Roughness', 'Roughness', 'ksRoughness'], 0.7)
    metal = _first(P, ['Base_Metalness', 'Metalness', 'ksMetalness'], 0.0)
    emis = _first(P, ['Base_EmissiveColor', 'EmissiveColor'], None)
    emis_i = _first(P, ['Base_EmissiveIntensity', 'EmissiveIntensity'], 1.0)
    uv = _first(P, ['Base_UVscale', 'UVscale'], [1, 1])
    nscale = _first(P, ['Base_NormalScale', 'NormalScale', 'ksNormalScale'], 1.0)
    blend = int(P.get('blendMode') or 0)          # 0 opaque, 1 alpha-blend, 2 alpha-test, 4 alpha-to-coverage
    shader = m['shader'].lower()
    alpha = 'OPAQUE'
    if blend in (2, 4) or shader == 'fence':
        alpha = 'MASK'
    elif blend == 1:
        alpha = 'BLEND'
    opacity = 1.0
    if P.get('UseOpacityOverride') and isinstance(P.get('OpacityOverride'), float):
        opacity = P['OpacityOverride']
    elif alpha == 'BLEND' and 'base_color' not in slots and 'opacity' not in slots:
        opacity = 0.35                              # untextured glass
    factor[3] = float(opacity)
    return {
        'textures': slots,
        'base_color_factor': [float(x) for x in factor],
        'roughness': float(rough if isinstance(rough, float) else 0.7),
        'metallic': float(metal if isinstance(metal, float) else 0.0),
        'emissive_factor': [float(x) * float(emis_i) for x in (emis[:3] if emis else [0, 0, 0])],
        'uv_scale': [float(uv[0] or 1), float(uv[1] or 1)] if isinstance(uv, list) else [1.0, 1.0],
        'normal_scale': abs(float(nscale)) if isinstance(nscale, float) else 1.0,
        'alpha_mode': alpha,
        'double_sided': int(P.get('cullMode') or 0) == 2,
        'alpha_cutoff': float(P.get('a2cCutoff') or 0.33),
        'blend_mode': blend,
    }

# --------------------------------------------------------------------------
# scenes
# --------------------------------------------------------------------------
COMP_SMESH, COMP_LIGHT, COMP_STARTPOS, COMP_ISMESH, COMP_SPLINE, COMP_CONTAINER = 100, 101, 104, 105, 108, 121

class SceneWalker:
    def __init__(self, res, track, all_containers=False, log=print):
        self.res, self.track, self.all_containers, self.log = res, track.lower(), all_containers, log
        self.static = []      # (mesh_path, M, name, scene)
        self.instanced = []   # (mesh_path, [M...], name, scene)
        self.starts = []      # (group, name, M)
        self.splines = []     # (group, name, [[x,y,z]...])
        self.lights = []
        self.skipped = Counter()
        self.missing = set()
        self._seen_scenes = set()

    def container_allowed(self, p):
        if self.all_containers:
            return True
        pl = p.replace('/', '\\').lower()
        return ('\\tracks\\%s\\' % self.track) in pl

    def walk(self, scene_path, M=None, depth=0):
        M = np.eye(4) if M is None else M
        real = self.res.resolve(scene_path)
        if real is None:
            self.missing.add(scene_path); return
        key = (real, tuple(np.round(M, 4).ravel()))
        if key in self._seen_scenes or depth > 8:
            return
        self._seen_scenes.add(key)
        data = open(real, 'rb').read()
        if not data:
            return
        try:
            top = pb(data)
        except ValueError:
            self.log('  ! could not parse %s' % scene_path); return
        group = stem(scene_path)
        for ob in top.get(2, []):
            o = pb(ob)
            name = s(pb1(o, 1, b''))
            otype = s(o[3][-1]) if 3 in o else ''
            Ml = trs_matrix(*read_transform(pb1(o, 4, b''))) if 4 in o else np.eye(4)
            Mw = M @ Ml
            comps = pb_parse(pb1(o, 50, b'')) if 50 in o else []
            if not comps:
                self.skipped[otype or '?'] += 1
                continue
            cid, _, payload = comps[0]
            c = pb(payload) if isinstance(payload, (bytes, bytearray)) else {}
            if cid == COMP_SMESH:
                mp = s(pb1(c, 1, b''))
                if mp.lower().endswith('.mesh'):
                    self.static.append((mp, Mw, name, group))
            elif cid == COMP_ISMESH:
                mp = s(pb1(c, 1, b''))
                inst = pb(pb1(c, 7, b''))
                P = np.frombuffer(pb1(inst, 2, b''), '<f4').reshape(-1, 3)
                n = len(P)
                R = np.frombuffer(pb1(inst, 3, b''), '<f4').reshape(-1, 3) if 3 in inst else np.zeros((n, 3))
                S = np.frombuffer(pb1(inst, 4, b''), '<f4').reshape(-1, 3) if 4 in inst else np.ones((n, 3))
                if len(R) != n: R = np.zeros((n, 3))
                if len(S) != n: S = np.ones((n, 3))
                mats = [Mw @ trs_matrix(P[i], R[i], S[i]) for i in range(n)]
                if mats and mp.lower().endswith('.mesh'):
                    self.instanced.append((mp, mats, name, group))
            elif cid == COMP_CONTAINER:
                sp = s(pb1(c, 1, b''))
                if sp.lower().endswith('.scene'):
                    if self.container_allowed(sp):
                        self.walk(sp, Mw, depth + 1)
                    else:
                        self.skipped['container:' + stem(sp)] += 1
            elif cid == COMP_STARTPOS:
                self.starts.append((group, name, Mw))
            elif cid == COMP_SPLINE:
                pts = []
                for pbk in pb(pb1(c, 1, b'')).get(3, []):
                    pk = pb(pbk)
                    pos, _, _ = read_transform(pb1(pk, 1, b''))
                    pts.append((Mw @ np.array(pos + [1.0]))[:3].tolist())
                if pts:
                    self.splines.append((otype or group, name, pts))
            elif cid == COMP_LIGHT:
                self.lights.append((name, Mw[:3, 3].tolist()))
            else:
                self.skipped[otype or str(cid)] += 1

# --------------------------------------------------------------------------
# glTF writer (minimal, binary .glb)
# --------------------------------------------------------------------------
class GLB:
    def __init__(self):
        self.j = {'asset': {'version': '2.0', 'generator': 'evo_track_export %s' % VERSION},
                  'scenes': [{'nodes': []}], 'scene': 0, 'nodes': [], 'meshes': [],
                  'accessors': [], 'bufferViews': [], 'buffers': [], 'materials': []}
        self.bin = bytearray()
        self.mat_index = {}
        self.img_index = {}

    def _view(self, data, target=None):
        while len(self.bin) % 4:
            self.bin.append(0)
        off = len(self.bin)
        self.bin += data
        bv = {'buffer': 0, 'byteOffset': off, 'byteLength': len(data)}
        if target:
            bv['target'] = target
        self.j['bufferViews'].append(bv)
        return len(self.j['bufferViews']) - 1

    def accessor(self, arr, gltf_type, target=34962, minmax=False):
        arr = np.ascontiguousarray(arr)
        comp = {np.dtype('float32'): 5126, np.dtype('uint32'): 5125, np.dtype('uint16'): 5123}[arr.dtype]
        acc = {'bufferView': self._view(arr.tobytes(), target), 'componentType': comp,
               'count': int(arr.shape[0]), 'type': gltf_type}
        if minmax:
            acc['min'] = arr.min(0).tolist(); acc['max'] = arr.max(0).tolist()
        self.j['accessors'].append(acc)
        return len(self.j['accessors']) - 1

    def image_texture(self, uri):
        if uri in self.img_index:
            return self.img_index[uri]
        self.j.setdefault('images', []).append({'uri': uri})
        self.j.setdefault('samplers', [{'magFilter': 9729, 'minFilter': 9987, 'wrapS': 10497, 'wrapT': 10497}])
        self.j.setdefault('textures', []).append({'source': len(self.j['images']) - 1, 'sampler': 0})
        self.img_index[uri] = len(self.j['textures']) - 1
        return self.img_index[uri]

    def material(self, name, pbr=None, tex_uri=None):
        if name in self.mat_index:
            return self.mat_index[name]
        m = {'name': name}
        if pbr:
            mr = {'baseColorFactor': pbr['base_color_factor'], 'metallicFactor': pbr['metallic'],
                  'roughnessFactor': pbr['roughness']}
            if tex_uri:
                t = pbr['textures']
                if t.get('base_color') and tex_uri(t['base_color']):
                    mr['baseColorTexture'] = {'index': self.image_texture(tex_uri(t['base_color']))}
                if t.get('normal') and tex_uri(t['normal']):
                    m['normalTexture'] = {'index': self.image_texture(tex_uri(t['normal']))}
            m['pbrMetallicRoughness'] = mr
            if pbr['alpha_mode'] != 'OPAQUE':
                m['alphaMode'] = pbr['alpha_mode']
            if pbr['double_sided']:
                m['doubleSided'] = True
            if any(pbr['emissive_factor']):
                m['emissiveFactor'] = [min(1.0, x) for x in pbr['emissive_factor']]
        self.j['materials'].append(m)
        self.mat_index[name] = len(self.j['materials']) - 1
        return self.mat_index[name]

    def mesh(self, name, lod, mat_for_section, with_uv1=True):
        prims = []
        P = lod['pos'].astype(np.float32)
        attrs = {'POSITION': self.accessor(P, 'VEC3', minmax=True)}
        if lod['nrm'] is not None:
            N = lod['nrm'].astype(np.float32)
            n = np.linalg.norm(N, axis=1, keepdims=True)
            N = np.where(n > 1e-6, N / np.where(n == 0, 1, n), np.array([0, 1, 0], np.float32))   # source has a few zero normals
            attrs['NORMAL'] = self.accessor(N.astype(np.float32), 'VEC3')
        if lod['uv0'] is not None:
            attrs['TEXCOORD_0'] = self.accessor(lod['uv0'].astype(np.float32), 'VEC2')
        if with_uv1 and lod['uv1'] is not None:
            attrs['TEXCOORD_1'] = self.accessor(lod['uv1'].astype(np.float32), 'VEC2')
        idx = lod['idx']
        for start, count, mpath, slot in lod['sections']:
            if count <= 0:
                continue
            sub = idx[start:start + count]
            sub = sub[:len(sub) - len(sub) % 3]
            if len(sub) == 0:
                continue
            prims.append({'attributes': attrs, 'indices': self.accessor(sub.astype(np.uint32), 'SCALAR', 34963),
                          'material': mat_for_section(mpath, slot)})
        if not prims:
            return None
        self.j['meshes'].append({'name': name, 'primitives': prims})
        return len(self.j['meshes']) - 1

    def node(self, name, mesh=None, M=None, children=None, root=True, extras=None):
        n = {'name': name}
        if mesh is not None:
            n['mesh'] = mesh
        if M is not None and not np.allclose(M, np.eye(4)):
            n['matrix'] = M.T.ravel().tolist()      # glTF is column-major
        if children:
            n['children'] = children
        if extras:
            n['extras'] = extras
        self.j['nodes'].append(n)
        i = len(self.j['nodes']) - 1
        if root:
            self.j['scenes'][0]['nodes'].append(i)
        return i

    def save(self, path):
        j = dict(self.j)
        for k in ('materials',):
            if not j[k]:
                del j[k]
        while len(self.bin) % 4:
            self.bin.append(0)
        j['buffers'] = [{'byteLength': len(self.bin)}]
        js = json.dumps(j, separators=(',', ':')).encode()
        js += b' ' * ((4 - len(js) % 4) % 4)
        with open(path, 'wb') as f:
            f.write(struct.pack('<III', 0x46546C67, 2, 12 + 8 + len(js) + 8 + len(self.bin)))
            f.write(struct.pack('<II', len(js), 0x4E4F534A)); f.write(js)
            f.write(struct.pack('<II', len(self.bin), 0x004E4942)); f.write(self.bin)

# --------------------------------------------------------------------------
# exporter
# --------------------------------------------------------------------------
COLLISION_HINT = re.compile(r'([\\/]physics[\\/]|collider|_collision)', re.I)

class ExportError(Exception):
    pass

class Cancelled(Exception):
    pass

def make_args(**kw):
    """Build an args namespace with CLI defaults (for GUI / scripting use)."""
    d = dict(max_texture=0, no_textures=False, lods=False, preview=False, preview_max_instances=2000,
             all_containers=False, raw_params=False, no_recenter=False, skip_existing=False)
    d.update(kw)
    return argparse.Namespace(**d)

def list_tracks(content_dir):
    """Track folder names under <content>/tracks that have a <name>.scene."""
    tdir = os.path.join(content_dir, 'tracks')
    out = []
    try:
        for n in sorted(os.listdir(tdir)):
            if os.path.isfile(os.path.join(tdir, n, n + '.scene')):
                out.append(n)
    except OSError:
        pass
    return out

class Exporter:
    def __init__(self, args, progress=None, logger=None, cancel=None):
        """progress(phase:str, done:int, total:int); logger(str); cancel() -> bool"""
        self.a = args
        self._progress = progress
        self._logger = logger
        self._cancel = cancel
        self.res = Resolver(args.content)
        self.out = os.path.abspath(args.out)
        self.track = args.track
        self.mesh_cache = {}        # evo path -> lods or None
        self.mesh_names = {}        # evo path(lower) -> unique asset name
        self.used_names = set()
        self.materials = {}         # evo path(lower) -> dict
        self.textures = {}          # evo path(lower) -> png name or None
        self.tex_names = set()
        self.warn = Counter()
        self.t0 = time.time()

    def log(self, *x):
        line = '[%6.1fs] ' % (time.time() - self.t0) + ' '.join(str(v) for v in x)
        if self._logger:
            self._logger(line)
        else:
            print(line, flush=True)

    def tick(self, phase, done, total):
        if self._cancel and self._cancel():
            raise Cancelled()
        if self._progress:
            self._progress(phase, done, total)

    # ---- naming ----
    def unique(self, base, used):
        n, i = safe_name(base), 1
        cand = n
        while cand.lower() in used:
            i += 1; cand = '%s_%d' % (n, i)
        used.add(cand.lower())
        return cand

    def mesh_name(self, p):
        k = p.lower().replace('/', '\\')
        if k not in self.mesh_names:
            self.mesh_names[k] = self.unique(stem(p), self.used_names)
        return self.mesh_names[k]

    # ---- loaders ----
    def load_mesh(self, p):
        k = p.lower().replace('/', '\\')
        if k not in self.mesh_cache:
            real = self.res.resolve(p)
            if not real:
                self.warn['missing mesh'] += 1; self.mesh_cache[k] = None
            else:
                try:
                    self.mesh_cache[k] = read_mesh(real)
                except Exception as e:
                    self.log('  ! mesh %s: %s' % (p, e)); self.warn['bad mesh'] += 1; self.mesh_cache[k] = None
        return self.mesh_cache[k]

    def load_material(self, p):
        k = (p or '').lower().replace('/', '\\')
        if k not in self.materials:
            real = self.res.resolve(p) if p else None
            info = {'name': self.unique(stem(p) if p else 'default', set(m['name'].lower() for m in self.materials.values())),
                    'source': p, 'shader': None, 'pbr': simplify_material({'shader': '', 'params': {}, 'textures': {}})}
            if real:
                try:
                    m = read_material(real)
                    info['shader'] = m['shader']
                    info['pbr'] = simplify_material(m)
                    if self.a.raw_params:
                        info['params'] = m['params']; info['all_textures'] = m['textures']
                except Exception as e:
                    self.log('  ! material %s: %s' % (p, e)); self.warn['bad material'] += 1
            elif p:
                self.warn['missing material'] += 1
            self.materials[k] = info
        return self.materials[k]

    def texture_png(self, p):
        """Decode EVO texture -> textures/<name>.png ; returns name (no ext) or None."""
        k = p.lower().replace('/', '\\')
        if k in self.textures:
            return self.textures[k]
        name = None
        real = self.res.resolve(p)
        if real and os.path.isfile(real + 'mips'):
            name = self.unique(stem(p).replace('.png', ''), self.tex_names)
            dst = os.path.join(self.out, 'textures', name + '.png')
            try:
                if not (self.a.skip_existing and os.path.exists(dst)):
                    img, info = decode_texture(real, self.a.max_texture)
                    img.save(dst, optimize=False, compress_level=3)
                else:
                    info = {'srgb': TEX_FORMATS.get(read_texture_header(real)['format'], (0, True))[1]}
                self.tex_meta[name] = {'source': p, 'srgb': info.get('srgb', True)}
            except Exception as e:
                self.log('  ! texture %s: %s' % (p, e)); self.warn['bad texture'] += 1; name = None
        else:
            self.warn['missing texture'] += 1
        self.textures[k] = name
        return name

    # ---- main ----
    def run(self):
        a = self.a
        os.makedirs(os.path.join(self.out, 'meshes'), exist_ok=True)
        if not a.no_textures:
            os.makedirs(os.path.join(self.out, 'textures'), exist_ok=True)
        self.tex_meta = {}
        self.write_helpers()
        tdir = 'content\\tracks\\%s' % self.track
        root_scene = None
        for cand in (tdir + '\\%s.scene' % self.track,):
            if self.res.resolve(cand):
                root_scene = cand
        if not root_scene:
            raise ExportError('Could not find tracks\\%s\\%s.scene under %s' % (self.track, self.track, self.res.content))
        self.log('EVO track exporter %s - track "%s"' % (VERSION, self.track))
        self.log('Walking scenes ...')
        self.tick('Reading scene', 0, 1)
        w = SceneWalker(self.res, self.track, a.all_containers, self.log)
        w.walk(root_scene)
        self.log('  %d static meshes, %d instanced groups (%d instances), %d start positions, %d splines'
                 % (len(w.static), len(w.instanced), sum(len(m) for _, m, _, _ in w.instanced), len(w.starts), len(w.splines)))

        # -- meshes --
        mesh_entries = {}
        all_paths = [p for p, *_ in w.static] + [p for p, *_ in w.instanced]
        uniq = list(dict.fromkeys(x.lower().replace('/', '\\') for x in all_paths))
        orig = {}
        for p in all_paths:
            orig.setdefault(p.lower().replace('/', '\\'), p)
        inst_keys = set(p.lower().replace('/', '\\') for p, *_ in w.instanced)
        self.log('Exporting %d unique meshes ...' % len(uniq))
        for n_i, k in enumerate(uniq):
            self.tick('Meshes', n_i, len(uniq))
            p = orig[k]
            lods = self.load_mesh(p)
            if not lods:
                continue
            name = self.mesh_name(p)
            collision = bool(COLLISION_HINT.search(p)) or all(
                (sec[2] or '').lower().replace('\\', '/').endswith('editor/default.material') or sec[3] == 'PHYSICS'
                for sec in lods[0]['sections'])
            entry = {'name': name, 'source': p, 'collision_only': collision, 'lods': [], 'slots': []}
            offset = np.zeros(3)
            if not a.no_recenter and k not in inst_keys and len(lods[0]['pos']):
                P0 = lods[0]['pos']
                offset = (P0.min(0) + P0.max(0)) * 0.5
                lods = [dict(l, pos=(l['pos'] - offset).astype(np.float32)) for l in lods]
                self.mesh_cache[k] = lods
            entry['pivot_offset'] = [float(x) for x in offset]
            entry['bounds'] = [lods[0]['pos'].min(0).tolist(), lods[0]['pos'].max(0).tolist()]
            nlods = len(lods) if a.lods else 1
            for li in range(nlods):
                lod = lods[li]
                g = GLB()
                slot_names = []
                def mat_for(mpath, slot, g=g, slot_names=slot_names):
                    mi = self.load_material(mpath)
                    if mi['name'] not in slot_names:
                        slot_names.append(mi['name'])
                    return g.material(mi['name'])
                mi = g.mesh(name if li == 0 else '%s_LOD%d' % (name, li), lod, mat_for)
                if mi is None:
                    continue
                g.node(name, mi)
                fn = name + ('' if li == 0 else '_LOD%d' % li) + '.glb'
                g.save(os.path.join(self.out, 'meshes', fn))
                entry['lods'].append({'file': 'meshes/' + fn, 'distance': lod['distance'],
                                      'vertices': int(len(lod['pos'])), 'triangles': int(len(lod['idx']) // 3)})
                if li == 0:
                    entry['slots'] = slot_names
            if entry['lods']:
                mesh_entries[k] = entry
            if (n_i + 1) % 100 == 0:
                self.log('  %d / %d' % (n_i + 1, len(uniq)))

        # -- textures --
        if not a.no_textures:
            needed = []
            for m in self.materials.values():
                needed += list(m['pbr']['textures'].values())
            needed = list(dict.fromkeys(needed))
            self.log('Decoding %d textures (max size %s) ...' % (len(needed), a.max_texture or 'full'))
            for i, tp in enumerate(needed):
                self.tick('Textures', i, len(needed))
                self.texture_png(tp)
                if (i + 1) % 50 == 0:
                    self.log('  %d / %d' % (i + 1, len(needed)))
        for m in self.materials.values():
            m['pbr']['texture_files'] = {slot: self.textures.get(tp.lower().replace('/', '\\'))
                                         for slot, tp in m['pbr']['textures'].items()}

        # -- manifest --
        def ue_xform(M):
            t, q, sc = mat_to_trs(M)
            return {'matrix': np.round(M, 6).tolist(), 't': t, 'q': q, 's': sc}
        placements = []
        for p, M, name, group in w.static:
            e = mesh_entries.get(p.lower().replace('/', '\\'))
            if e:
                T = np.eye(4); T[:3, 3] = e['pivot_offset']
                placements.append({'mesh': e['name'], 'name': name, 'group': group, **ue_xform(M @ T)})
        instances = []
        for p, mats, name, group in w.instanced:
            e = mesh_entries.get(p.lower().replace('/', '\\'))
            if e:
                ts = []
                for M in mats:
                    t, q, sc = mat_to_trs(M)
                    ts.append([round(x, 4) for x in t] + [round(x, 6) for x in q] + [round(x, 4) for x in sc])
                instances.append({'mesh': e['name'], 'name': name, 'group': group,
                                  'transform_layout': 'tx,ty,tz,qx,qy,qz,qw,sx,sy,sz', 'transforms': ts})
        manifest = {
            'format': 'evo_track_export', 'version': VERSION, 'track': self.track,
            'coordinate_system': 'right-handed, Y up, metres (glTF convention)',
            'meshes': sorted(mesh_entries.values(), key=lambda e: e['name']),
            'materials': sorted(self.materials.values(), key=lambda m: m['name']),
            'textures': self.tex_meta,
            'placements': placements, 'instances': instances,
            'start_positions': [{'group': g, 'name': n, **ue_xform(M)} for g, n, M in w.starts],
            'splines': [{'type': t, 'name': n, 'points': pts} for t, n, pts in w.splines],
            'lights': [{'name': n, 'position': p} for n, p in w.lights],
            'skipped_objects': dict(w.skipped), 'missing_files': sorted(w.missing),
            'warnings': dict(self.warn),
        }
        with open(os.path.join(self.out, 'track.json'), 'w') as f:
            json.dump(manifest, f, indent=1)

        # -- preview --
        if a.preview:
            self.log('Writing whole-track preview glb ...')
            self.tick('Preview', 0, 1)
            self.write_preview(manifest, mesh_entries)

        self.log('Done: %d meshes, %d materials, %d textures, %d placements, %d instance groups.'
                 % (len(mesh_entries), len(self.materials), len(self.tex_meta), len(placements), len(instances)))
        if self.warn:
            self.log('Warnings: %s' % dict(self.warn))
        ue = self.write_ue_script()
        if ue:
            self.log('Unreal import script ready: %s' % ue)
        self.tick('Done', 1, 1)
        self.summary = {'meshes': len(mesh_entries), 'materials': len(self.materials), 'textures': len(self.tex_meta),
                        'placements': len(placements), 'instance_groups': len(instances),
                        'instances': sum(len(i['transforms']) for i in instances),
                        'seconds': time.time() - self.t0, 'warnings': dict(self.warn), 'ue_script': ue}
        return manifest

    def write_ue_script(self):
        """Copy evo_ue_import.py next to the output with EXPORT_DIR filled in."""
        here = getattr(sys, '_MEIPASS', os.path.dirname(os.path.abspath(__file__)))
        src = os.path.join(here, 'evo_ue_import.py')
        if not os.path.isfile(src):
            return None
        txt = open(src, encoding='utf-8').read()
        line = 'EXPORT_DIR      = r"%s"' % self.out.replace('"', '')
        txt = re.sub(r'^EXPORT_DIR\s*=.*$', lambda _m: line, txt, count=1, flags=re.M)   # lambda: no escape parsing of Windows paths
        dst = os.path.join(self.out, 'import_into_unreal.py')
        with open(dst, 'w', encoding='utf-8') as f:
            f.write(txt)
        return dst

    def write_helpers(self):
        """_evo_axis_probe.glb: tetrahedron (0,0,0) (1,0,0) (0,2,0) (0,0,3) in glTF metres.
        The UE script imports it and reads its bounds to detect the importer's axis/unit
        conversion, so placements stay correct whatever the engine version does."""
        P = np.array([[0, 0, 0], [1, 0, 0], [0, 2, 0], [0, 0, 3]], np.float32)
        lod = {'pos': P, 'nrm': None, 'uv0': None, 'uv1': None,
               'idx': np.array([0, 2, 1, 0, 1, 3, 0, 3, 2, 1, 2, 3], np.uint32),
               'sections': [(0, 12, '', 'probe')]}
        g = GLB(); mi = g.mesh('_evo_axis_probe', lod, lambda mp, sl: g.material('M_probe'))
        g.node('_evo_axis_probe', mi); g.save(os.path.join(self.out, '_evo_axis_probe.glb'))
        if not self.a.no_textures:
            d = os.path.join(self.out, 'textures')
            Image.new('L', (4, 4), 255).save(os.path.join(d, '_evo_white_linear.png'))
            Image.new('RGB', (4, 4), (128, 128, 255)).save(os.path.join(d, '_evo_flat_normal.png'))

    def write_preview(self, manifest, mesh_entries):
        g = GLB()
        by_name = {e['name']: e for e in mesh_entries.values()}
        mats = {m['name']: m for m in manifest['materials']}
        def tex_uri_from_evo(tp):
            n = self.textures.get(tp.lower().replace('/', '\\'))
            return ('textures/%s.png' % n) if n else None
        mesh_idx = {}
        def get_mesh(name):
            if name in mesh_idx:
                return mesh_idx[name]
            e = by_name[name]
            lods = self.load_mesh(e['source'])
            def mat_for(mpath, slot):
                mi = self.load_material(mpath)
                return g.material(mi['name'], mi['pbr'], tex_uri_from_evo if not self.a.no_textures else None)
            mesh_idx[name] = g.mesh(name, lods[0], mat_for, with_uv1=False)
            return mesh_idx[name]
        groups = defaultdict(list)
        for pl in manifest['placements']:
            if by_name[pl['mesh']]['collision_only']:
                continue
            mi = get_mesh(pl['mesh'])
            if mi is not None:
                groups[pl['group']].append(g.node(pl['name'] or pl['mesh'], mi, np.array(pl['matrix']), root=False))
        for ig in manifest['instances']:
            if by_name[ig['mesh']]['collision_only']:
                continue
            mi = get_mesh(ig['mesh'])
            if mi is None:
                continue
            ts = ig['transforms']
            cap = self.a.preview_max_instances
            if cap and len(ts) > cap:                     # thin out huge groups (tyre walls, crowds)
                step = len(ts) / float(cap)
                ts = [ts[int(i * step)] for i in range(cap)]
            kids = [g.node('%s_%d' % (ig['mesh'], i), mi, tqs_to_matrix(t), root=False)
                    for i, t in enumerate(ts)]
            groups[ig['group']].append(g.node(ig['name'], children=kids, root=False))
        for grp, kids in groups.items():
            g.node(grp, children=kids)
        g.save(os.path.join(self.out, '%s_preview.glb' % self.track))

def main(argv=None):
    ap = argparse.ArgumentParser(description='Export an unpacked Assetto Corsa EVO track to glTF + PNG + JSON.')
    ap.add_argument('content', help=r'path to the unpacked EVO "content" folder')
    ap.add_argument('track', help='track folder name, e.g. laguna_seca')
    ap.add_argument('out', help='output folder')
    ap.add_argument('--max-texture', type=int, default=0, help='downscale textures to this size (uses game mips; 0 = full res)')
    ap.add_argument('--no-textures', action='store_true', help='skip texture decoding')
    ap.add_argument('--lods', action='store_true', help='also export LOD1+ meshes (<name>_LOD1.glb ...)')
    ap.add_argument('--preview', action='store_true', help='also write <track>_preview.glb (whole track, textured)')
    ap.add_argument('--preview-max-instances', type=int, default=2000, help='cap instances per group in the preview glb (0 = all)')
    ap.add_argument('--all-containers', action='store_true', help='follow container scenes outside the track folder (event props, cones)')
    ap.add_argument('--raw-params', action='store_true', help='include every raw material parameter in track.json')
    ap.add_argument('--no-recenter', action='store_true', help='keep EVO world-space pivots for static meshes')
    ap.add_argument('--skip-existing', action='store_true', help="don't re-decode PNGs that already exist")
    a = ap.parse_args(argv)
    if not os.path.isdir(a.content):
        sys.exit('content folder not found: %s' % a.content)
    try:
        Exporter(a).run()
    except ExportError as e:
        sys.exit(str(e))

if __name__ == '__main__':
    main()
