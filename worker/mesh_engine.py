"""Bounded CPU mesh generation, topology checks, STL and glTF 2.0 exports.

No GPU, arbitrary Python execution, model downloads, or optional meshing packages.
Sculpting uses a sampled distance field and indexed marching tetrahedra.
CAD booleans still use the Manifold kernel in cad_engine.
"""
import json
import math
import struct
from dataclasses import dataclass
from pathlib import Path

import numpy as np

MAX_FACES = 200_000
MAX_VERTICES = 120_000
MAX_CELLS = 450_000


def number(value, low=-10000, high=10000):
    result = float(value)
    if not math.isfinite(result) or not low <= result <= high:
        raise ValueError(f"Number must be finite and between {low} and {high}")
    return result


def vector(value, positive=False):
    if not isinstance(value, (list, tuple)) or len(value) != 3:
        raise ValueError("Expected three coordinates")
    return np.array([number(v, 0.001 if positive else -10000) for v in value], dtype=np.float64)


@dataclass
class Mesh:
    vertices: np.ndarray
    faces: np.ndarray

    def validate(self, connected=True):
        v, f = self.vertices, self.faces
        if v.ndim != 2 or v.shape[1] != 3 or not 4 <= len(v) <= MAX_VERTICES:
            raise ValueError("Mesh needs 4–120,000 XYZ vertices")
        if f.ndim != 2 or f.shape[1] != 3 or not 4 <= len(f) <= MAX_FACES:
            raise ValueError("Mesh needs 4–200,000 triangular faces")
        if not np.isfinite(v).all() or f.min() < 0 or f.max() >= len(v):
            raise ValueError("Mesh contains non-finite vertices or out-of-range indices")
        a, b, c = v[f[:, 0]], v[f[:, 1]], v[f[:, 2]]
        area = np.linalg.norm(np.cross(b-a, c-a), axis=1)
        if np.any(area <= 1e-12):
            raise ValueError("Mesh contains collapsed or zero-area triangles")
        canonical = np.sort(f, axis=1)
        if len(np.unique(canonical, axis=0)) != len(f):
            raise ValueError("Mesh contains duplicate triangles")
        edges = np.concatenate([f[:, [0, 1]], f[:, [1, 2]], f[:, [2, 0]]])
        keys = np.sort(edges, axis=1)
        _, counts = np.unique(keys, axis=0, return_counts=True)
        if np.any(counts != 2):
            raise ValueError("Mesh is not a closed two-manifold: each edge must have exactly two faces")
        n = len(v)
        if not np.array_equal(np.sort(edges[:, 0]*n + edges[:, 1]), np.sort(edges[:, 1]*n + edges[:, 0])):
            raise ValueError("Mesh faces have inconsistent winding")
        volume = float(np.einsum('ij,ij->i', a, np.cross(b, c)).sum()/6)
        if volume <= 1e-8:
            raise ValueError("Mesh must have positive enclosed volume and outward faces")
        # Component count includes disconnected shells; no silent dropped body parts.
        parent = np.arange(n)
        def find(i):
            while parent[i] != i:
                parent[i] = parent[parent[i]]
                i = parent[i]
            return i
        for x, y in edges:
            x, y = find(x), find(y)
            if x != y:
                parent[max(x, y)] = min(x, y)
        components = len({find(i) for i in np.unique(f)})
        if connected and components != 1:
            raise ValueError(f"Mesh has {components} disconnected shells; connect the parts or explicitly set allow_multiple=true")
        return {"vertices": int(n), "triangles": int(len(f)), "components": components,
                "watertight": True, "winding_consistent": True, "volume_mm3": volume,
                "size_mm": np.ptp(v, axis=0).tolist(),
                "checks": ["finite coordinates", "nondegenerate faces", "no duplicate faces",
                           "two faces per edge", "consistent winding", "positive volume", "shell count"],
                "not_checked": ["self intersections", "minimum local wall thickness", "printer supports", "visual fidelity"]}

    def grounded(self):
        v = self.vertices.copy()
        v[:, :2] -= (v[:, :2].min(axis=0) + v[:, :2].max(axis=0))/2
        v[:, 2] -= v[:, 2].min()
        return Mesh(v, self.faces.copy())

    def outward(self):
        a, b, c = (self.vertices[self.faces[:, i]] for i in range(3))
        if np.einsum('ij,ij->i', a, np.cross(b, c)).sum() < 0:
            self.faces = self.faces[:, [0, 2, 1]]
        return self


def indexed(spec):
    vertices, faces = spec.get('vertices'), spec.get('faces')
    if not isinstance(vertices, list) or not isinstance(faces, list):
        raise ValueError("mesh requires vertices and faces lists")
    if len(vertices) > MAX_VERTICES or len(faces) > MAX_FACES:
        raise ValueError("Indexed mesh exceeds the server mesh budget")
    raw_faces = np.asarray(faces, dtype=np.float64)
    if not np.isfinite(raw_faces).all() or np.any(raw_faces != np.floor(raw_faces)):
        raise ValueError("Face indices must be finite integers")
    mesh = Mesh(np.asarray(vertices, dtype=np.float64), raw_faces.astype(np.int64))
    mesh.validate(connected=not spec.get('allow_multiple', False))
    return mesh


def loft(spec):
    """Closed, capped elliptical rings along Z. Ring: [cx,cy,z,rx,ry]."""
    sections = spec.get('sections')
    if not isinstance(sections, list) or not 2 <= len(sections) <= 64:
        raise ValueError("loft needs 2–64 sections [cx,cy,z,rx,ry]")
    rings = np.array([[number(x) for x in r] for r in sections], dtype=float)
    if rings.shape != (len(sections), 5) or np.any(rings[:, 3:] <= 0) or np.any(np.diff(rings[:, 2]) <= 0):
        raise ValueError("Loft radii must be positive and section heights strictly increasing")
    segments = int(number(spec.get('segments', 48), 12, 96))
    # Linear ring interpolation is deliberate: exact sections, no overshoot.
    sub = int(number(spec.get('subdivisions', 3), 1, 8))
    rings = np.concatenate([np.array([a+(b-a)*t/sub for t in range(sub)])
                            for a, b in zip(rings[:-1], rings[1:])] + [rings[-1:]])
    angles = np.arange(segments)*2*np.pi/segments
    points = np.stack([rings[:, 0, None]+rings[:, 3, None]*np.cos(angles),
                       rings[:, 1, None]+rings[:, 4, None]*np.sin(angles),
                       np.broadcast_to(rings[:, 2, None], (len(rings), segments))], axis=-1).reshape(-1, 3)
    faces = []
    for j in range(len(rings)-1):
        for i in range(segments):
            a, b = j*segments+i, j*segments+(i+1)%segments
            faces.extend([[a, b, b+segments], [a, b+segments, a+segments]])
    bottom, top = len(points), len(points)+1
    points = np.concatenate([points, rings[[0, -1], :3]])
    for i in range(segments):
        nxt = (i+1)%segments
        faces.extend([[bottom, nxt, i], [top, (len(rings)-1)*segments+i, (len(rings)-1)*segments+nxt]])
    return Mesh(points, np.array(faces, dtype=np.int64))


def _rotation(angles):
    x, y, z = np.radians(vector(angles))
    cx, cy, cz, sx, sy, sz = np.cos(x), np.cos(y), np.cos(z), np.sin(x), np.sin(y), np.sin(z)
    return np.array([[cz, -sz, 0], [sz, cz, 0], [0, 0, 1]]) @ np.array([[cy, 0, sy], [0, 1, 0], [-sy, 0, cy]]) @ np.array([[1, 0, 0], [0, cx, -sx], [0, sx, cx]])


def sculpt(spec):
    """Smooth ellipsoids/capsules, then optional subtractive ellipsoids/capsules."""
    parts = spec.get('parts', [])
    if not isinstance(parts, list) or not 1 <= len(parts) <= 100:
        raise ValueError("sculpt requires 1–100 ellipsoid/capsule parts")
    blend = number(spec.get('blend', 1.2), 0, 20)
    prepared, lo, hi = [], [], []
    for p in parts:
        if not isinstance(p, dict):
            raise ValueError("Each sculpt part must be an object")
        subtract = p.get('op', 'add') == 'subtract'
        if p.get('shape', 'ellipsoid') == 'capsule':
            a, b = vector(p.get('a')), vector(p.get('b'))
            r = number(p.get('radius'), .05, 1000)
            r_end = number(p.get('radius_end', r), .05, 1000)
            prepared.append(('capsule', a, b, (r,r_end), subtract))
            if not subtract:
                lo.append(np.minimum(a, b)-max(r,r_end)); hi.append(np.maximum(a, b)+max(r,r_end))
        elif p.get('shape', 'ellipsoid') == 'ellipsoid':
            center, radii = vector(p.get('center')), vector(p.get('radii'), True)
            rot = _rotation(p.get('rotation', [0, 0, 0]))
            prepared.append(('ellipsoid', center, radii, rot, subtract))
            extent = np.abs(rot) @ radii
            if not subtract:
                lo.append(center-extent); hi.append(center+extent)
        else:
            raise ValueError("Sculpt parts support ellipsoid or capsule")
    if not lo:
        raise ValueError("Sculpt needs additive geometry")
    low, high = np.min(lo, axis=0), np.max(hi, axis=0)
    resolution = int(number(spec.get('resolution', 112), 32, 160))
    step = max(high-low)/(resolution-6)
    # Polynomial smooth union expands by at most blend/4 per merge.
    padding = blend*2 + step*3
    low -= padding; high += padding
    dims = np.ceil((high-low)/step).astype(int)+1
    if np.prod(dims-1) > MAX_CELLS:
        step *= (np.prod(dims-1)/MAX_CELLS)**(1/3)*1.02
        dims = np.ceil((high-low)/step).astype(int)+1
    axes = [low[i]+np.arange(dims[i])*step for i in range(3)]
    # Sparse broadcast arrays keep temporary storage bounded on Render Free.
    x, y, z = np.meshgrid(*axes, indexing='ij', sparse=True)
    field = np.full(tuple(dims), np.inf, dtype=np.float64)
    for subtract_pass in (False, True):
        for shape, a, b, param, subtract in prepared:
            if subtract != subtract_pass:
                continue
            px, py, pz = x-a[0], y-a[1], z-a[2]
            if shape == 'capsule':
                ab = b-a
                t = np.clip((px*ab[0]+py*ab[1]+pz*ab[2])/max(np.dot(ab, ab), 1e-12), 0, 1)
                dist = np.sqrt((px-t*ab[0])**2+(py-t*ab[1])**2+(pz-t*ab[2])**2)-(param[0]+t*(param[1]-param[0]))
            else:
                q = [px*param[0, i]+py*param[1, i]+pz*param[2, i] for i in range(3)]
                dist = (np.sqrt(sum((q[i]/b[i])**2 for i in range(3)))-1)*min(b)
            if subtract:
                field = np.maximum(field, -dist)
            elif blend > 0:
                h = np.maximum(blend-np.abs(field-dist), 0)/blend
                field = np.minimum(field, dist)-h*h*blend*.25
            else:
                field = np.minimum(field, dist)
    boundary = np.concatenate([field[0].ravel(), field[-1].ravel(), field[:, 0].ravel(),
                               field[:, -1].ravel(), field[:, :, 0].ravel(), field[:, :, -1].ravel()])
    if np.any(boundary <= 0):
        raise ValueError("Sculpt touches sampling bounds; reduce blend or separate fewer overlapping parts")
    return _march(field, low, step)


def _march(field, origin, step):
    # All cubes share the same 0–6 diagonal. Edges are welded by global grid IDs.
    dims = field.shape
    active = np.zeros(np.array(dims)-1, dtype=np.uint8)
    corners = [(0,0,0), (1,0,0), (1,1,0), (0,1,0), (0,0,1), (1,0,1), (1,1,1), (0,1,1)]
    for c in corners:
        active += (field[c[0]:dims[0]-1+c[0], c[1]:dims[1]-1+c[1], c[2]:dims[2]-1+c[2]] < 0)
    cubes = np.argwhere((active > 0) & (active < 8))
    if not len(cubes):
        raise ValueError("Sculpt produced no surface")
    grid_ids = np.ravel_multi_index((cubes[:, 0], cubes[:, 1], cubes[:, 2]), dims)
    corner_offsets = np.array([np.ravel_multi_index(c, dims) for c in corners])
    ids = grid_ids[:, None] + corner_offsets
    flat = field.ravel()
    edge_pairs = []
    for tetra in ((0,5,1,6), (0,1,2,6), (0,2,3,6), (0,3,7,6), (0,7,4,6), (0,4,5,6)):
        ti = ids[:, tetra]
        vals = flat[ti]
        mask = ((vals < 0)*(1 << np.arange(4))).sum(axis=1)
        for case in range(1, 15):
            rows = ti[mask == case]
            if not len(rows):
                continue
            inside = [i for i in range(4) if case & (1 << i)]
            outside = [i for i in range(4) if not case & (1 << i)]
            if len(inside) == 1 or len(outside) == 1:
                lone, others = (inside[0], outside) if len(inside) == 1 else (outside[0], inside)
                tri = [[lone, i] for i in others]
                # Orient via the inside-to-outside direction below, independent of tetra ordering.
                triangles = [tri]
            else:
                a, b = inside; c, d = outside
                triangles = [[[a,c], [a,d], [b,d]], [[a,c], [b,d], [b,c]]]
            for tri in triangles:
                pairs = rows[:, np.array(tri)]
                coords = np.stack(np.unravel_index(rows, dims), axis=-1).astype(float)
                direction = coords[:, outside].mean(axis=1)-coords[:, inside].mean(axis=1)
                p0 = np.stack(np.unravel_index(pairs[..., 0], dims), axis=-1).astype(float)
                p1 = np.stack(np.unravel_index(pairs[..., 1], dims), axis=-1).astype(float)
                w = flat[pairs[..., 0]]/(flat[pairs[..., 0]]-flat[pairs[..., 1]])
                pos = p0 + (p1-p0)*w[..., None]
                reverse = (np.cross(pos[:, 1]-pos[:, 0], pos[:, 2]-pos[:, 0])*direction).sum(axis=1) < 0
                pairs[reverse] = pairs[reverse][:, [0,2,1]]
                edge_pairs.append(pairs)
    pairs = np.concatenate(edge_pairs).reshape(-1, 2)
    if len(pairs)//3 > MAX_FACES:
        raise ValueError("Sculpt exceeds 200,000 triangles; lower resolution")
    # Isosurface exactly on a grid vertex: use the vertex's ID on all incident edges.
    denominator = np.abs(flat[pairs[:,0]]-flat[pairs[:,1]])
    for j in (0, 1):
        # Snap intersections within 0.1% of a cell edge to the grid vertex.
        # This avoids microscopic slivers collapsing when STL uses float32.
        near = np.abs(flat[pairs[:, j]]) <= denominator*0.001
        pairs[near] = pairs[near, j, None]
    pairs.sort(axis=1)
    unique, inverse = np.unique(pairs, axis=0, return_inverse=True)
    p0 = np.stack(np.unravel_index(unique[:, 0], dims), axis=-1).astype(float)
    p1 = np.stack(np.unravel_index(unique[:, 1], dims), axis=-1).astype(float)
    den = flat[unique[:, 0]]-flat[unique[:, 1]]
    w = np.divide(flat[unique[:, 0]], den, out=np.zeros(len(den)), where=den != 0)
    vertices = origin + (p0+(p1-p0)*w[:, None])*step
    faces = inverse.reshape(-1, 3)
    good = (faces[:, 0] != faces[:, 1]) & (faces[:, 1] != faces[:, 2]) & (faces[:, 2] != faces[:, 0])
    return Mesh(vertices, faces[good]).outward()


def human(spec):
    """An editable neutral standing figurine, not an anatomical scan."""
    height = number(spec.get('height', 180), 40, 500)
    build = number(spec.get('build', 1), .75, 1.35)
    pose = number(spec.get('arm_angle', 12), 0, 65)
    parts = []
    def e(center, radii):
        parts.append({'center': center, 'radii': radii})
    def limb(a, b, radius, radius_end=None):
        parts.append({'shape':'capsule', 'a':a, 'b':b, 'radius':radius, 'radius_end':radius if radius_end is None else radius_end})
    # Coordinates use a 180mm adult figure, about 7.5 heads tall, front toward -Y.
    e([0,0,105], [16*build,10,17]); e([0,0,121], [14*build,9,24])
    e([0,0,136], [20*build,10.5,17]); e([0,0,150], [5.5,5.5,10])
    e([0,-.5,168], [8.5,8,12]); e([0,-2.4,161.5], [6.3,5.7,6.8])
    e([0,-8,166], [1.7,2.7,3.3])
    for side in (-1,1):
        e([side*8.5,0,167], [1.5,2.6,3.5])
        hip, knee, ankle = [side*10,0,100], [side*11,-1,56], [side*12,1,12]
        limb(hip,knee,7.7*build,4.7); e([side*10.5,0,79], [7.7*build,7.7,19])
        limb(knee,ankle,4.6,2.9); e([side*11,1,37], [5.5,6.2,15])
        e([side*12,-5,6], [5.5,11,5.5])
        shoulder = [side*21*build,0,141]
        dx = math.sin(math.radians(pose))
        dz = math.cos(math.radians(pose))
        elbow = [shoulder[0]+side*(6+dx*29), -1, 141-dz*29]
        wrist = [elbow[0]+side*dx*26, -3, elbow[2]-dz*26]
        limb(shoulder,elbow,5.5*build,3.7); limb(elbow,wrist,3.8*build,2.3)
        e([wrist[0],-3,wrist[2]-4], [4,2.7,6])
        for i in range(4):
            x = wrist[0]+side*(i-1.5)*1.8
            limb([x,-3,wrist[2]-6], [x+side*.35,-3.6,wrist[2]-12+(abs(i-1.5))], .95)
        limb([wrist[0]-side*3,-3,wrist[2]-2], [wrist[0]-side*5,-4,wrist[2]-7], 1.6)
    factor = height/180
    for part in parts:
        for key in ('center','radii','a','b'):
            if key in part:
                part[key] = [v*factor for v in part[key]]
        for key in ('radius','radius_end'):
            if key in part:
                part[key] *= factor
    for side in (-1,1):
        parts.append({'op':'subtract','center':[side*3.5*factor,-7.6*factor,169*factor], 'radii':[2.2*factor,1.8*factor,1.4*factor]})
    return sculpt({'parts':parts, 'blend':2.2*factor, 'resolution':spec.get('resolution', 144)})


def build_shape(spec):
    shape = spec.get('shape', 'mesh')
    return {'mesh':indexed, 'loft':loft, 'sculpt':sculpt, 'human':human}[shape](spec)


def write_stl(path, mesh):
    a, b, c = (mesh.vertices[mesh.faces[:, i]] for i in range(3))
    normal = np.cross(b-a, c-a)
    normal /= np.linalg.norm(normal, axis=1)[:, None]
    rec = np.zeros(len(mesh.faces), dtype=[('n','<f4',3), ('v','<f4',(3,3)), ('attr','<u2')])
    rec['n'], rec['v'] = normal, np.stack([a,b,c], axis=1)
    Path(path).write_bytes(b'Velaris mesh; units millimetres'.ljust(80,b' ') + struct.pack('<I', len(rec)) + rec.tobytes())


def read_stl(path):
    data = Path(path).read_bytes()
    if len(data) < 84:
        raise ValueError('STL is truncated')
    count = struct.unpack_from('<I', data, 80)[0]
    if count < 4 or count > MAX_FACES or len(data) != 84+50*count:
        raise ValueError('STL length or triangle count is invalid')
    rec = np.frombuffer(data, offset=84, dtype=[('n','<f4',3), ('v','<f4',(3,3)), ('attr','<u2')])
    vertices, faces = np.unique(rec['v'].reshape(-1,3), axis=0, return_inverse=True)
    return Mesh(vertices.astype(float), faces.reshape(-1,3))


def write_glb(path, mesh, material=None):
    # glTF is right-handed, Y up, metres. CAD is Z up, millimetres.
    vertices = (mesh.vertices[:, [0,2,1]]*np.array([1,1,-1])/1000).astype('<f4')
    faces = mesh.faces.astype('<u4')
    # Split render vertices at sharp creases; preserve smooth normals on curves.
    from cad_engine import _corner_normals
    cross=np.cross(vertices[faces[:,1]]-vertices[faces[:,0]],vertices[faces[:,2]]-vertices[faces[:,0]])
    face_n=cross/np.maximum(np.linalg.norm(cross,axis=1)[:,None],1e-20)
    corners=_corner_normals(vertices,faces,face_n).reshape(-1,3).astype('<f4')
    packed=np.concatenate([vertices[faces.reshape(-1)],np.round(corners,6)],axis=1)
    unique,inverse=np.unique(packed,axis=0,return_inverse=True)
    vertices=unique[:,:3].astype('<f4');normal=unique[:,3:].astype('<f4');faces=inverse.reshape(-1,3).astype('<u4')
    material=material or {}
    if not isinstance(material,dict):raise ValueError('Model material must be an object')
    color=material.get('color',[.48,.66,.9])
    if isinstance(color,str):
        import re
        if not re.fullmatch(r'#?[0-9A-Fa-f]{6}',color):raise ValueError('Model color must be a six-digit hex color')
        color=color.lstrip('#');color=[int(color[i:i+2],16)/255 for i in (0,2,4)]
    if not isinstance(color,(list,tuple)) or len(color)!=3 or not all(isinstance(v,(int,float)) and math.isfinite(v) and 0<=v<=1 for v in color):raise ValueError('Model color needs three components between 0 and 1')
    metallic=number(material.get('metallic',0),0,1);roughness=number(material.get('roughness',.65),0,1)
    chunks = [vertices.tobytes(), normal.astype('<f4').tobytes(), faces.tobytes()]
    lengths = [len(c) for c in chunks]
    offsets = [0, lengths[0], lengths[0]+lengths[1]]
    binary = b''.join(chunks)
    meta = {'asset':{'version':'2.0','generator':'Beam mesh engine'}, 'scene':0,
            'scenes':[{'nodes':[0]}], 'nodes':[{'mesh':0}],
            'meshes':[{'primitives':[{'attributes':{'POSITION':0,'NORMAL':1},'indices':2,'material':0,'mode':4}]}],
            'materials':[{'name':'Porcelain','pbrMetallicRoughness':{'baseColorFactor':list(color)+[1], 'metallicFactor':metallic, 'roughnessFactor':roughness}}],
            'buffers':[{'byteLength':len(binary)}],
            'bufferViews':[{'buffer':0,'byteOffset':o,'byteLength':n,'target':34962 if i < 2 else 34963} for i,(o,n) in enumerate(zip(offsets,lengths))],
            'accessors':[{'bufferView':0,'componentType':5126,'count':len(vertices),'type':'VEC3','min':vertices.min(axis=0).tolist(),'max':vertices.max(axis=0).tolist()},
                         {'bufferView':1,'componentType':5126,'count':len(vertices),'type':'VEC3'},
                         {'bufferView':2,'componentType':5125,'count':faces.size,'type':'SCALAR'}],
            'extras':{'sourceUnits':'mm','exportUnits':'m','sourceUpAxis':'Z','exportUpAxis':'Y'}}
    encoded = json.dumps(meta, separators=(',',':')).encode()
    encoded += b' ' * (-len(encoded)%4)
    binary += b'\0' * (-len(binary)%4)
    total = 12+8+len(encoded)+8+len(binary)
    Path(path).write_bytes(struct.pack('<4sII',b'glTF',2,total)+struct.pack('<II',len(encoded),0x4E4F534A)+encoded+struct.pack('<II',len(binary),0x004E4942)+binary)


def read_glb(path):
    """Read the self-contained indexed primitive written by this engine."""
    data = Path(path).read_bytes()
    if len(data) < 28 or struct.unpack_from('<4sII', data) != (b'glTF',2,len(data)):
        raise ValueError('Invalid GLB header or length')
    jslen, kind = struct.unpack_from('<II',data,12)
    if kind != 0x4E4F534A or jslen%4 or 20+jslen+8 > len(data):
        raise ValueError('Invalid GLB JSON chunk')
    doc = json.loads(data[20:20+jslen])
    pos = 20+jslen
    blen, kind = struct.unpack_from('<II', data, pos)
    if kind != 0x004E4942 or pos+8+blen != len(data):
        raise ValueError('Invalid GLB binary chunk')
    binary = memoryview(data)[pos+8:]
    primitive = doc['meshes'][0]['primitives'][0]
    def array(index, typ, width):
        acc = doc['accessors'][index]; view = doc['bufferViews'][acc['bufferView']]
        count = acc['count']; offset = view.get('byteOffset',0)+acc.get('byteOffset',0)
        itemsize = np.dtype(typ).itemsize
        if offset < 0 or count < 0 or offset+count*width*itemsize > len(binary):
            raise ValueError('GLB accessor exceeds buffer')
        return np.frombuffer(binary,dtype=typ,count=count*width,offset=offset).reshape(-1,width).copy()
    v = array(primitive['attributes']['POSITION'],'<f4',3).astype(float)
    indices = array(primitive['indices'],'<u4',1).ravel()
    if len(indices)%3:
        raise ValueError('GLB has incomplete triangles')
    # Undo export coordinate transform to verify physical size in millimetres.
    v = v[:, [0,2,1]]*np.array([1,-1,1])*1000
    # Merge shading-only vertex splits before checking physical mesh topology.
    welded,inverse=np.unique(v,axis=0,return_inverse=True)
    return Mesh(welded,inverse[indices].reshape(-1,3).astype(np.int64))
