"""Geometry and optical regression tests, including rays through the exported STL."""

from dataclasses import replace
from pathlib import Path
import tempfile
import unittest
import math

import cv2
import numpy as np
from PIL import Image, ImageDraw
from shapely.geometry import LineString, Point
import trimesh

import generate as g


class ProjectorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.settings = g.Settings()
        cls.art, cls.bounds = g.image_geometry(g.ROOT / "fish.jpg", cls.settings)
        cls.panels = g.build_panels(cls.art, cls.bounds, cls.settings)
        cls.parts = g.build_parts(cls.panels, cls.settings)
        cls.by_name = {p.name: p for p in cls.parts}
        cls.model = g.build_model(cls.panels, cls.settings)
        cls.mesh = g.to_trimesh(cls.model)

    def test_seven_printable_parts_are_connected_watertight_and_on_bed(self):
        self.assertEqual(len(self.parts), 7)
        for part in self.parts:
            mesh = g.to_trimesh(part.printable)
            self.assertTrue(mesh.is_watertight, part.name)
            self.assertTrue(mesh.is_winding_consistent, part.name)
            self.assertEqual(len(mesh.split()), 1, part.name)
            self.assertAlmostEqual(mesh.bounds[0, 2], 0, places=5)
            if part.name.startswith("wall_") or part.name == "hood_support":
                self.assertAlmostEqual(mesh.extents[2], 2, places=5)
        self.assertTrue(self.mesh.is_watertight)
        self.assertTrue(self.mesh.is_winding_consistent)
        np.testing.assert_allclose(self.mesh.extents, [132, 132, 54.5])

    def test_base_floor_is_closed_except_for_the_through_bore(self):
        # Independently calculate the area of the 48-sided bore in the base.
        section = self.by_name["base"].solid.slice(.5)
        radius = self.settings.emitter_diameter / 2 + .2
        bore_area = 48 / 2 * radius ** 2 * math.sin(2 * math.pi / 48)
        self.assertAlmostEqual(section.area(), 132 * 132 - bore_area, places=4)
        source = [0, 0, self.settings.light_z]
        self.assertFalse(self.model.ray_cast(source, [0, 0, -20]))

    def test_center_post_is_hollow_along_its_entire_length(self):
        for x, y in ((0, 0), (2, 0), (0, -2), (1, 1)):
            self.assertFalse(self.model.ray_cast([x, y, 49], [x, y, -1]))
        # Tube walls and the surrounding base still exist.
        self.assertTrue(self.by_name["base"].solid.ray_cast([3.5, 0, 49], [3.5, 0, -1]))
        # Above the artwork, a slice intersects only the annular post.
        self.assertAlmostEqual(self.by_name["base"].solid.slice(42.75).area(),
                               24 * (4.2 ** 2 - 2.7 ** 2) * math.sin(2 * math.pi / 48), places=4)

    def test_blank_upper_walls_and_top_rails_are_removed(self):
        for panel in self.panels:
            high = panel.material.bounds[3]
            self.assertLess(high, 0)
            across_top = LineString([(-60, high - .01), (60, high - .01)])
            self.assertLess(panel.material.intersection(across_top).length, 60)
        # Light outside the image boundary is free to leave above the stencil.
        self.assertFalse(self.model.ray_cast([0, 0, 49], [1000, 0, -20]))

    def test_tabs_fit_slots_without_part_intersections(self):
        for i, part in enumerate(self.parts):
            for other in self.parts[i+1:]:
                self.assertLess(abs((part.solid ^ other.solid).volume()), 1e-5,
                                f"{part.name} overlaps {other.name}")
        base = self.by_name["base"].solid
        for part in self.parts:
            if part.name.startswith("wall_"):
                # Moving a wall to the next side must fail its keyed slot fit.
                self.assertGreater((part.solid.rotate([0, 0, 90]) ^ base).volume(), 1)

    def test_hood_blocks_room_facing_light_and_leaves_projection_rays(self):
        hood = self.by_name["light_hood"].solid
        origin = np.array([0, 0, 49])
        for angle in np.linspace(0, 2*math.pi, 12, endpoint=False):
            for elevation in (.01, .1, .5, 1.2, math.pi/2):
                direction = np.array([math.cos(angle)*math.cos(elevation),
                                      math.sin(angle)*math.cos(elevation), math.sin(elevation)])
                self.assertTrue(hood.ray_cast(origin, origin + direction*1000))
        for target in ([250, 0, -20], [-250, 0, -20], [0, 250, -20], [0, -250, -20]):
            self.assertFalse(hood.ray_cast(origin, target))
        # Regression: a support close to the emitter made a large diagonal
        # wedge. The distant corner bracket must leave these background rays.
        for target in ([350, 310, -20], [300, 340, -20], [400, 440, -20]):
            self.assertFalse(self.model.ray_cast(origin, target))

    def test_exported_stl_reproduces_artwork_in_all_four_sectors(self):
        # Independent expected pixels come directly from fish.jpg. Trimesh ray
        # testing reloads the written STL, independently of Manifold ray_cast.
        with tempfile.TemporaryDirectory() as tmp:
            restored = []
            for part in self.parts:
                path = Path(tmp) / f"{part.name}.stl"
                g.to_trimesh(part.printable).export(path)
                loaded = trimesh.load(path)
                loaded.apply_transform(part.assembly_matrix)
                restored.append(loaded)
            exported = trimesh.util.concatenate(restored)
            source_image = np.asarray(Image.open(g.ROOT / "fish.jpg").convert("L"))
            h, w = source_image.shape
            grid = np.linspace(-480, 480, 55)
            points = np.array([[x, y, -20] for y in grid for x in grid])
            origins = np.tile([0, 0, self.settings.light_z], (len(points), 1))
            blocked = exported.ray.intersects_any(origins, points - origins)
            cols = np.clip(((points[:, 0] / 1000 + .5) * w).astype(int), 0, w - 1)
            rows = np.clip(((.5 - points[:, 1] / 1000) * h).astype(int), 0, h - 1)
            expected_dark = source_image[rows, cols] <= 125
            dark_interior = cv2.erode((source_image <= 125).astype(np.uint8), np.ones((3, 3), np.uint8))
            x, y = points[:, 0], points[:, 1]
            for sector in (x > abs(y), y > abs(x), -x > abs(y), -y > abs(x)):
                # Ignore silhouette edges affected by input JPEG anti-aliasing.
                dark = sector & (dark_interior[rows, cols] > 0)
                self.assertGreater(blocked[dark].mean(), .94)
                clear = sector & ~expected_dark & (np.maximum(abs(x), abs(y)) > 110)
                self.assertGreater((~blocked[clear]).mean(), .75)

    def test_target_to_panel_mapping_has_correct_orientation(self):
        for panel in self.panels:
            local = np.array(g.polygons(panel.holes)[0].representative_point().coords[0])
            u, v = local
            target = (panel.normal * self.settings.near + panel.tangent * u) * self.settings.distance / -v
            self.assertFalse(self.art.contains(Point(target)))
            self.assertTrue(self.bounds.covers(Point(target)))
            # Every panel is in a unique quadrant of the single receiving wall.
            self.assertGreater(target @ panel.normal, abs(target @ panel.tangent))

    def test_nested_holes_transparency_and_non_square_aspect(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "ring.png"
            image = Image.new("RGBA", (200, 100), (0, 0, 0, 0))
            draw = ImageDraw.Draw(image)
            draw.ellipse((60, 10, 140, 90), fill="black")
            draw.ellipse((80, 30, 120, 70), fill="white")
            draw.ellipse((96, 46, 104, 54), fill="black")
            image.save(path)
            art, bounds = g.image_geometry(path, self.settings)
            self.assertEqual(bounds.bounds, (-500, -250, 500, 250))
            self.assertTrue(art.contains(Point(0, 0)))
            self.assertFalse(art.contains(Point(60, 0)))
            self.assertTrue(art.contains(Point(150, 0)))
            self.assertFalse(art.contains(Point(-400, 0)))

    def test_changed_optics_and_dimensions_produce_valid_solid(self):
        settings = replace(self.settings, size=100, depth=75, wall_gap=8, projection_width=800,
                           light_height=42, wall_thickness=3)
        settings.validate()
        art, bounds = g.image_geometry(g.ROOT / "fish.jpg", settings)
        panels = g.build_panels(art, bounds, settings)
        model = g.build_model(panels, settings)
        mesh = g.to_trimesh(model)
        self.assertTrue(mesh.is_watertight)
        np.testing.assert_allclose(mesh.extents, [112, 112, settings.hood_top])

    def test_invalid_settings_are_rejected(self):
        for change in ({"wall_gap": -1}, {"wall_thickness": 70}, {"depth": 0},
                       {"light_height": 2}, {"threshold": 300}, {"bridge_width": .1},
                       {"size": float("nan")}, {"fit_clearance": 0}, {"tab_width": 30},
                       {"hood_radius": 4}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                replace(self.settings, **change).validate()


if __name__ == "__main__":
    unittest.main()
