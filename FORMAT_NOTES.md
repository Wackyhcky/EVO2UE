# Assetto Corsa EVO track formats — reverse-engineering notes

Worked out from the unpacked Laguna Seca files (game build current at Oct 2026). Every
format below is **schema-less protobuf** (standard wire format, no length prefix or
magic). Field numbers are protobuf tags. "f32" = wire type 5 float, "vec3" = a
sub-message `{1: x, 2: y, 3: z}` of f32s where a missing field means 0 (proto3 default).

## Coordinate system

* Right-handed, **Y up**, metres — identical to glTF. (Verified: the track centre line
  measures 3,598 m against the real 3,602 m. The direction of travel is counter-clockwise
  with the Andretti Hairpin turning left and the Corkscrew going left-then-right, which
  only holds under the right-handed reading.)
* Triangle winding is **counter-clockwise** = front face (matches stored normals 99.7–100%).
* Euler angles are degrees, **R = Rz · Ry · Rx** (X applied first). Object forward is **+Z**
  (all 49 spawn points align with the centre-line tangent to 1.000).

## `.track`
`1: {…}` small header, `2: path to the dynamic-track preset`, `3: f32`. Not needed for geometry.

## `.scene`  (root `<track>.scene` and `containers/*.scene`)
```
1: string   repeated  - list of object type names used in the file
2: Object   repeated
3: Global   (sky/haze/terrain-variation settings)

Object:
  1: string  name
  2: varint  (enabled?)
  3: string  type ("SMesh", "ISMesh", "Container", "Spline"...); a second 3 may give a subtype
  4: Transform {1: vec3 position, 2: vec3 euler_deg, 3: vec3 scale}
  12: fixed64 id
  13: { 1:{x,z} 2:{x,z} 3:f32 }   2D bounds + cull distance
  14: f32 draw distance
  50: Component — exactly one entry, its field number is the component type:
      100 SMesh       {1: mesh path, 4: material overrides}
      101 Light
      104 Start position
      105 ISMesh      {1: mesh path, 7: {2: float3[] positions, 3: float3[] euler_deg, 4: float3[] scales}}
      108 Spline      {1: {3: repeated {1: Transform}}}   (point list)
      109 Surface definition (ROAD/WALL/… physics surfaces)
      111 Track info
      112 Zone (timelines, pit zones)
      118 Reflection probe, 120 Irradiance probe, 128 Decal, 135 Macro irradiance volume, 137 Marshal
      121 Container   {1: path to another .scene, 3: enabled}
```
Static meshes (SMesh) are authored **in world space** with identity transforms.

## `.mesh`
```
2,3: varint flags      4: f32 draw distance    6/7: vec3 bounds min/max
5: LOD   repeated (LOD0 first)
   1: varint
   3: f32   switch distance (m)   (absent on LOD0)
   4: Section repeated {1: name, 2: index start (absent = 0), 3: index count, 4: material path}
   5: float32[3] positions        6: float32[3] normals
   7: float32[2] UV0              8: float32[4] tangents (w = ±1)
   11: packed varint indices (triangle list)
   13: float32[4] vertex colour (only alpha used: baked AO in 0..1/255)
   14: float32[2] UV1             15: float32[2] world XZ (splat UV)   16: float32[2] UV3
   17/18: vec3 bounds min/max     22: varint
```

## `.material`
```
1: string  shader  (UberMaterial, UberBiplanarMaterial, DynamicTrack, DynamicTrackMultilayer,
                    Subsurface, Fence, InteriorMapping, Particle, …)
4: Param   repeated {1: name, 2: value}   value: {1: f32 | 2: vec2 | 3: vec3 | 4: vec4}
5: Texture repeated {1: slot name, 2: {2: texture path}}
```
Useful params: `blendMode` (0 opaque, 1 alpha blend, 2 alpha test, 4 alpha-to-coverage),
`cullMode` (2 = two-sided), `a2cCutoff`, `Base_UVscale`, `Base_Roughness`, `Base_Basecolor`.
Layered shaders use `Base_*`, `Red_*`, `Green_*`, `Blue_*` layers blended by a splat map;
`DynamicTrack*` shaders keep their real maps in `tx*` slots.

## `.texture` + `.texturemips`
Header (`.texture`):
```
1: width  2: height  3,4: mip count  6: varint
10: source info {1: original .png path, 2: w, 3: h, 6: import preset}
11: {1: FORMAT, 2: ?, 9: 1, 11: f32 0.5}
12: tiling {1: tile_w, 2: tile_h, 3: 1, 4: bytes[mip] first tile index, 5: bytes[mip] tile count, 7: packed sizes}
```
`.texturemips` is a sequence of **64 KiB tiles** (D3D12 standard tile size). Each mip uses
`ceil(w/tile_w) × ceil(h/tile_h)` tiles, row-major, starting at the index from field 12.4;
inside a tile, blocks are stored linearly (row-major). Small mips still take a whole tile.

| format (11.1) | codec | tile | notes |
|---|---|---|---|
| 0 | BC7 | 256×256 | sRGB colour |
| 1 | BC7 | 256×256 | linear (masks) |
| 2 | BC4 | 512×256 | single channel (roughness, AO, masks) |
| 3 | BC5 | 256×256 | normal map, Z rebuilt from XY |
| 5 | BC1 | 512×256 | linear (some normal maps) |
| 6 | BC3 | 256×256 | inferred: linear twin of 7 |
| 7 | BC3 | 256×256 | sRGB colour + alpha (foliage) |
| 8 | BC1 | 512×256 | sRGB colour |

Unknown codes fall back to a guess from bytes-per-pixel (the tile size gives it away).
