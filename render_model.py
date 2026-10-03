"""Optional high-quality mesh preview: blender --background --python render_model.py.

Uses only Blender's bundled Python. This does not modify the printable geometry.
"""

from pathlib import Path
import json
import sys

import bpy
from mathutils import Matrix, Vector

ROOT = Path(__file__).resolve().parent
if "--" in sys.argv and len(sys.argv) > sys.argv.index("--") + 1:
    ROOT = Path(sys.argv[sys.argv.index("--") + 1]).resolve()
report = json.loads((ROOT / "generation_report.json").read_text())
bpy.ops.object.select_all(action="SELECT")
bpy.ops.object.delete(use_global=False)


def material(name, color, roughness=.65):
    result = bpy.data.materials.new(name)
    result.diffuse_color = (*color, 1)
    result.use_nodes = True
    shader = result.node_tree.nodes.get("Principled BSDF")
    shader.inputs["Base Color"].default_value = (*color, 1)
    shader.inputs["Roughness"].default_value = roughness
    return result


def aim(obj, target):
    obj.rotation_euler = (Vector(target) - obj.location).to_track_quat("-Z", "Y").to_euler()


graphite = material("Graphite", (.085, .13, .15))
part_objects = []
for name, metadata in report["parts"].items():
    bpy.ops.wm.stl_import(filepath=str(ROOT / metadata["file"]))
    obj = bpy.context.object
    obj.name = name
    obj.matrix_world = Matrix.Scale(.01, 4) @ Matrix(metadata["assembly_matrix"])
    obj.data.materials.append(graphite)
    part_objects.append((obj, metadata))
bpy.ops.mesh.primitive_plane_add(size=200, location=(0, 0, -.008))
bpy.context.object.data.materials.append(material("Backdrop", (.76, .75, .70)))

# A non-printable gold marker makes the intended emitting point visible.
light_z = report["light_position_mm"][2] * .01
bpy.ops.mesh.primitive_uv_sphere_add(segments=24, ring_count=12, radius=.024, location=(0, 0, light_z))
bpy.context.object.data.materials.append(material("Emitter marker", (1, .53, .06), .25))

for position, energy, size in (((1.5, -2.5, 4), 500, 3), ((-2, -.5, 2.5), 250, 2), ((0, 2, 3), 400, 2)):
    bpy.ops.object.light_add(type="AREA", location=position)
    lamp = bpy.context.object
    lamp.data.energy = energy
    lamp.data.shape = "DISK"
    lamp.data.size = size
    aim(lamp, (0, 0, .35))

bpy.ops.object.camera_add(location=(1.8, -2.4, 3.1))
camera = bpy.context.object
aim(camera, (0, 0, .22))
camera.data.type = "ORTHO"
camera.data.ortho_scale = 2.35
scene = bpy.context.scene
scene.camera = camera
scene.render.engine = "CYCLES"
scene.cycles.samples = 48
scene.cycles.use_denoising = True
scene.render.resolution_x = 1100
scene.render.resolution_y = 1000
scene.render.resolution_percentage = 100
scene.world.color = (.3, .3, .3)
scene.view_settings.view_transform = "AgX"
scene.render.image_settings.file_format = "PNG"
scene.render.filepath = str(ROOT / "model_render.png")
bpy.ops.wm.save_as_mainfile(filepath=str(ROOT / "projector_preview.blend"))
bpy.ops.render.render(write_still=True)

# The second image shows the actual tab/slot connections and removable hood.
for obj, metadata in part_objects:
    obj.location += Vector(metadata["exploded_offset_mm"]) * .01
aim(camera, (0, 0, .5))
camera.data.ortho_scale = 2.85
scene.render.filepath = str(ROOT / "assembly_exploded.png")
bpy.ops.render.render(write_still=True)
