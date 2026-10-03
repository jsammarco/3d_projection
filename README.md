# Assembled shadow projector

The generator produces **seven separate printable parts**: a slotted base with a hollow light post, four flat stencil walls, a flat corner bracket, and a removable light hood sized for a 5 mm emitter. The walls combine into one fish image on a flat receiving wall behind the base. They retain only the artwork, bottom rails, bridges and short corner posts; there are no blank upper wall panels or top rails.

## Shadow Studio GUI

Double-click **`Start Shadow Studio.cmd`**, or run:

```powershell
cd C:\Users\Joe\Projects\3d_projection
.\.venv\Scripts\python.exe gui.py
```

The app opens at **http://127.0.0.1:8765**. Keep its terminal window open while using it; Ctrl+C stops the server. Launching it again reopens the running app. Everything runs on your computer, including image processing and ray tracing. The viewer libraries are bundled locally; no internet connection or cloud upload is needed.

1. Choose an image, or drag one onto the artwork preview. Adjust the threshold and use **Mask** to inspect dark material versus open areas. PNG, JPG, WebP and BMP are supported, up to 25 MB and 20 million pixels.
2. Set wall span, base/wall thickness, slot clearance, projection width, distance to the receiving wall, light height, and LED body diameter. **Print details** exposes the artwork resolution, bridges, tabs and hood dimensions. Dimensions are in millimetres. The default LED body remains **5 mm**.
3. Leave **Auto update** enabled, or click **Update model**. Invalid combinations display an explanation. Generation uses the same geometry as the command-line tool.
4. In **3D model**, drag to orbit, scroll to zoom, right-drag to pan, use **Explode**, or hide the hood to inspect the interior. In **Print parts**, inspect all seven parts in their export orientations. This layout illustrates separate parts, not a prescribed printer-bed arrangement.
5. In **Light simulation**, inspect the complete projection on one receiving wall. **Face the wall** aligns the camera to the image. Turn the light on/off, adjust brightness, or enable natural falloff. Click the projection thumbnail for an enlarged shadow-detail image.
6. **Save STLs** downloads a ZIP with all seven printable STLs, an assembly guide, and `project.json` recording dimensions and assembly transforms. The print-files dropdown also downloads individual STLs. Regenerate after changing settings before exporting. Unsaved models live in a temporary working directory; save the ZIP to keep them.

The light preview ray-tests the actual assembled meshes, including the base, hollow post, bridges, bracket and hood. The **Emitting area** slider approximates a uniformly luminous disk with 25 samples, revealing finite-source blur; zero means an ideal point. This optical area is separate from the LED's physical body diameter and does not change the print files. The default view normalizes illumination so the whole shadow is easy to inspect; natural falloff adds inverse-square and angle attenuation. Brightness is relative, not a calibrated lumen measurement. The thumbnail shows normalized shadow detail. Reflections, material transmission, scattering, and a specific LED's angular emission pattern are not modeled. Geometry alone cannot guarantee physical print fit or optical sharpness.

For a new environment, install the GUI dependencies once:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-gui.txt
.\.venv\Scripts\python.exe gui.py
```

Use `--port 8766` if another program uses the default port, or `--no-browser` to start without opening a tab. Chrome or Edge with WebGL enabled supports the interactive viewer. The Python service only listens on the local loopback interface.

Run geometry and GUI integration checks with:

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

## Files to print

Download or open `print_parts.zip`, containing these parts and `assembly_guide.txt`:

| File | Print orientation | Assembly |
| --- | --- | --- |
| `base.stl` | Base flat, post upward | Three blind slots for each wall; center tube bored through the base |
| `wall_right.stl` | Flat on its broad face | +x side |
| `wall_top.stl` | Flat on its broad face | +y side |
| `wall_left.stl` | Flat on its broad face | -x side |
| `wall_bottom.stl` | Flat on its broad face | -y side |
| `hood_support.stl` | Flat on its broad face | Diagonal slot at the +x,+y corner |
| `light_hood.stl` | Upside down, closed cap on bed | External socket slides over the bracket arm |

Each STL is already in print orientation and rests at z=0. Keep the wall panels and bracket flat in the slicer. Check the hood's short socket bridge and the small artwork features, and use opaque material. Mesh validation does not replace checking your printer's feature limits.

**Do not print `shadow.stl` as a single object.** It is an assembled preview for inspection and optical testing. `shadow.scad` loads the individual STL files into their assembly positions; keep them beside it. Set `exploded=1` to inspect the assembly or `show_hood=false` to see inside.

## Assembly and fit

The base is **132 × 132 mm**, with a **4 mm floor** and **2 mm wall panels**. The four walls have different asymmetric tab patterns, which match only their intended slot positions and orientation. Seat their lower rails against the top of the base. The mitered corners have clearance so adjacent walls do not intersect.

Default slots are **2.4 mm deep**; tabs are **2.25 mm deep**. Fit clearance is **0.3 mm total**, or **0.15 mm per side**. These are locating slip fits, not locking snap fits. Secure the walls after checking fit if mounting the base vertically. Increase `--fit-clearance` and regenerate if your printer needs more clearance. A separate mounting bracket/spacer is not included.

The central post has a **5.4 mm bore** running all the way through the base. Feed wiring out underneath for the 5 mm emitter. Insert the tall end of the L-shaped hood bracket into the separate diagonal slot at the **+x,+y corner**. Slide the hood's external socket over its inward-pointing arm, with the closed cap upward. The socket roof seats on the arm to set its height.

The hood is a **32 mm diameter inverted cup**, with its open lower rim at the emitting-point height. It blocks direct light toward the room while leaving downward/rearward rays available for projection. Its upper support arm is above the emitting point, and its upright sits far from the light in a corner-shadow direction. This avoids the large shadow produced by a bracket close to the emitter. The hood is removable; the walls remain open along their upper artwork outlines.

## Optical positioning

Keep the base parallel to the receiving wall, **20 mm in front of it**. The emitting point must be at **(0, 0, 49) mm**, measured from the center of the base underside, so it is 69 mm from the receiving wall. The post top is at z=43 mm; the emitter is 6 mm beyond it. The assembled hood reaches z=54.5 mm. The default combined image is **1,000 mm wide**.

The light must illuminate all four sides and shine back toward the receiving wall. Use a small exposed, low-heat emitter with broad angular coverage. The command-line preview assumes an ideal point source and opaque material. Shadow Studio additionally offers approximate brightness falloff and finite-source blur; neither preview simulates material scattering.

The base obscures most of the middle of the image, within the fish picture's blank central region. An uncovered wiring bore can transmit a small central spot. Corner ribs, tabs/rails, bridges and the hood arm can also cast shadows. These are included in the preview. Removing upper wall material permits some rearward light outside the image boundary. No physical print, fit or illumination test has been performed.

## Generate with another image

Run in PowerShell:

```powershell
cd C:\Users\Joe\Projects\3d_projection
.\.venv\Scripts\python.exe generate.py "C:\path\to\your-image.png" --output-dir "output\new-image"
```

That directory receives the seven print files, ZIP, assembly guide, previews and report. Transparent pixels become white. Dark pixels become shadow-casting material; the original aspect ratio is preserved.

For a new environment:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

Examples:

```powershell
.\.venv\Scripts\python.exe generate.py fish.jpg --fit-clearance 0.4
.\.venv\Scripts\python.exe generate.py fish.jpg --size 140 --depth 100 --projection-width 1200 --wall-gap 25
.\.venv\Scripts\python.exe generate.py --light-height 54 --emitter-diameter 5 --emitter-height 6
.\.venv\Scripts\python.exe generate.py --bridge-width 1.0 --min-feature 0.8
.\.venv\Scripts\python.exe generate.py --output-dir output --skip-previews
.\.venv\Scripts\python.exe generate.py --help
```

`--depth` is the nominal optical envelope used to calculate the default light height (`base_thickness + depth / 2`). The actual walls are trimmed to the artwork. `--light-height` overrides the emitting-point height from the base underside. Changing the light position, wall gap or projection size requires regenerating the masks. The receiving wall must remain parallel to the base.

## Previews and validation

- `projection_preview.png`: the actual assembled geometry ray-tested against one receiving wall, including the hood and supports.
- `projection_mask.png`: the same result as a binary image.
- `generation_report.json`: dimensions, per-part mesh checks, print-to-assembly transforms and fit settings.
- `shadow.svg` and `apertures_*.svg`: diagnostic 2D artwork masks; the tabs and lower rails are added in 3D.
- `model_render.png`, `assembly_exploded.png`, `projector_preview.blend`: optional Blender assembly views. The gold sphere is a placement marker, not printable geometry.

Refresh the optional views with Blender 4.3 or later:

```powershell
blender --background --python render_model.py
blender --background --python render_model.py -- "output\new-image"
```

Blender is not needed to generate the print files or ray-traced projection. Run the regression tests with:

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

Tests verify seven connected, watertight parts, flat wall/bracket orientation, an uninterrupted post bore, retained base floor, tab/slot clearance, rejection of wrong-side wall placement, hood light blocking without a broad support shadow, and image alignment after reassembling the exported STLs.
