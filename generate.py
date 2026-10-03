"""Build an open-top four-wall shadow projector for ONE plane behind its base.

Coordinates are millimetres: base at z=0, open front at +z, receiving wall
at z=-wall_gap, point light at (0, 0, light_z). Image x/y stay wall x/y.
Run python generate.py --help for dimensions and optical settings.
"""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
import json
import math
from pathlib import Path
import zipfile

import cv2
import manifold3d as m3d
import numpy as np
from PIL import Image, ImageDraw
from shapely import affinity, make_valid
from shapely.geometry import LineString, Polygon, box
from shapely.ops import nearest_points, unary_union
import trimesh

ROOT = Path(__file__).resolve().parent
SIDES = ("right", "top", "left", "bottom")


@dataclass(frozen=True)
class Settings:
    size: float = 120.0
    depth: float = 90.0
    wall_thickness: float = 2.0
    base_thickness: float = 4.0
    frame: float = 1.2
    bridge_width: float = 0.8
    min_feature: float = 0.6
    projection_width: float = 1000.0
    wall_gap: float = 20.0
    threshold: int = 125
    resolution: int = 1200
    light_height: float | None = None
    emitter_diameter: float = 5.0
    emitter_height: float = 6.0
    base_margin: float = 6.0
    fit_clearance: float = 0.3
    tab_width: float = 10.0
    hood_radius: float = 16.0
    hood_thickness: float = 1.5

    @property
    def light_z(self):
        return self.base_thickness + self.depth / 2 if self.light_height is None else self.light_height

    @property
    def distance(self):
        return self.light_z + self.wall_gap

    @property
    def near(self):
        return self.size / 2 - self.wall_thickness

    @property
    def top(self):
        return self.base_thickness + self.depth

    @property
    def slot_depth(self):
        return self.base_thickness * .6

    @property
    def tab_depth(self):
        return self.slot_depth - self.fit_clearance / 2

    @property
    def hood_top(self):
        return self.light_z + 4 + self.hood_thickness

    def validate(self):
        for key, value in asdict(self).items():
            if value is not None and not math.isfinite(value):
                raise ValueError(f"{key} must be finite")
        for key in ("size", "depth", "wall_thickness", "base_thickness", "frame",
                    "bridge_width", "min_feature", "projection_width", "emitter_diameter", "emitter_height",
                    "base_margin", "fit_clearance", "tab_width", "hood_radius", "hood_thickness"):
            if getattr(self, key) <= 0:
                raise ValueError(f"{key} must be positive")
        if self.wall_gap < 0 or self.near <= 2 * self.frame or self.depth <= 2 * self.frame:
            raise ValueError("Wall gap must be nonnegative and the frame must fit inside the walls")
        if not 0 <= self.threshold <= 255 or not 128 <= self.resolution <= 4096:
            raise ValueError("Threshold must be 0..255 and resolution 128..4096")
        if self.bridge_width < self.min_feature:
            raise ValueError("Bridge width must be at least min-feature")
        if not self.base_thickness + self.emitter_height + 4 < self.light_z < self.top - self.frame:
            raise ValueError("Light height must leave room for the mount above the base and below the open front")
        if self.emitter_diameter / 2 + 1.5 >= self.near:
            raise ValueError("Emitter mount must fit inside the box")
        if self.base_margin <= self.fit_clearance / 2 or self.tab_depth < 1:
            raise ValueError("Base must leave material outside and beneath the locating slots")
        if self.base_margin < 5 or self.fit_clearance >= 1:
            raise ValueError("Corner bracket needs base-margin >= 5 and fit-clearance < 1 mm")
        if self.tab_width + self.fit_clearance >= self.size * .14:
            raise ValueError("Tabs are too wide for the keyed slot pattern; reduce tab-width or increase size")
        if self.hood_radius <= self.emitter_diameter / 2 + 5 or self.hood_radius >= self.near - 5:
            raise ValueError("Hood must clear the emitter and fit inside the walls")
        if not 1 <= self.hood_thickness < self.hood_radius / 3:
            raise ValueError("Hood thickness must be at least 1 mm and less than one third of its radius")


@dataclass
class Panel:
    name: str
    normal: np.ndarray
    tangent: np.ndarray
    material: object
    holes: object
    bridges: int
    removed_details: int


@dataclass
class Part:
    name: str
    solid: object
    print_matrix: np.ndarray
    explosion: np.ndarray

    @property
    def printable(self):
        return self.solid.transform(self.print_matrix)

    @property
    def assembly_matrix(self):
        return np.linalg.inv(np.vstack([self.print_matrix, [0, 0, 0, 1]]))


def polygons(geometry):
    if isinstance(geometry, Polygon):
        return [geometry] if not geometry.is_empty else []
    return [p for part in getattr(geometry, "geoms", ()) for p in polygons(part)]


def image_geometry(path: Path, settings: Settings):
    """Return dark artwork in receiving-wall coordinates, with holes intact."""
    with Image.open(path) as original:
        rgba = original.convert("RGBA")
        background = Image.new("RGBA", rgba.size, "white")
        background.alpha_composite(rgba)
        gray = background.convert("L")
        gray.thumbnail((settings.resolution, settings.resolution), Image.Resampling.LANCZOS)
        pixels = np.asarray(gray)
    h, w = pixels.shape
    mask = (pixels <= settings.threshold).astype(np.uint8) * 255
    contours, hierarchy = cv2.findContours(mask, cv2.RETR_CCOMP, cv2.CHAIN_APPROX_SIMPLE)
    shapes = []
    if hierarchy is not None:
        hierarchy = hierarchy[0]
        for i, contour in enumerate(contours):
            if hierarchy[i, 3] != -1 or len(contour) < 3:
                continue
            holes = []
            child = hierarchy[i, 2]
            while child != -1:
                if len(contours[child]) >= 3:
                    holes.append(contours[child][:, 0, :])
                child = hierarchy[child, 0]
            shapes.extend(polygons(make_valid(Polygon(contour[:, 0, :], holes))))
    scale = settings.projection_width / w
    artwork = affinity.scale(unary_union(shapes), xfact=scale, yfact=-scale, origin=(0, 0))
    artwork = affinity.translate(artwork, -settings.projection_width / 2, h * scale / 2)
    bounds = box(-w * scale / 2, -h * scale / 2, w * scale / 2, h * scale / 2)
    return artwork.intersection(bounds), bounds


def map_polygons(geometry, transform):
    mapped = []
    for polygon in polygons(geometry):
        exterior = transform(np.asarray(polygon.exterior.coords))
        interiors = [transform(np.asarray(ring.coords)) for ring in polygon.interiors]
        mapped.extend(polygons(make_valid(Polygon(exterior, interiors))))
    return unary_union(mapped)


def connect_material(material, width):
    """Attach floating dark islands using shortest bridges to the growing frame."""
    pieces = sorted(polygons(material), key=lambda p: p.area, reverse=True)
    frame_index = max(range(len(pieces)), key=lambda i: box(*pieces[i].bounds).area)
    connected = pieces.pop(frame_index)
    count = 0
    while pieces:
        distances = [connected.distance(piece) for piece in pieces]
        piece = pieces.pop(int(np.argmin(distances)))
        a, b = nearest_points(connected, piece)
        bridge = LineString([a, b]).buffer(width / 2, cap_style="round")
        connected = unary_union([connected, piece, bridge])
        touched = [p for p in pieces if connected.intersects(p)]
        if touched:
            connected = unary_union([connected, *touched])
            pieces = [p for p in pieces if not connected.intersects(p)]
        count += 1
    return connected, count


def build_panels(artwork, image_bounds, settings):
    panels = []
    r = settings.near
    low = settings.base_thickness - settings.light_z
    # Four triangular image sectors meet on the diagonals. Each is projected
    # backwards onto its own wall, NOT copied onto every side.
    reach = max(abs(v) for v in image_bounds.bounds) * 4 + settings.size
    inner = settings.distance * r / -low
    for index, name in enumerate(SIDES):
        angle = index * math.pi / 2
        normal = np.round([math.cos(angle), math.sin(angle)])
        tangent = np.array([-normal[1], normal[0]])
        sector = Polygon([
            normal * inner - tangent * inner,
            normal * reach - tangent * reach,
            normal * reach + tangent * reach,
            normal * inner + tangent * inner,
        ])
        visible = image_bounds.intersection(sector)

        def to_panel(points):
            radial = points @ normal
            return np.column_stack((r * (points @ tangent) / radial, -settings.distance * r / radial))

        # Keep the shadow-casting artwork itself. In particular, do not fill the
        # unused area above the image with an opaque rectangular wall or top rail.
        dark = map_polygons(visible.intersection(artwork), to_panel)
        high = max(low + 2 * settings.frame, dark.bounds[3] if not dark.is_empty else low)
        domain = box(-settings.size / 2, low, settings.size / 2, high)
        # A U-shaped support: bottom rail and short corner posts, with no top
        # rail. Each wall stops at its own highest projected artwork detail.
        aperture = box(-r + settings.frame, low + settings.frame, r - settings.frame, high + settings.frame)
        frame = domain.difference(aperture)
        # Close sub-printable slots while retaining the open artwork outline.
        radius = settings.min_feature / 2
        dark = dark.buffer(radius).buffer(-radius).intersection(domain).intersection(aperture)
        material = unary_union([frame, dark])
        parts = polygons(material)
        keep = [p for p in parts if p.area >= settings.min_feature ** 2]
        removed = len(parts) - len(keep)
        material, bridge_count = connect_material(unary_union(keep), settings.bridge_width)
        material = material.intersection(domain).simplify(0.025, preserve_topology=True)
        holes = domain.difference(material)
        panels.append(Panel(name, normal, tangent, material, holes, bridge_count, removed))
    return panels


def cross_section(geometry):
    rings = []
    for polygon in polygons(geometry):
        rings.append(np.asarray(polygon.exterior.coords)[:-1])
        rings.extend(np.asarray(ring.coords)[:-1] for ring in polygon.interiors)
    return m3d.CrossSection(rings, m3d.FillRule.EvenOdd)


def panel_matrix(panel, light_z):
    # Local (u, v, d) -> world (tangent*u + normal*d, light_z+v).
    return np.array([[panel.tangent[0], 0, panel.normal[0], 0],
                     [panel.tangent[1], 0, panel.normal[1], 0],
                     [0, 1, 0, light_z]])


def tab_positions(name, settings):
    # Distinct asymmetric patterns key each wall to its side and orientation.
    patterns = {"right": (-.34, -.06, .27), "top": (-.28, .03, .35),
                "left": (-.35, -.12, .22), "bottom": (-.25, .10, .34)}
    return [v * settings.size for v in patterns[name]]


def build_parts(panels, settings):
    size, thick, base = settings.size, settings.wall_thickness, settings.base_thickness
    footprint = size + 2 * settings.base_margin
    floor = m3d.Manifold.cube([footprint, footprint, base]).translate([-footprint / 2, -footprint / 2, 0])
    parts = []
    # Taper the remaining material along rays from the point source. Intersect
    # with a wall-sized slab to retain a square footprint and a flat base joint.
    near, far = settings.near - 0.02, size / 2 + 0.02
    for panel in panels:
        slab = m3d.Manifold.cube([size, settings.depth, thick]).translate(
            [-size / 2, base - settings.light_z, settings.near])
        material = cross_section(panel.material).scale([near / settings.near] * 2).extrude(
            far - near, scale_top=[far / near, far / near]).translate([0, 0, near])
        wall = slab ^ material
        # A full-thickness lower rail joins the artwork, corner posts and tabs.
        rail = m3d.Manifold.cube([size, max(settings.frame, thick), thick]).translate(
            [-size / 2, base - settings.light_z, settings.near])
        # 45-degree miters leave a small assembly gap, avoiding overlapping walls.
        gap = settings.fit_clearance / 2
        wedge = Polygon([(-settings.near + gap, settings.near),
                         (settings.near - gap, settings.near),
                         (size / 2 - gap, size / 2), (-size / 2 + gap, size / 2)])
        miter = cross_section(wedge).extrude(settings.depth).transform(
            [[1, 0, 0, 0], [0, 0, -1, settings.top-settings.light_z], [0, 1, 0, 0]])
        wall = (wall + rail) ^ miter
        matrix = panel_matrix(panel, settings.light_z)
        for position in tab_positions(panel.name, settings):
            tab = m3d.Manifold.cube([settings.tab_width, settings.tab_depth + .1, thick]).translate(
                [position - settings.tab_width / 2, base-settings.light_z-settings.tab_depth, settings.near])
            wall = wall + tab
            slot = m3d.Manifold.cube([settings.tab_width+settings.fit_clearance,
                                    settings.slot_depth+.1, thick+settings.fit_clearance]).translate(
                [position-(settings.tab_width+settings.fit_clearance)/2,
                 base-settings.light_z-settings.slot_depth, settings.near-settings.fit_clearance/2])
            floor = floor - slot.transform(matrix)
        print_matrix = np.array([[panel.tangent[0], panel.tangent[1], 0, size / 2],
                                 [0, 0, 1, -base+settings.tab_depth],
                                 [panel.normal[0], panel.normal[1], 0, -settings.near]])
        parts.append(Part("wall_" + panel.name, wall.transform(matrix), print_matrix,
                          np.array([panel.normal[0]*25, panel.normal[1]*25, 12])))
    # Keep the post on the base and cut its bore through the whole base.
    socket_radius = settings.emitter_diameter / 2 + 0.2
    mount_top = settings.light_z - settings.emitter_height
    post = m3d.Manifold.cylinder(mount_top - base + 0.1, socket_radius + 1.5,
                               circular_segments=48).translate([0, 0, base - 0.1])
    bore = m3d.Manifold.cylinder(mount_top + 0.2, socket_radius, circular_segments=48).translate([0, 0, -0.1])
    identity = np.eye(4)[:3]
    parts.insert(0, Part("base", (floor + post) - bore, identity, np.zeros(3)))

    # Removable hood: an inverted cup blocks the room-facing hemisphere.
    # Support it at a distant corner, not beside the emitter: a nearby support
    # would magnify into a wide shadow over the artwork. The separate L bracket
    # prints flat and its upper arm stays entirely above the emitting point.
    radius, shell = settings.hood_radius, settings.hood_thickness
    hood = m3d.Manifold.cylinder(4 + shell, radius, circular_segments=64).translate([0, 0, settings.light_z])
    cavity = m3d.Manifold.cylinder(4.1, radius-shell, circular_segments=64).translate([0, 0, settings.light_z-.1])
    hood = hood - cavity
    unit = 1 / math.sqrt(2)
    diagonal = np.array([[unit, 0, unit, 0], [unit, 0, -unit, 0], [0, 1, 0, 0]])
    tower_r = (size / 2 + settings.base_margin / 2) * math.sqrt(2)
    tower_width, bracket_thickness = 3.0, 2.0
    arm_top = settings.hood_top - 1
    arm_bottom = arm_top - 3
    tip_r = radius + 1 + settings.fit_clearance / 2
    profile = Polygon([(tower_r-1.5, base), (tower_r+1.5, base),
                       (tower_r+1.5, arm_top), (tip_r, arm_top),
                       (tip_r, arm_bottom), (tower_r-1.5, arm_bottom)])
    profile = profile.union(box(tower_r-1.5, base-settings.tab_depth, tower_r+1.5, base+.1))
    bracket = cross_section(profile).extrude(bracket_thickness).translate([0, 0, -bracket_thickness/2]).transform(diagonal)
    bracket_slot = m3d.Manifold.cube([tower_width+settings.fit_clearance, settings.slot_depth+.1,
                                    bracket_thickness+settings.fit_clearance]).translate(
        [tower_r-(tower_width+settings.fit_clearance)/2, base-settings.slot_depth,
         -(bracket_thickness+settings.fit_clearance)/2]).transform(diagonal)
    parts[0].solid = parts[0].solid - bracket_slot
    bracket_print = np.array([[unit, unit, 0, -tip_r], [0, 0, 1, -base+settings.tab_depth],
                              [unit, -unit, 0, bracket_thickness/2]])
    parts.append(Part("hood_support", bracket, bracket_print, np.array([12, 12, 20])))

    # A blind radial pocket in an external lug receives the bracket tip.
    # It does not break into the cup, preserving the room-light shield.
    lug = m3d.Manifold.cube([7.5, 5, 4]).translate([radius-1.5, arm_bottom-1, -2]).transform(diagonal)
    pocket = m3d.Manifold.cube([5.1, 3+settings.fit_clearance, bracket_thickness+settings.fit_clearance]).translate(
        [radius+1, arm_bottom-settings.fit_clearance, -(bracket_thickness+settings.fit_clearance)/2]).transform(diagonal)
    hood = (hood + lug) - pocket
    hood_print = np.array([[1, 0, 0, 0], [0, -1, 0, 0], [0, 0, -1, settings.hood_top]])
    parts.append(Part("light_hood", hood, hood_print, np.array([0, 0, 55])))
    for part in parts:
        if part.solid.status() != m3d.Error.NoError or len(part.solid.decompose()) != 1:
            raise ValueError(f"{part.name} must be one connected manifold; adjust feature widths")
    return parts


def build_model(panels, settings):
    """Assembled solid for ray testing only; print the individual parts instead."""
    return m3d.Manifold.batch_boolean([p.solid for p in build_parts(panels, settings)], m3d.OpType.Add)


def to_trimesh(model):
    data = model.to_mesh()
    return trimesh.Trimesh(vertices=np.asarray(data.vert_properties)[:, :3],
                           faces=np.asarray(data.tri_verts), process=True)


def svg_path(geometry):
    commands = []
    for polygon in polygons(geometry):
        for ring in (polygon.exterior, *polygon.interiors):
            coordinates = np.asarray(ring.coords)[:-1]
            commands.append("M " + " L ".join(f"{x:.5f},{-y:.5f}" for x, y in coordinates) + " Z")
    return " ".join(commands)


def export_masks(panels, settings, directory):
    """Diagnostic 2D artwork masks; printable tabs and rails are added in 3D."""
    s = settings
    layout_height = max(p.material.bounds[3] for p in panels) + s.light_z - s.base_thickness
    layout = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {4*(s.size+12)} {layout_height+36}">',
              '<rect width="100%" height="100%" fill="#f4f2ec"/>',
              '<text x="6" y="12" font-family="sans-serif" font-size="7">Open-top wall stencils | dark = material | viewed from the light</text>']
    for index, panel in enumerate(panels):
        panel_height = panel.material.bounds[3] - (s.base_thickness - s.light_z)
        svg = (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="{-s.size/2} {-panel.material.bounds[3]} {s.size} {panel_height}" '
               f'width="{s.size}mm" height="{panel_height}mm">'
               f'<path d="{svg_path(panel.holes)}" fill="black" fill-rule="evenodd"/></svg>')
        (directory / f"apertures_{panel.name}.svg").write_text(svg, encoding="utf-8")
        x = index * (s.size + 12) + 6
        layout.extend([f'<text x="{x}" y="24" font-family="sans-serif" font-size="6">{panel.name}</text>',
                       f'<g transform="translate({x+s.size/2},{30+layout_height+s.base_thickness-s.light_z})">',
                       f'<path d="{svg_path(panel.material)}" fill="#243237" fill-rule="evenodd"/></g>'])
    (directory / "shadow.svg").write_text("\n".join([*layout, "</svg>"]), encoding="utf-8")


def export_parts(parts, settings, directory):
    lines = ["// Assembled preview. PRINT THE SEVEN INDIVIDUAL STLS, not shadow.stl.",
             "// Keep this file beside the individual STL files. Units: mm.",
             "// Set exploded=1 to inspect tab/slot assembly; remove hood with show_hood=false.",
             "exploded = 0;", "show_hood = true;", "union() {"]
    report = {}
    for part in parts:
        mesh = to_trimesh(part.printable)
        if not mesh.is_watertight or not mesh.is_winding_consistent or len(mesh.split()) != 1:
            raise ValueError(f"Printable mesh validation failed: {part.name}")
        mesh.export(directory / f"{part.name}.stl")
        lines.append(f"  // {part.name}: exported in print orientation")
        if part.name == "light_hood":
            lines.append("  if (show_hood)")
        lines.extend([f"  translate({json.dumps(part.explosion.tolist())} * exploded)",
                      f"    multmatrix({json.dumps(part.assembly_matrix.tolist())})",
                      f'      import("{part.name}.stl", convexity=20);'])
        report[part.name] = {"file": f"{part.name}.stl", "watertight": True, "connected_components": 1,
                             "print_size_mm": mesh.extents.tolist(), "volume_mm3": float(mesh.volume),
                             "assembly_matrix": part.assembly_matrix.tolist(),
                             "exploded_offset_mm": part.explosion.tolist()}
    (directory / "shadow.scad").write_text("\n".join([*lines, "}"]) + "\n", encoding="utf-8")
    guide = f"""SEVEN-PART SHADOW PROJECTOR - millimetres

Print one each: base.stl, wall_right.stl, wall_top.stl,
wall_left.stl, wall_bottom.stl, hood_support.stl, light_hood.stl.

Each STL is already oriented for printing. Keep the four wall panels FLAT,
with their broad faces on the bed. Base prints upright, post pointing up.
The L-shaped hood support also prints flat. Hood prints upside down, closed
cap and mounting-lug roof on the bed. Its short socket bridge should be
checked in your slicer; keep support off the delicate wall artwork.

The base has three blind locating slots for each wall. Different asymmetric
tab patterns key each wall to its own side and orientation. Seat the bottom
rails against the base. Fit clearance is {settings.fit_clearance:g} mm total
({settings.fit_clearance/2:g} mm per side). Slots are {settings.slot_depth:g} mm deep;
tabs are {settings.tab_depth:g} mm deep. Adjust --fit-clearance and regenerate if
your printer needs a looser fit. These are slip fits, not locking snap fits;
secure the walls after checking fit if the base will be mounted vertically.

Pass wiring through the hollow center post and base for the {settings.emitter_diameter:g} mm emitter.
Seat the L-shaped hood support in the separate diagonal slot at the +x,+y
corner of the base. Slide the hood's external socket over its inward-pointing
arm, closed face upward. The pocket roof rests on the arm to set hood height.
The tall support sits in a corner-shadow direction, far from the emitter.
Place the emitting point at (0, 0, {settings.light_z:g}), measured from
the base underside; it sits inside the hood, level with the hood's lower rim.
The closed hood faces the room; light exits underneath toward the wall.

Place the base parallel to the receiving wall, {settings.wall_gap:g} mm in front
of it, to make one {settings.projection_width:g} mm-wide image. A wall mounting
bracket/spacer and electronics are not included. Keep the optical position
unchanged when assembling. No physical print/fit/light test has been done.

shadow.stl is an assembled PREVIEW, not a print layout. shadow.scad can show
an exploded view by setting exploded=1. The ZIP contains only printable parts
and this guide, to avoid accidentally printing the assembled preview.
"""
    (directory / "assembly_guide.txt").write_text(guide, encoding="utf-8")
    with zipfile.ZipFile(directory / "print_parts.zip", "w", zipfile.ZIP_DEFLATED) as archive:
        for filename in [*(f"{p.name}.stl" for p in parts), "assembly_guide.txt"]:
            archive.write(directory / filename, filename)
    return report


def projection_preview(model, image_bounds, settings, directory, resolution=600):
    """Trace the final solid, including the base, frame, mount and bridges."""
    minx, miny, maxx, maxy = image_bounds.bounds
    height = max(1, round(resolution * (maxy - miny) / (maxx - minx)))
    if height > 1800:
        resolution = max(1, round(resolution * 1800 / height))
        height = 1800
    xs = np.linspace(minx, maxx, resolution)
    ys = np.linspace(maxy, miny, height)
    source = [0, 0, settings.light_z]
    visible = np.empty((height, resolution), dtype=bool)
    for row, y in enumerate(ys):
        for col, x in enumerate(xs):
            visible[row, col] = not model.ray_cast(source, [x, y, -settings.wall_gap])
    pixels = np.where(visible[:, :, None], np.array([250, 218, 153]), np.array([26, 25, 23])).astype(np.uint8)
    result = Image.new("RGB", (resolution, height + 65), (245, 243, 236))
    result.paste(Image.fromarray(pixels), (0, 0))
    draw = ImageDraw.Draw(result)
    draw.text((14, height + 12), "ONE wall | all four sides combined | actual mesh ray test", fill=(38, 40, 43))
    draw.text((14, height + 34), f"{settings.projection_width:g} mm wide | ideal point light | center = base shadow", fill=(38, 40, 43))
    result.save(directory / "projection_preview.png")
    Image.fromarray((visible * 255).astype(np.uint8)).save(directory / "projection_mask.png")
    return float(visible.mean())


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("input", nargs="?", type=Path, default=ROOT / "fish.jpg")
    parser.add_argument("--output-dir", type=Path, default=ROOT)
    for field, default in asdict(Settings()).items():
        parser.add_argument("--" + field.replace("_", "-"), type=int if field in ("threshold", "resolution") else float,
                            default=default, help=f"{field.replace('_', ' ')} (default: {default}; dimensions in mm)")
    parser.add_argument("--skip-previews", action="store_true", help="Only export CAD, STL and report")
    args = parser.parse_args(argv)
    settings = Settings(**{field: getattr(args, field) for field in asdict(Settings())})
    try:
        settings.validate()
        artwork, bounds = image_geometry(args.input, settings)
        panels = build_panels(artwork, bounds, settings)
        parts = build_parts(panels, settings)
        model = m3d.Manifold.batch_boolean([p.solid for p in parts], m3d.OpType.Add)
        mesh = to_trimesh(model)
        if not mesh.is_watertight or not mesh.is_winding_consistent or mesh.volume <= 0:
            raise ValueError("Export mesh failed watertight/normal/volume validation")
    except (ValueError, OSError) as error:
        parser.error(str(error))
    args.output_dir.mkdir(parents=True, exist_ok=True)
    mesh.export(args.output_dir / "shadow.stl")
    export_masks(panels, settings, args.output_dir)
    part_report = export_parts(parts, settings, args.output_dir)
    report = {"input": str(args.input.resolve()), "settings": asdict(settings),
              "light_position_mm": [0, 0, settings.light_z], "receiving_wall_z_mm": -settings.wall_gap,
              "projection_size_mm": [bounds.bounds[2] * 2, bounds.bounds[3] * 2],
              "model_size_mm": mesh.extents.tolist(), "watertight": bool(mesh.is_watertight),
              "connected_components": len(model.decompose()), "triangles": len(mesh.faces),
              "volume_mm3": float(mesh.volume),
              "assembly_preview_only": True, "parts": part_report,
              "fit": {"clearance_total_mm": settings.fit_clearance, "slot_depth_mm": settings.slot_depth,
                      "tab_depth_mm": settings.tab_depth, "tab_width_mm": settings.tab_width,
                      "wall_tab_positions_mm": {p.name: tab_positions(p.name, settings) for p in panels}},
              "bore_diameter_mm": settings.emitter_diameter + 0.4,
              "panels": {p.name: {"bridges": p.bridges, "removed_tiny_details": p.removed_details,
                                   "top_z_mm": p.material.bounds[3] + settings.light_z} for p in panels},
              "notes": ["One image on a plane parallel to the base, not four repeated images.",
                        "Print the base, four walls, flat hood support and removable hood separately; shadow.stl is only an assembled preview.",
                        "Walls print flat and have keyed tabs. Upper edges follow the artwork, without blank panels.",
                        "The center tube is hollow through the base. An uncovered bore can transmit a small central spot.",
                        "The base obscures most of the center. Corner ribs and bridges also cast shadows.",
                        "The hood blocks the room-facing hemisphere; its distant corner support avoids a large central support shadow.",
                        "Preview assumes an ideal point source; real emitter size affects sharpness."]}
    print(f"Created {len(parts)} printable parts, plus shadow.stl/scad assembly previews: {len(mesh.faces):,} triangles", flush=True)
    if not args.skip_previews:
        print("Tracing the combined projection through the final mesh...", flush=True)
        report["projection_light_fraction"] = projection_preview(model, bounds, settings, args.output_dir)
    (args.output_dir / "generation_report.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print("Dimensions: " + " x ".join(f"{v:g}" for v in mesh.extents) + " mm")
    print(f"Emitter: [0, 0, {settings.light_z:g}] mm; receiving wall: z={-settings.wall_gap:g} mm")
    print(f"Combined projection: {settings.projection_width:g} mm wide")


if __name__ == "__main__":
    main()
