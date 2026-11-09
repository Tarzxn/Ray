"""Velaris CAD engine.

Turns a JSON "CAD program" written by a model into a guaranteed-watertight
solid using the Manifold geometry kernel (exact boolean union / difference /
intersection, extrusion, revolution, convex hull). Output is a binary STL plus
a shaded preview image.

Program formats accepted by ``build_program``:
  * {"plan": "...", "tree": NODE}          (preferred: a CSG tree)
  * {"plan": "...", "parts": [NODE, ...]}   (unioned together)
  * {"plan": "...", "ops": [...]}           (legacy add/repeat/mirror list)

NODE = shape | boolean | pattern, any of which may carry
  "position": [x,y,z]  "rotation": [rx,ry,rz] degrees (X, then Y, then Z)
  "scale": number | [sx,sy,sz]   "name": "label"
Units are millimetres, +Z is up. The finished model is centred in X/Y and set
on the build plate (min Z = 0).
"""
import math
import struct

import numpy as np

try:  # the kernel is a hard requirement of this module; callers fall back if absent
    import manifold3d as m3d
    from manifold3d import CrossSection, Manifold
    AVAILABLE = True
except Exception:  # pragma: no cover - exercised only when the wheel is missing
    AVAILABLE = False

MAX_NODES = 600
MAX_REPEAT = 200
MAX_TRIANGLES = 200_000
MAX_DEPTH = 24


class CADError(ValueError):
    pass


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------
def _num(value, default=None, name="value", minimum=None):
    if value is None:
        if default is None:
            raise CADError(f"missing {name}")
        return float(default)
    try:
        result = float(value)
    except (TypeError, ValueError):
        raise CADError(f"{name} must be a number, got {value!r}")
    if not math.isfinite(result):
        raise CADError(f"{name} must be finite")
    if minimum is not None and result < minimum:
        raise CADError(f"{name} must be at least {minimum}, got {result:g}")
    return result


def _vec(value, default=(0.0, 0.0, 0.0), name="vector", n=3):
    if value is None:
        return tuple(float(v) for v in default)
    if isinstance(value, (int, float)):
        return tuple(float(value) for _ in range(n))
    if isinstance(value, dict):
        keys = (("x", "width", "w", "length"), ("y", "depth", "d"), ("z", "height", "h", "thickness"))
        value = [next((value[k] for k in group if k in value), default[i]) for i, group in enumerate(keys)][:n]
    if not isinstance(value, (list, tuple)) or not value:
        raise CADError(f"{name} must be a list of {n} numbers")
    vals = [_num(v, name=name) for v in value][:n]
    while len(vals) < n:
        vals.append(float(default[len(vals)]))
    return tuple(vals)


def _segments(radius, explicit=None):
    if explicit is not None:
        return int(max(8, min(int(round(_num(explicit, name="segments"))), 256)))
    return int(max(32, min(112, round(28 + 7 * math.sqrt(max(radius, 0.1))))))


def _poly(points, name="points"):
    if not isinstance(points, (list, tuple)) or len(points) < 3:
        raise CADError(f"{name} needs at least 3 [x,y] points")
    out = []
    for p in points:
        if not isinstance(p, (list, tuple)) or len(p) < 2:
            raise CADError(f"{name} must be a list of [x,y] pairs")
        out.append((_num(p[0], name=name), _num(p[1], name=name)))
    return out


def _cross_section(spec):
    """spec: {"points": [...], "holes": [[...], ...]} -> CrossSection (even-odd)."""
    outer = _poly(spec.get("points"), "points")
    polys = [outer]
    for hole in spec.get("holes") or []:
        polys.append(_poly(hole, "hole"))
    section = CrossSection(polys, m3d.FillRule.EvenOdd)
    if section.is_empty():
        raise CADError("2D outline is empty or self-intersecting")
    return section


def _regular_polygon(sides, radius):
    sides = int(_num(sides, name="sides", minimum=3))
    return [(radius * math.cos(2 * math.pi * i / sides), radius * math.sin(2 * math.pi * i / sides)) for i in range(sides)]


# --------------------------------------------------------------------------
# shape builders (all return a Manifold centred on its own origin)
# --------------------------------------------------------------------------
def _box(spec):
    w, d, h = _vec(spec.get("size"), (20, 20, 20), "size")
    if min(w, d, h) <= 0:
        raise CADError("box size must be positive")
    return Manifold.cube([w, d, h], True)


def _rounded_box(spec):
    w, d, h = _vec(spec.get("size"), (20, 20, 20), "size")
    r = _num(spec.get("radius"), min(w, d, h) * 0.15, "radius", 0)
    r = min(r, min(w, d, h) / 2 - 1e-3)
    if r <= 1e-3:
        return Manifold.cube([w, d, h], True)
    seg = _segments(r, spec.get("segments") or 24)
    ball = Manifold.sphere(r, seg)
    pts = [ball.translate([sx * (w / 2 - r), sy * (d / 2 - r), sz * (h / 2 - r)])
           for sx in (1, -1) for sy in (1, -1) for sz in (1, -1)]
    return Manifold.batch_hull(pts) if hasattr(Manifold, "batch_hull") else pts[0].hull()


def _sphere(spec):
    r = _num(spec.get("radius", _num(spec.get("size"), 20, "size") / 2 if spec.get("size") is not None else 10), name="radius", minimum=0.01)
    return Manifold.sphere(r, _segments(r, spec.get("segments")))


def _cylinder(spec):
    r = _num(spec.get("radius", _num(spec.get("size"), 20, "size") / 2 if spec.get("size") is not None else 10), name="radius", minimum=0.01)
    h = _num(spec.get("height", spec.get("size", 20)), name="height", minimum=0.01)
    top = spec.get("radius_top")
    return Manifold.cylinder(h, r, -1 if top is None else _num(top, name="radius_top", minimum=0), _segments(r, spec.get("segments")), True)


def _cone(spec):
    r = _num(spec.get("radius", 10), name="radius", minimum=0.01)
    h = _num(spec.get("height", spec.get("size", 20)), name="height", minimum=0.01)
    return Manifold.cylinder(h, r, 0.0, _segments(r, spec.get("segments")), True)


def _tube(spec):
    outer = _num(spec.get("radius", spec.get("outer_radius", 15)), name="radius", minimum=0.02)
    inner = _num(spec.get("inner_radius", spec.get("inner", outer * 0.6)), name="inner_radius", minimum=0)
    if inner >= outer:
        raise CADError("tube inner_radius must be smaller than radius")
    h = _num(spec.get("height", 20), name="height", minimum=0.01)
    seg = _segments(outer, spec.get("segments"))
    body = Manifold.cylinder(h, outer, -1, seg, True)
    if inner <= 0:
        return body
    return body - Manifold.cylinder(h + 2, inner, -1, seg, True)


def _torus(spec):
    major = _num(spec.get("radius", 20), name="radius", minimum=0.02)
    minor = _num(spec.get("tube", spec.get("minor_radius", 5)), name="tube", minimum=0.01)
    if minor >= major:
        raise CADError("torus tube must be smaller than radius")
    seg = _segments(major, spec.get("segments"))
    circle = CrossSection.circle(minor, max(16, min(seg // 2, 48))).translate([major, 0])
    return Manifold.revolve(circle, seg)


def _capsule(spec):
    r = _num(spec.get("radius", 8), name="radius", minimum=0.01)
    h = _num(spec.get("height", 0), name="height", minimum=0)
    seg = _segments(r, spec.get("segments"))
    top = Manifold.sphere(r, seg).translate([0, 0, h / 2])
    bottom = Manifold.sphere(r, seg).translate([0, 0, -h / 2])
    return Manifold.batch_hull([top, bottom]) if hasattr(Manifold, "batch_hull") else top.hull()


def _wedge(spec):
    w, d, h = _vec(spec.get("size"), (20, 20, 20), "size")
    # triangle in the XZ plane (slope down along +x), extruded along Y
    section = CrossSection([[(-w / 2, -h / 2), (w / 2, -h / 2), (-w / 2, h / 2)]])
    solid = Manifold.extrude(section, d).rotate([90, 0, 0])  # extrude axis Z -> Y
    return solid.translate([0, d / 2, 0])


def _pyramid(spec):
    size = _num(spec.get("size", 20), name="size", minimum=0.01) if not isinstance(spec.get("size"), (list, tuple)) else _vec(spec.get("size"))[0]
    h = _num(spec.get("height", size), name="height", minimum=0.01)
    return Manifold.cylinder(h, size / math.sqrt(2), 0.0, 4, True).rotate([0, 0, 45])


def _prism(spec):
    r = _num(spec.get("radius", 10), name="radius", minimum=0.01)
    h = _num(spec.get("height", 10), name="height", minimum=0.01)
    pts = _regular_polygon(spec.get("sides", 6), r)
    section = _round_section(CrossSection([pts]), _num(spec.get("round"), 0, "round", 0))
    return Manifold.extrude(section, h).translate([0, 0, -h / 2])


def _extrude(spec):
    h = _num(spec.get("height", 10), name="height", minimum=0.01)
    section = _round_section(_cross_section(spec), _num(spec.get("round"), 0, "round", 0))
    twist = _num(spec.get("twist"), 0, "twist")
    scale_top = _num(spec.get("scale_top"), 1, "scale_top", 0)
    solid = Manifold.extrude(section, h, int(max(0, min(spec.get("divisions", 24 if twist else 0), 200))), twist, (scale_top, scale_top))
    return solid.translate([0, 0, -h / 2])


def _revolve(spec):
    """Lathe a [radius, z] profile around the Z axis (vases, knobs, cups, bottles)."""
    profile = _poly(spec.get("profile"), "profile")
    if any(p[0] < 0 for p in profile):
        raise CADError("revolve profile radius values must be >= 0")
    section = CrossSection([profile])
    if section.is_empty():
        raise CADError("revolve profile is empty or self-intersecting")
    r = max(p[0] for p in profile)
    solid = Manifold.revolve(section, int(max(16, min(_num(spec.get("segments"), _segments(r), "segments"), 256))))
    box = solid.bounding_box()
    # keep the profile's own z coordinates, but centre vertically like other shapes
    return solid.translate([0, 0, -(box[2] + box[5]) / 2])


def _gear(spec):
    teeth = int(_num(spec.get("teeth"), 16, "teeth", 6))
    r = _num(spec.get("radius", 20), name="radius", minimum=1)
    depth = _num(spec.get("tooth_depth"), r * 0.15, "tooth_depth", 0.1)
    h = _num(spec.get("height", 8), name="height", minimum=0.01)
    if depth >= r * 0.6:
        raise CADError("gear tooth_depth is too large for its radius")
    half = math.pi / teeth
    pts = []
    for i in range(teeth):
        a = 2 * math.pi * i / teeth
        for ang, rad in ((a - half * 0.55, r - depth), (a - half * 0.28, r), (a + half * 0.28, r), (a + half * 0.55, r - depth)):
            pts.append((rad * math.cos(ang), rad * math.sin(ang)))
    return Manifold.extrude(CrossSection([pts]), h).translate([0, 0, -h / 2])


def _star(spec):
    points = int(_num(spec.get("points"), 5, "points", 3))
    outer = _num(spec.get("radius", 20), name="radius", minimum=0.1)
    inner = _num(spec.get("inner_radius"), outer * 0.45, "inner_radius", 0.05)
    h = _num(spec.get("height", 6), name="height", minimum=0.01)
    pts = [((outer if i % 2 == 0 else inner) * math.cos(math.pi * i / points + math.pi / 2),
            (outer if i % 2 == 0 else inner) * math.sin(math.pi * i / points + math.pi / 2)) for i in range(points * 2)]
    return Manifold.extrude(CrossSection([pts]), h).translate([0, 0, -h / 2])


def _round_section(section, radius, segments=24):
    """Round the convex corners of a 2D outline (morphological opening)."""
    r = max(0.0, float(radius))
    if r <= 1e-6:
        return section
    rounded = section.offset(-r, m3d.JoinType.Round, 2.0, segments).offset(r, m3d.JoinType.Round, 2.0, segments)
    return section if rounded.is_empty() else rounded


def _plate(spec):
    """Flat slab with rounded corners: tags, signs, bases, brackets, phone stands."""
    w, d, h = _vec(spec.get("size"), (40, 20, 3), "size")
    if min(w, d, h) <= 0:
        raise CADError("plate size must be positive")
    r = _num(spec.get("corner_radius", spec.get("radius")), min(w, d) * 0.12, "corner_radius", 0)
    r = min(r, min(w, d) / 2 - 1e-3)
    section = _round_section(CrossSection([[(-w / 2, -d / 2), (w / 2, -d / 2), (w / 2, d / 2), (-w / 2, d / 2)]]), r)
    return Manifold.extrude(section, h).translate([0, 0, -h / 2])


def _text(spec):
    """Real 3D lettering from vector glyph outlines (DejaVu Sans), lying in XY and extruded up Z."""
    from matplotlib.font_manager import FontProperties
    from matplotlib.textpath import TextPath
    text = str(spec.get("text", ""))[:60]
    if not text.strip():
        raise CADError("text is empty")
    size = _num(spec.get("size"), 10, "size", 1)
    h = _num(spec.get("height"), 2, "height", 0.2)
    weight = "bold" if spec.get("bold", True) else "normal"
    path = TextPath((0, 0), text, size=size, prop=FontProperties(family="DejaVu Sans", weight=weight))
    polys = [[(float(x), float(y)) for x, y in poly] for poly in path.to_polygons(closed_only=True) if len(poly) >= 3]
    if not polys:
        raise CADError("the text has no printable outlines")
    section = CrossSection(polys, m3d.FillRule.EvenOdd)
    if section.is_empty():
        raise CADError("the text has no printable outlines")
    x0, y0, x1, y1 = section.bounds()
    align = str(spec.get("align", "center")).lower()
    dx = -(x0 + x1) / 2 if align == "center" else (-x0 if align == "left" else -x1)
    section = section.translate([dx, -(y0 + y1) / 2])
    return Manifold.extrude(section, h).translate([0, 0, -h / 2])


def _catmull_rom(points, closed, sub=8):
    n = len(points)
    out = []
    count = n if closed else n - 1
    for i in range(count):
        p0, p1, p2, p3 = (points[(i - 1) % n], points[i], points[(i + 1) % n], points[(i + 2) % n]) if closed else (
            points[max(i - 1, 0)], points[i], points[i + 1], points[min(i + 2, n - 1)])
        for k in range(sub):
            t = k / sub
            t2, t3 = t * t, t * t * t
            out.append(tuple(0.5 * ((2 * p1[j]) + (-p0[j] + p2[j]) * t + (2 * p0[j] - 5 * p1[j] + 4 * p2[j] - p3[j]) * t2 + (-p0[j] + 3 * p1[j] - 3 * p2[j] + p3[j]) * t3) for j in range(3)))
    if not closed:
        out.append(tuple(points[-1]))
    return out


def _sweep_points(points, radius, radius_end, closed, seg):
    """Solid wire of constant (or tapering) radius along a 3D polyline: hulls of
    consecutive spheres, fused. Used for handles, rails, hooks, springs, pipes."""
    if len(points) < 2:
        raise CADError("a path needs at least 2 points")
    if len(points) > 260:
        step = len(points) / 260.0
        points = [points[int(i * step)] for i in range(260)] + [points[-1]]
    r_end = radius if radius_end is None else radius_end
    last = len(points) - 1
    balls = []
    for i, p in enumerate(points):
        r = radius + (r_end - radius) * (i / max(last, 1))
        balls.append(Manifold.sphere(max(r, 0.05), seg).translate(list(p)))
    pairs = [Manifold.batch_hull([balls[i], balls[i + 1]]) for i in range(last)]
    if closed:
        pairs.append(Manifold.batch_hull([balls[last], balls[0]]))
    return _union(pairs)


def _path(spec):
    pts = spec.get("points")
    if not isinstance(pts, list) or len(pts) < 2:
        raise CADError("path needs 'points': [[x,y,z], ...]")
    points = [_vec(p, name="path point") for p in pts][:120]
    closed = bool(spec.get("closed", False))
    if spec.get("smooth", True) and len(points) >= 3:
        points = _catmull_rom(points, closed, int(max(2, min(_num(spec.get("detail"), 8, "detail"), 16))))
    r = _num(spec.get("radius", 2), name="radius", minimum=0.05)
    r_end = None if spec.get("radius_end") is None else _num(spec.get("radius_end"), name="radius_end", minimum=0.05)
    solid = _sweep_points(points, r, r_end, closed, 12 if r < 3 else 16)
    box = solid.bounding_box()
    return solid.translate([-(box[0] + box[3]) / 2, -(box[1] + box[4]) / 2, -(box[2] + box[5]) / 2]) if spec.get("center", False) else solid


def _spring(spec):
    """Coil spring / screw thread helix around the Z axis."""
    R = _num(spec.get("radius", 10), name="radius", minimum=0.2)
    wire = _num(spec.get("wire", spec.get("wire_radius", 1.2)), name="wire", minimum=0.1)
    pitch = _num(spec.get("pitch", 4), name="pitch", minimum=wire * 2.05)
    turns = _num(spec.get("turns", 6), name="turns", minimum=0.5)
    if turns > 40:
        raise CADError("spring has too many turns (max 40)")
    steps = int(max(12, min(turns * 14, 240)))
    points = [(R * math.cos(2 * math.pi * turns * i / steps), R * math.sin(2 * math.pi * turns * i / steps), pitch * turns * i / steps) for i in range(steps + 1)]
    solid = _sweep_points(points, wire, None, False, 12)
    box = solid.bounding_box()
    return solid.translate([0, 0, -(box[2] + box[5]) / 2])


SHAPES = {
    "plate": _plate, "text": _text, "path": _path, "wire": _path, "spring": _spring, "coil": _spring,
    "gear": _gear, "star": _star,
    "box": _box, "cube": _box, "rounded_box": _rounded_box, "sphere": _sphere, "cylinder": _cylinder,
    "cone": _cone, "tube": _tube, "pipe": _tube, "torus": _torus, "capsule": _capsule, "wedge": _wedge,
    "pyramid": _pyramid, "prism": _prism, "extrude": _extrude, "revolve": _revolve, "lathe": _revolve,
}


# --------------------------------------------------------------------------
# tree evaluation
# --------------------------------------------------------------------------
class _Context:
    def __init__(self):
        self.warnings = []
        self.nodes = 0

    def warn(self, text):
        if len(self.warnings) < 25:
            self.warnings.append(text)


def _transform(solid, spec):
    scale = spec.get("scale")
    if scale is not None:
        sx, sy, sz = _vec(scale, (1, 1, 1), "scale")
        if min(abs(sx), abs(sy), abs(sz)) < 1e-6:
            raise CADError("scale cannot be zero")
        solid = solid.scale([sx, sy, sz])
    rotation = _vec(spec.get("rotation"), name="rotation")
    if any(rotation):
        solid = solid.rotate(list(rotation))
    anchor = str(spec.get("anchor", "center")).lower()
    if anchor in ("bottom", "base", "floor"):
        solid = solid.translate([0, 0, -solid.bounding_box()[2]])
    elif anchor == "top":
        solid = solid.translate([0, 0, -solid.bounding_box()[5]])
    position = _vec(spec.get("position"), name="position")
    if any(position):
        solid = solid.translate(list(position))
    return solid


def _union(parts):
    parts = [p for p in parts if p is not None and not p.is_empty()]
    if not parts:
        return None
    return parts[0] if len(parts) == 1 else Manifold.batch_boolean(parts, m3d.OpType.Add)


def _eval_children(spec, ctx, depth, key="children"):
    children = spec.get(key)
    if children is None and key == "children":
        children = spec.get("parts")
    if not isinstance(children, list) or not children:
        raise CADError(f"'{spec.get('op', 'group')}' needs a non-empty '{key}' list")
    out = []
    for index, child in enumerate(children):
        try:
            out.append(_eval(child, ctx, depth + 1))
        except (ValueError, TypeError) as error:
            raise CADError(f"{spec.get('op', 'group')} child {index + 1} failed: {error}") from error
    return out


_SHAPE_ALIASES = {"cube": "box", "rect": "box", "cuboid": "box", "block": "box", "ball": "sphere", "round_box": "rounded_box",
                  "roundedbox": "rounded_box", "disk": "cylinder", "disc": "cylinder", "rod": "cylinder", "pipe": "tube", "ring": "tube",
                  "donut": "torus", "label": "text", "lettering": "text", "slab": "plate", "helix": "spring", "polygon": "prism"}
_OP_ALIASES = {"merge": "union", "combine": "union", "join": "union", "fuse": "union", "minus": "difference", "subtraction": "difference",
               "remove": "difference", "carve": "difference", "intersect": "intersection", "bevel": "chamfer", "fillets": "round",
               "hollow": "shell", "array": "grid", "pattern": "repeat", "symmetry": "mirror"}


def _normalize(spec):
    """Accept the common ways models misspell the CAD schema: type/kind -> shape,
    diameter -> radius, width/depth/height -> size, dims -> size, pos -> position..."""
    spec = dict(spec)
    # Normalize common model-produced boolean syntax without discarding children.
    if isinstance(spec.get('boolean'),str) and 'op' not in spec:spec['op']=spec.pop('boolean')
    if str(spec.get('shape',spec.get('type',''))).lower() in ('union','difference','intersection','hull'):
        spec['op']=spec.pop('shape',spec.pop('type',''))
    if 'base' in spec and 'children' not in spec and str(spec.get('op','')).lower() in ('difference','subtract','cut'):
        cutters=spec.get('cutters',spec.get('tools',[]))
        if isinstance(cutters,dict):cutters=[cutters]
        spec['children']=[spec['base']]+cutters
    if "shape" not in spec and isinstance(spec.get("type"), str):
        spec["shape"] = spec.pop("type")
    if "op" not in spec and isinstance(spec.get("operation"), str):
        spec["op"] = spec.pop("operation")
    if isinstance(spec.get("shape"), str):
        spec["shape"] = _SHAPE_ALIASES.get(spec["shape"].lower().strip(), spec["shape"].lower().strip())
    if isinstance(spec.get("op"), str):
        spec["op"] = _OP_ALIASES.get(spec["op"].lower().strip(), spec["op"].lower().strip())
    for src in ("dimensions", "dims", "extents"):
        if src in spec and "size" not in spec:
            spec["size"] = spec.pop(src)
    for src in ("pos", "location", "at", "offset_position", "center"):
        if src in spec and "position" not in spec and isinstance(spec[src], (list, tuple)):
            spec["position"] = spec.pop(src)
    for src in ("rot", "angle", "orientation"):
        if src in spec and "rotation" not in spec and isinstance(spec[src], (list, tuple)):
            spec["rotation"] = spec.pop(src)
    if spec.get("shape") and "translate" in spec and "position" not in spec and isinstance(spec["translate"], (list, tuple)):
        spec["position"] = spec.pop("translate")
    if "radius" not in spec:
        for src, factor in (("diameter", 0.5), ("d", 0.5), ("r", 1.0)):
            if src in spec and isinstance(spec[src], (int, float, str)):
                try: spec["radius"] = float(spec[src]) * factor; break
                except ValueError: pass
    if "inner_radius" not in spec and "inner_diameter" in spec:
        try: spec["inner_radius"] = float(spec["inner_diameter"]) / 2
        except (TypeError, ValueError): pass
    if "size" not in spec and spec.get("shape") in ("box", "rounded_box", "plate", "wedge"):
        w = spec.get("width", spec.get("length", spec.get("w", spec.get("x"))))
        d = spec.get("depth", spec.get("d", spec.get("y")))
        h = spec.get("height", spec.get("thickness", spec.get("h", spec.get("z"))))
        if w is not None or d is not None or h is not None:
            spec["size"] = [w if w is not None else 20, d if d is not None else 20, h if h is not None else 20]
    if "height" not in spec and "h" in spec and spec.get("shape") in ("cylinder", "cone", "tube", "prism", "extrude", "gear", "star", "text", "capsule"):
        spec["height"] = spec["h"]
    if "children" not in spec and spec.get('shape') != 'sculpt':
        for src in ("items", "components", "nodes", "shapes", "parts", "operands", "elements"):
            if isinstance(spec.get(src), list):
                spec["children"] = spec.pop(src); break
    if "child" not in spec and isinstance(spec.get("target"), dict):
        spec["child"] = spec.pop("target")
    return spec


def _eval(spec, ctx, depth=0):
    if depth > MAX_DEPTH:
        raise CADError("model nesting is too deep")
    ctx.nodes += 1
    if ctx.nodes > MAX_NODES:
        raise CADError("model has too many parts")
    if isinstance(spec, list):
        spec = {"op": "union", "children": spec}
    if not isinstance(spec, dict):
        raise CADError("each part must be an object")
    spec = _normalize(spec)

    op = str(spec.get("op", "")).lower()
    shape = str(spec.get("shape", "")).lower()

    if shape and op in ("", "add"):
        if shape in ("mesh", "loft", "sculpt", "human"):
            import mesh_engine
            try:
                mesh = mesh_engine.build_shape(spec)
                mesh.validate(connected=not spec.get("allow_multiple", False))
                solid = Manifold(m3d.Mesh(np.asarray(mesh.vertices, dtype=np.float32), np.asarray(mesh.faces, dtype=np.uint32)))
                if solid.status() != m3d.Error.NoError or solid.is_empty():
                    raise CADError(f"Mesh could not become a solid: {solid.status()}")
            except (ValueError, TypeError) as error:
                raise CADError(str(error)) from error
            return _transform(solid, spec)
        builder = SHAPES.get(shape)
        if builder is None:
            raise CADError(f"unknown shape '{shape}' (use: {', '.join(sorted(set(SHAPES) - {'cube', 'pipe', 'lathe'}))})")
        solid = builder(spec)
        bore = spec.get("bore")
        if bore:  # legacy/shortcut: round hole through a shape along an axis
            solid = _apply_bore(solid, bore)
        return _transform(solid, spec)

    if op in ("union", "group", "add", "assembly"):
        solid = _union(_eval_children(spec, ctx, depth))
        if solid is None:
            raise CADError("group produced no geometry")
        return _transform(solid, spec)

    if op in ("difference", "subtract", "cut"):
        kids = _eval_children(spec, ctx, depth)
        if len(kids) < 1:
            raise CADError("difference has no base shape")
        first_ok = spec.get("children", spec.get("parts"))[0]
        base = kids[0]
        cutter = _union(kids[1:])
        solid = base - cutter if cutter is not None else base
        if solid.is_empty():
            raise CADError("difference removed everything — the cutters swallow the base")
        return _transform(solid, spec)

    if op in ("intersection", "intersect", "common"):
        kids = _eval_children(spec, ctx, depth)
        if len(kids) < 2:
            raise CADError("intersection needs two or more shapes")
        solid = kids[0]
        for other in kids[1:]:
            solid = solid ^ other
        if solid.is_empty():
            raise CADError("intersection is empty — the shapes don't overlap")
        return _transform(solid, spec)

    if op in ("round", "fillet", "soften"):
        child = _eval(spec.get("child") or (spec.get("children") or [None])[0], ctx, depth + 1)
        solid = _rounded(child, _num(spec.get("radius"), 1.5, "radius", 0.05), str(spec.get("mode", "outer")).lower(), ctx)
        return _transform(solid, spec)

    if op == "chamfer":
        child = _eval(spec.get("child") or (spec.get("children") or [None])[0], ctx, depth + 1)
        solid = _rounded(child, _num(spec.get("size", spec.get("radius")), 1.0, "size", 0.05), "outer", ctx, chamfer=True)
        return _transform(solid, spec)

    if op == "shell":
        child = _eval(spec.get("child") or (spec.get("children") or [None])[0], ctx, depth + 1)
        return _transform(_shell(child, _num(spec.get("thickness"), 2.0, "thickness", 0.2), spec.get("open")), spec)

    if op == "grid":
        child = _eval(spec.get("child") or (spec.get("children") or [None])[0], ctx, depth + 1)
        nx, ny, nz = [int(max(1, min(_num(v, 1, "counts"), MAX_REPEAT))) for v in _vec(spec.get("counts"), (2, 2, 1), "counts")]
        if nx * ny * nz > 500:
            raise CADError("grid has too many copies (max 500)")
        dx, dy, dz = _vec(spec.get("spacing"), (10, 10, 10), "spacing")
        copies = [child.translate([i * dx, j * dy, k * dz]) for i in range(nx) for j in range(ny) for k in range(nz)]
        solid = _union(copies)
        if spec.get("center", True):
            b = solid.bounding_box()
            solid = solid.translate([-(nx - 1) * dx / 2, -(ny - 1) * dy / 2, -(nz - 1) * dz / 2])
        return _transform(solid, spec)

    if op == "hull":
        kids = _eval_children(spec, ctx, depth)
        solid = Manifold.batch_hull(kids) if hasattr(Manifold, "batch_hull") else _union(kids).hull()
        return _transform(solid, spec)

    if op == "repeat":
        child = _eval(spec.get("child") or (spec.get("children") or [None])[0], ctx, depth + 1)
        count = int(max(1, min(_num(spec.get("count"), 2, "count"), MAX_REPEAT)))
        translate, rotate, pivot = _vec(spec.get("translate")), _vec(spec.get("rotate")), _vec(spec.get("around"))
        copies = []
        for i in range(count):
            piece = child
            if any(rotate):
                piece = piece.translate([-pivot[0], -pivot[1], -pivot[2]]).rotate([a * i for a in rotate]).translate(list(pivot))
            if any(translate):
                piece = piece.translate([a * i for a in translate])
            copies.append(piece)
        return _transform(_union(copies), spec)

    if op == "mirror":
        child = _eval(spec.get("child") or (spec.get("children") or [None])[0], ctx, depth + 1)
        axis = str(spec.get("axis", "x")).lower()
        if axis not in ("x", "y", "z"):
            raise CADError("mirror axis must be x, y or z")
        normal = {"x": [1, 0, 0], "y": [0, 1, 0], "z": [0, 0, 1]}[axis]
        offset = _num(spec.get("offset"), 0, "offset")
        shift = [offset * 2 * n for n in normal]
        reflected = child.mirror(normal).translate(shift)
        solid = _union([child, reflected]) if spec.get("keep", True) else reflected
        return _transform(solid, spec)

    raise CADError(f"unknown node (needs 'shape' or 'op'): {sorted(spec)[:6]}")



def _ball(radius, seg):
    """Convex ball with vertices exactly on the ±X/±Y/±Z axes, so offsetting by it
    keeps flat faces at their exact distances (dimensions never shrink or grow)."""
    lon = max(8, (int(seg) + 3) // 4 * 4)
    lat = max(4, (lon // 2 + 1) // 2 * 2)
    points = [[0.0, 0.0, radius], [0.0, 0.0, -radius]]
    for i in range(1, lat):
        phi = math.pi * i / lat
        for j in range(lon):
            theta = 2 * math.pi * j / lon
            points.append([radius * math.sin(phi) * math.cos(theta), radius * math.sin(phi) * math.sin(theta), radius * math.cos(phi)])
    return Manifold.hull_points(points)


def _rounded(solid, radius, mode, ctx, chamfer=False):
    """Fillet outer edges (opening) and/or inner edges (closing) with a ball
    (or an octahedron for chamfers), halving the radius until the shape keeps
    its volume — so thin parts are never eaten."""
    tris = solid.num_tri()
    if tris > 24000:
        ctx.warn("⚠️ A part was too detailed to round — left sharp.")
        return solid
    seg = 14 if tris < 3000 else 10 if tris < 9000 else 8
    original = solid.volume()
    r = radius
    for _ in range(4):
        ball = (Manifold.hull_points([[r, 0, 0], [-r, 0, 0], [0, r, 0], [0, -r, 0], [0, 0, r], [0, 0, -r]]) if chamfer
                else _ball(r, seg))
        out = solid
        if mode in ("outer", "both", "convex"):
            eroded = out.minkowski_difference(ball)
            if eroded.is_empty() or eroded.volume() < 0.35 * original:
                r /= 2; continue
            out = eroded.minkowski_sum(ball)
        if mode in ("inner", "both", "concave"):
            out = out.minkowski_sum(ball).minkowski_difference(ball)
        if not out.is_empty() and out.status() == m3d.Error.NoError and abs(out.volume() - original) < 0.2 * original:
            if r < radius:
                ctx.warn(f"ℹ️ Rounding radius reduced to {r:.2f} mm so thin parts survive.")
            return out
        r /= 2
    ctx.warn("ℹ️ A part is too thin to round — left sharp.")
    return solid


def _shell(solid, thickness, opening):
    """Hollow a solid to a uniform wall thickness, optionally open on one side."""
    if solid.num_tri() > 24000:
        raise CADError("shape is too detailed to shell")
    ball = _ball(thickness, 12)
    inner = solid.minkowski_difference(ball)
    if inner.is_empty():
        raise CADError(f"shell thickness {thickness:g} mm is too large for this shape")
    result = solid - inner
    side = str(opening or "").lower()
    if side in ("top", "bottom", "left", "right", "front", "back", "+z", "-z", "+x", "-x", "+y", "-y"):
        ib = inner.bounding_box()
        big = 5000.0
        e = 0.01
        if side in ("top", "+z"):
            cutter = Manifold.cube([ib[3] - ib[0] + 2 * e, ib[4] - ib[1] + 2 * e, big]).translate([ib[0] - e, ib[1] - e, ib[5] - e])
        elif side in ("bottom", "-z"):
            cutter = Manifold.cube([ib[3] - ib[0] + 2 * e, ib[4] - ib[1] + 2 * e, big]).translate([ib[0] - e, ib[1] - e, ib[2] + e - big])
        elif side in ("right", "+x"):
            cutter = Manifold.cube([big, ib[4] - ib[1] + 2 * e, ib[5] - ib[2] + 2 * e]).translate([ib[3] - e, ib[1] - e, ib[2] - e])
        elif side in ("left", "-x"):
            cutter = Manifold.cube([big, ib[4] - ib[1] + 2 * e, ib[5] - ib[2] + 2 * e]).translate([ib[0] + e - big, ib[1] - e, ib[2] - e])
        elif side in ("back", "+y"):
            cutter = Manifold.cube([ib[3] - ib[0] + 2 * e, big, ib[5] - ib[2] + 2 * e]).translate([ib[0] - e, ib[4] - e, ib[2] - e])
        else:
            cutter = Manifold.cube([ib[3] - ib[0] + 2 * e, big, ib[5] - ib[2] + 2 * e]).translate([ib[0] - e, ib[1] + e - big, ib[2] - e])
        result = result - cutter
    if result.is_empty():
        raise CADError("shell produced nothing")
    return result


def _apply_bore(solid, bore):
    if not isinstance(bore, dict):
        raise CADError("bore must be an object")
    box = solid.bounding_box()
    axis = str(bore.get("axis", "z")).lower()
    radius = _num(bore.get("radius"), name="bore radius", minimum=0.01)
    extent = max(box[3] - box[0], box[4] - box[1], box[5] - box[2]) + 4
    hole = Manifold.cylinder(extent, radius, -1, _segments(radius, bore.get("segments")), True)
    if axis == "x":
        hole = hole.rotate([0, 90, 0])
    elif axis == "y":
        hole = hole.rotate([90, 0, 0])
    cx, cy, cz = ((box[0] + box[3]) / 2, (box[1] + box[4]) / 2, (box[2] + box[5]) / 2)
    return solid - hole.translate([cx, cy, cz])


def _legacy_ops_to_tree(ops, ctx):
    """Old add/repeat/mirror programs -> a tree, fused by a real union."""
    nodes = []
    if not isinstance(ops,list) or not 1 <= len(ops) <= 200:
        raise CADError('Legacy program must contain 1–200 steps; split a larger model')
    for index, op in enumerate(ops):
        if not isinstance(op, dict):
            raise CADError(f'Step {index+1}: each step must be an object')
        kind = str(op.get("op", "add")).lower()
        if kind == "add":
            node = {k: v for k, v in op.items() if k != "op"}
            nodes.append(node)
        elif kind in ("repeat", "mirror") and nodes:
            previous = nodes.pop()
            wrapper = {k: v for k, v in op.items()}
            wrapper["child"] = previous
            nodes.append(wrapper)
        else:
            raise CADError(f"Step {index+1}: unsupported operation '{kind}' or nothing to act on")
    return {"op": "union", "children": nodes}


# --------------------------------------------------------------------------
# public API
# --------------------------------------------------------------------------
def build_program(spec, *, ground=True):
    """-> (Manifold, notes[list[str]], info{dict}). Raises CADError when nothing usable results."""
    if not AVAILABLE:
        raise CADError("CAD kernel (manifold3d) is not installed")
    ctx = _Context()
    if isinstance(spec, list):
        spec = {"ops": spec}
    if not isinstance(spec, dict):
        raise CADError("3D program must be a JSON object")
    if spec.get("tree") is not None:
        tree = spec["tree"]
    elif spec.get("shape") or spec.get("op"):
        tree = spec
    elif spec.get("parts"):
        tree = {"op": "union", "children": spec["parts"]}
    elif spec.get("ops"):
        tree = _legacy_ops_to_tree(spec["ops"], ctx)
    elif spec.get("shapes"):
        tree = {"op": "union", "children": spec["shapes"]}
    elif spec.get("shape") or spec.get("op"):
        tree = spec
    else:
        raise CADError("3D program needs a 'tree', 'parts' or 'ops'")

    solid = _eval(tree, ctx)
    if solid is None or solid.is_empty():
        raise CADError("the model has no geometry")

    box = solid.bounding_box()
    size = (box[3] - box[0], box[4] - box[1], box[5] - box[2])
    biggest = max(size)
    notes = list(ctx.warnings)
    if biggest < .01 or biggest > 20000:
        raise CADError(f'Model spans {biggest:.5g} mm. Use explicit mm/cm/in units and keep the span within 0.01–20,000 mm; it was not rescaled.')
    # centre on X/Y, sit on the build plate
    if ground:
        solid = solid.translate([-(box[0] + box[3]) / 2, -(box[1] + box[4]) / 2, -box[2]])

    mesh = solid.to_mesh()
    if len(mesh.tri_verts) > MAX_TRIANGLES:
        raise CADError(f"model is too detailed ({len(mesh.tri_verts):,} triangles)")
    if solid.status() != m3d.Error.NoError:
        raise CADError(f"geometry is invalid ({solid.status()})")
    volume = solid.volume()
    if volume <= 1e-6:
        raise CADError("the model has no volume")

    box = solid.bounding_box()
    info = {
        "size": (box[3] - box[0], box[4] - box[1], box[5] - box[2]),
        "volume_mm3": volume, "surface_mm2": solid.surface_area(),
        "triangles": int(len(mesh.tri_verts)), "genus": int(solid.genus()),
        "pieces": int(solid.num_components()) if hasattr(solid, "num_components") else 1,
    }
    notes.append(
        f"ℹ️ 3D model: {info['size'][0]:.1f} × {info['size'][1]:.1f} × {info['size'][2]:.1f} mm, "
        f"{info['volume_mm3'] / 1000:.2f} cm³, {info['triangles']:,} triangles, watertight (verified)"
        + (f", {info['genus']} through-hole{'s' if info['genus'] != 1 else ''}" if info["pieces"] == 1 and info["genus"] > 0 else "")
        + (f", {info['pieces']} separate pieces" if info["pieces"] > 1 else "") + ".")
    # 2V/A is not a minimum wall-thickness measurement. Do not present it as one.
    if max(info["size"]) > 256:
        notes.append("ℹ️ At least one dimension is over 256 mm, larger than many hobby 3D-printer beds — scale it down or split it for printing.")
    if info["pieces"] > 1:
        notes.append("⚠️ The model is made of separate, unconnected pieces — if it should be one object, make the parts overlap so they fuse.")
    return solid, notes, info


def mesh_arrays(solid):
    mesh = solid.to_mesh()
    verts = np.asarray(mesh.vert_properties, dtype=np.float64)[:, :3]
    tris = np.asarray(mesh.tri_verts, dtype=np.int64)
    return verts, tris


def write_binary_stl(path, solid):
    verts, tris = mesh_arrays(solid)
    a, b, c = verts[tris[:, 0]], verts[tris[:, 1]], verts[tris[:, 2]]
    normals = np.cross(b - a, c - a)
    lengths = np.linalg.norm(normals, axis=1, keepdims=True)
    normals = np.divide(normals, lengths, out=np.zeros_like(normals), where=lengths > 0)
    record = np.zeros(len(tris), dtype=[("n", "<f4", 3), ("v", "<f4", (3, 3)), ("attr", "<u2")])
    record["n"] = normals
    record["v"] = np.stack([a, b, c], axis=1)
    with open(path, "wb") as handle:
        handle.write(b"Velaris CAD (binary STL, mm)".ljust(80, b" "))
        handle.write(struct.pack("<I", len(tris)))
        handle.write(record.tobytes())


def verify_binary_stl(path):
    """Independent re-check of the written file: size, finite numbers, closed surface, positive volume."""
    data = open(path, "rb").read()
    if len(data) < 84:
        raise CADError("STL is too small")
    count = struct.unpack("<I", data[80:84])[0]
    if len(data) != 84 + 50 * count or count < 4:
        raise CADError("STL size doesn't match its triangle count")
    rec = np.frombuffer(data[84:], dtype=[("n", "<f4", 3), ("v", "<f4", (3, 3)), ("attr", "<u2")])
    v = rec["v"].astype(np.float64)
    if not np.isfinite(v).all():
        raise CADError("STL contains invalid numbers")
    keys = np.round(v.reshape(-1, 3), 4)
    _, inverse = np.unique(keys, axis=0, return_inverse=True)
    tri = inverse.reshape(-1, 3)
    edges = np.concatenate([tri[:, [0, 1]], tri[:, [1, 2]], tri[:, [2, 0]]])
    directed = edges[:, 0].astype(np.int64) * (inverse.max() + 1) + edges[:, 1]
    reverse = edges[:, 1].astype(np.int64) * (inverse.max() + 1) + edges[:, 0]
    if not np.array_equal(np.sort(directed), np.sort(reverse)):
        raise CADError("STL surface is not closed (open or non-manifold edges)")
    a, b, c = v[:, 0], v[:, 1], v[:, 2]
    volume = float(np.einsum("ij,ij->i", a, np.cross(b, c)).sum() / 6)
    if volume <= 0:
        raise CADError("STL faces point inward (negative volume)")
    return count, volume


# --------------------------------------------------------------------------
# preview rendering (pure numpy + Pillow, no GPU or display needed)
# --------------------------------------------------------------------------
def _corner_normals(verts, tris, face_n, threshold_deg=38.0):
    """Smooth shading with a crease angle: each triangle corner averages the
    normals of the faces around its vertex that are within the threshold of its
    own face, so curved surfaces look smooth while hard edges stay crisp."""
    n_tri = len(tris)
    order = np.argsort(tris.reshape(-1), kind="stable")
    sorted_vertices = tris.reshape(-1)[order]
    face_of = order // 3
    starts = np.searchsorted(sorted_vertices, np.arange(len(verts)))
    ends = np.searchsorted(sorted_vertices, np.arange(len(verts)), side="right")
    max_deg = int((ends - starts).max()) if len(verts) else 1
    max_deg = min(max_deg, 24)
    pad = np.full((len(verts), max_deg), -1, dtype=np.int64)
    for k in range(max_deg):
        idx = starts + k
        valid = idx < ends
        pad[valid, k] = face_of[idx[valid]]
    cos_t = math.cos(math.radians(threshold_deg))
    out = np.zeros((n_tri, 3, 3))
    for corner in range(3):
        for start in range(0, n_tri, 8192):
            end = min(start+8192,n_tri)
            neigh = pad[tris[start:end, corner]]
            ok = neigh >= 0
            nn = face_n[np.where(ok, neigh, 0)]
            agree = (nn @ face_n[start:end, :, None]).squeeze(-1) > cos_t
            summed = (nn * (ok & agree)[:, :, None]).sum(axis=1)
            length = np.linalg.norm(summed, axis=1, keepdims=True)
            out[start:end, corner] = np.divide(summed, length, out=face_n[start:end].copy(), where=length > 1e-12)
    return out


def _render_view(verts, tris, yaw_deg, pitch_deg, width, height, ss=2, ground=True, fit=0.74, color=None):
    """Z-buffered, smooth-shaded render of one view; returns a PIL image."""
    from PIL import Image

    centre = (verts.max(axis=0) + verts.min(axis=0)) / 2
    v = verts - centre
    yaw, pitch = math.radians(yaw_deg), math.radians(pitch_deg)
    rz = np.array([[math.cos(yaw), -math.sin(yaw), 0], [math.sin(yaw), math.cos(yaw), 0], [0, 0, 1]])
    rx = np.array([[1, 0, 0], [0, math.cos(pitch), -math.sin(pitch)], [0, math.sin(pitch), math.cos(pitch)]])
    rot = rx @ rz
    p = v @ rot.T                      # x right, y = depth (larger = farther), z up
    tri_p = p[tris]
    face_n = np.cross(tri_p[:, 1] - tri_p[:, 0], tri_p[:, 2] - tri_p[:, 0])
    ln = np.linalg.norm(face_n, axis=1, keepdims=True)
    face_n = np.divide(face_n, ln, out=np.zeros_like(face_n), where=ln > 0)
    corner_n = _corner_normals(p, tris, face_n)
    key = np.array([-0.45, -0.8, 0.55]); key /= np.linalg.norm(key)
    fill = np.array([0.6, -0.3, 0.2]); fill /= np.linalg.norm(fill)
    corner_shade = (np.clip(corner_n @ key, 0, 1) * 0.68 + np.clip(corner_n @ fill, 0, 1) * 0.18 + 0.22)

    W, H = width * ss, height * ss
    extent = max(np.ptp(p[:, 0]), np.ptp(p[:, 2]), 1e-6)
    scale = fit * min(W, H) / extent
    sx = W / 2 + p[:, 0] * scale
    sy = H / 2 - p[:, 2] * scale + 0.03 * H
    zbuf = np.full((H, W), np.inf)
    shade_buf = np.zeros((H, W)); cover = np.zeros((H, W), dtype=bool)

    facing = face_n[:, 1] < 0.02       # camera looks along +y: keep faces whose normal points back at it
    for t in np.nonzero(facing)[0]:
        i0, i1, i2 = tris[t]
        x0, y0, x1, y1, x2, y2 = sx[i0], sy[i0], sx[i1], sy[i1], sx[i2], sy[i2]
        minx, maxx = int(max(math.floor(min(x0, x1, x2)), 0)), int(min(math.ceil(max(x0, x1, x2)), W - 1))
        miny, maxy = int(max(math.floor(min(y0, y1, y2)), 0)), int(min(math.ceil(max(y0, y1, y2)), H - 1))
        if minx > maxx or miny > maxy:
            continue
        den = (y1 - y2) * (x0 - x2) + (x2 - x1) * (y0 - y2)
        if abs(den) < 1e-9:
            continue
        gx, gy = np.meshgrid(np.arange(minx, maxx + 1) + 0.5, np.arange(miny, maxy + 1) + 0.5)
        l0 = ((y1 - y2) * (gx - x2) + (x2 - x1) * (gy - y2)) / den
        l1 = ((y2 - y0) * (gx - x2) + (x0 - x2) * (gy - y2)) / den
        l2 = 1 - l0 - l1
        inside = (l0 >= -1e-9) & (l1 >= -1e-9) & (l2 >= -1e-9)
        if not inside.any():
            continue
        depth = l0 * p[i0, 1] + l1 * p[i1, 1] + l2 * p[i2, 1]
        sh = l0 * corner_shade[t, 0] + l1 * corner_shade[t, 1] + l2 * corner_shade[t, 2]
        window = zbuf[miny:maxy + 1, minx:maxx + 1]
        win_sh = shade_buf[miny:maxy + 1, minx:maxx + 1]
        win_cov = cover[miny:maxy + 1, minx:maxx + 1]
        nearer = inside & (depth < window)
        window[nearer] = depth[nearer]; win_sh[nearer] = sh[nearer]; win_cov[nearer] = True

    # background: vertical gradient + soft elliptical ground shadow
    rows = np.linspace(0, 1, H)[:, None]
    bg = np.stack([22 + 16 * rows, 24 + 16 * rows, 36 + 22 * rows], axis=-1) * np.ones((H, W, 1))
    foot_y = float(sy[np.argmin(p[:, 2])]); foot_x = float(sx[np.argmin(p[:, 2])])
    yy, xx = np.mgrid[0:H, 0:W]
    rx_, ry_ = max(np.ptp(p[:, 0]) * scale * 0.55, 10), max(np.ptp(p[:, 0]) * scale * 0.14, 6)
    shadow = np.clip(1 - (((xx - W / 2) / rx_) ** 2 + ((yy - (foot_y + 0.01 * H)) / ry_) ** 2), 0, 1)
    bg = bg * (1 - 0.45 * shadow[:, :, None]) if ground else bg
    # Depth cue separates recessed horizontal faces from the equally lit outer lid.
    span=max(float(np.ptp(p[:,1])),1e-6)
    shade_buf[cover]*=.65+.35*(1-np.clip((zbuf[cover]-float(p[:,1].min()))/span,0,1))
    base = np.array(color if color is not None else [112,168,255],dtype=np.float64)
    lit = np.clip(base[None, None, :] * shade_buf[:, :, None] + 14, 0, 255)
    frame = np.where(cover[:, :, None], lit, bg).astype(np.uint8)
    return Image.fromarray(frame).resize((width, height), Image.LANCZOS)




def _label_font(size):
    try:
        from matplotlib.font_manager import findfont
        from PIL import ImageFont
        return ImageFont.truetype(findfont("DejaVu Sans"), size)
    except Exception:
        from PIL import ImageFont
        return ImageFont.load_default()


def render_preview(solid, path, width=1440, height=820):
    """CAD-style sheet: a large isometric view plus front / right / top views."""
    from PIL import Image, ImageDraw

    verts, tris = mesh_arrays(solid)
    box = solid.bounding_box()
    dims = f"{box[3] - box[0]:.1f} × {box[4] - box[1]:.1f} × {box[5] - box[2]:.1f} mm"
    heavy = len(tris) > 30000
    iso_w = int(width * 0.66) if not heavy else width
    iso = _render_view(verts, tris, -35, 26, iso_w, height, ss=1 if heavy else 2)
    sheet = Image.new("RGB", (width, height), (20, 22, 30))
    sheet.paste(iso, (0, 0))
    draw = ImageDraw.Draw(sheet)
    draw.text((22, height - 40), dims, fill=(170, 180, 205), font=_label_font(20))
    if not heavy:
        col_w, gap = width - iso_w, 8
        view_h = (height - 2 * gap) // 3
        for i, (label, yaw, pitch) in enumerate((("FRONT", 0, 0), ("RIGHT", -90, 0), ("TOP", 0, 90))):
            view = _render_view(verts, tris, yaw, pitch, col_w - gap, view_h, ss=2, ground=False, fit=0.62)
            y = i * (view_h + gap)
            sheet.paste(view, (iso_w + gap, y))
            draw.text((iso_w + gap + 12, y + 8), label, fill=(150, 160, 185), font=_label_font(15))
    sheet.save(path, "PNG")

def build_files(stl_path, spec, preview_path=None):
    """Build, write, independently verify, preview. Returns (notes, info)."""
    solid, notes, info = build_program(spec)
    write_binary_stl(stl_path, solid)
    count, volume = verify_binary_stl(stl_path)
    info["verified_volume_mm3"] = volume
    if preview_path:
        try:
            render_preview(solid, preview_path)
        except Exception as error:  # a preview is a bonus, never a reason to fail the model
            notes.append(f"ℹ️ Preview image skipped ({error}).")
    return notes, info
