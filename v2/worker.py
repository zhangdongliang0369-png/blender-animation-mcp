"""Entrypoint for Blender. Only reviewed recipes bundled here are executable."""
from pathlib import Path
import json
import math
import sys

import bpy
from mathutils import Vector

INPUT = Path("/inputs")
OUTPUT = Path("/output")
ENGINES = {"workbench": "BLENDER_WORKBENCH", "eevee": "BLENDER_EEVEE", "cycles": "CYCLES"}


def write_json(name, value):
    (OUTPUT / name).write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def open_blend(path):
    bpy.ops.wm.open_mainfile(filepath=str(path), use_scripts=False, load_ui=False)
    if bpy.data.libraries:
        raise ValueError("Linked Blender libraries must be made local and packed before import")


def import_model(path):
    suffix = path.suffix.lower()
    if suffix in {".blend", ".blend1"}:
        open_blend(path)
        return
    bpy.ops.wm.read_factory_settings(use_empty=True)
    if suffix in {".glb", ".gltf"}:
        bpy.ops.import_scene.gltf(filepath=str(path))
    elif suffix == ".fbx":
        bpy.ops.import_scene.fbx(filepath=str(path))
    elif suffix == ".obj":
        bpy.ops.wm.obj_import(filepath=str(path))
    elif suffix == ".stl":
        bpy.ops.wm.stl_import(filepath=str(path))
    else:
        raise ValueError(f"Unsupported model format: {suffix}")


def dependency_errors():
    missing = []
    for img in bpy.data.images:
        if img.source not in {"FILE", "TILED", "SEQUENCE", "MOVIE"} or img.packed_file or img.packed_files:
            continue
        original = Path(bpy.path.abspath(img.filepath))
        path = original.resolve()
        if not path.is_relative_to(INPUT) or not path.is_file():
            matches = [p for p in INPUT.rglob(original.name) if p.is_file() and p != original]
            if len(matches) != 1:
                missing.append(img.name)
                continue
            path = matches[0]
        img.filepath = str(path)
        try:
            img.pack()
        except RuntimeError:
            missing.append(img.name)
    return missing


def bounds(objects):
    vertices = [obj.matrix_world @ Vector(p) for obj in objects for p in obj.bound_box]
    if not vertices:
        raise ValueError("No mesh geometry to frame")
    low = Vector([min(p[i] for p in vertices) for i in range(3)])
    high = Vector([max(p[i] for p in vertices) for i in range(3)])
    return low, high


def report(missing):
    scene = bpy.context.scene
    bpy.context.view_layer.update()
    meshes = [obj for obj in scene.objects if obj.type == "MESH"]
    low, high = bounds(meshes)
    boxes = []
    for obj in meshes:
        obj_low, obj_high = bounds([obj])
        boxes.append((obj.name, obj_low, obj_high))
    overlaps = []
    for i, (name, a, b) in enumerate(boxes):
        for other, c, d in boxes[i + 1:]:
            if len(overlaps) < 200 and all(min(b[k], d[k]) - max(a[k], c[k]) > 1e-6 for k in range(3)):
                overlaps.append([name, other])
    write_json("model-report.json", {
        "blender_version": bpy.app.version_string,
        "units": {"system": scene.unit_settings.system, "scale_length": scene.unit_settings.scale_length,
                  "length_unit": scene.unit_settings.length_unit},
        "bounds": {"min": list(low), "max": list(high), "dimensions": list(high - low)},
        "objects": [{
            "name": obj.name, "type": obj.type, "parent": obj.parent.name if obj.parent else None,
            "local_matrix": [list(row) for row in obj.matrix_local],
            "world_pivot": list(obj.matrix_world.translation),
            "dimensions": list(obj.dimensions), "hidden_render": obj.hide_render,
            "hidden_viewport": obj.hide_viewport or obj.hide_get(),
            "vertices": len(obj.data.vertices) if obj.type == "MESH" else None,
            "faces": len(obj.data.polygons) if obj.type == "MESH" else None,
            "materials": [slot.material.name for slot in obj.material_slots if slot.material],
            "collections": [collection.name for collection in obj.users_collection],
        } for obj in scene.objects],
        "mesh_count": len(meshes), "object_count": len(scene.objects),
        "materials": sorted({slot.material.name for obj in meshes for slot in obj.material_slots if slot.material}),
        "missing_dependencies": missing, "image_count": len(bpy.data.images),
        "images": [{"name": image.name, "packed": bool(image.packed_file or image.packed_files)}
                   for image in bpy.data.images],
        "possible_aabb_overlaps": overlaps,
        "overlap_note": "Broad-phase bounding boxes only. This is not a clearance or identity assessment.",
    })


def target(name, kind=None):
    obj = bpy.context.scene.objects.get(name)
    if not obj or (kind and obj.type != kind):
        raise ValueError(f"Object not found or incompatible: {name}")
    return obj


def material_edit(obj, action):
    mat = bpy.data.materials.new(f"{obj.name} V2")
    mat.diffuse_color = (*action["rgb"], 1)
    mat.use_nodes = True
    shader = next((node for node in mat.node_tree.nodes if node.type == "BSDF_PRINCIPLED"), None)
    if shader:
        shader.inputs["Base Color"].default_value = mat.diffuse_color
        shader.inputs["Roughness"].default_value = action["roughness"]
        shader.inputs["Metallic"].default_value = action["metallic"]
        if action.get("texture"):
            tex = mat.node_tree.nodes.new("ShaderNodeTexImage")
            tex.image = bpy.data.images.load(str(INPUT / action["texture"]), check_existing=True)
            tex.image.pack()
            mat.node_tree.links.new(tex.outputs["Color"], shader.inputs["Base Color"])
    if obj.material_slots:
        slot = obj.material_slots[action["slot"]]
        slot.link = "OBJECT"
        slot.material = mat
    else:
        obj.data = obj.data.copy()
        obj.data.materials.append(mat)


def apply_edits(actions):
    scene = bpy.context.scene
    for action in actions:
        kind = action["kind"]
        if kind == "marker":
            scene.timeline_markers.new(action["name"], frame=action["frame"])
        elif kind == "camera_cut":
            marker = scene.timeline_markers.new(action["name"], frame=action["frame"])
            marker.camera = target(action["object_name"], "CAMERA")
        elif kind == "group":
            collection = bpy.data.collections.new(action["name"])
            scene.collection.children.link(collection)
            for name in action["objects"]:
                collection.objects.link(target(name))
        elif kind == "environment":
            scene.world = bpy.data.worlds.new("V2 Environment")
            scene.world.use_nodes = True
            nodes = scene.world.node_tree.nodes
            background = nodes.get("Background")
            background.inputs["Strength"].default_value = action["strength"]
            image = nodes.new("ShaderNodeTexEnvironment")
            image.image = bpy.data.images.load(str(INPUT / action["texture"]), check_existing=True)
            image.image.pack()
            scene.world.node_tree.links.new(image.outputs["Color"], background.inputs["Color"])
        else:
            obj = target(action["object_name"])
            if kind == "transform":
                for channel in ("location", "rotation_euler", "scale"):
                    if action.get(channel) is not None:
                        if channel == "rotation_euler":
                            obj.rotation_mode = "XYZ"
                        setattr(obj, channel, action[channel])
                        if action.get("frame") is not None:
                            obj.keyframe_insert(data_path=channel, frame=action["frame"])
            elif kind == "material":
                material_edit(target(obj.name, "MESH"), action)
            elif kind == "parent":
                parent = target(action["parent_name"])
                ancestor = parent
                while ancestor:
                    if ancestor == obj:
                        raise ValueError("A parent relationship cannot create a cycle")
                    ancestor = ancestor.parent
                world = obj.matrix_world.copy()
                obj.parent = parent
                obj.matrix_world = world
            elif kind == "follow":
                following = target(action["target_name"])
                if following == obj:
                    raise ValueError("Constraint cannot target the same object")
                constraint = obj.constraints.new("COPY_LOCATION" if action["mode"] == "location" else "DAMPED_TRACK")
                constraint.target = following
                if constraint.type == "DAMPED_TRACK":
                    constraint.track_axis = "TRACK_NEGATIVE_Z"
            elif kind == "camera":
                obj = target(obj.name, "CAMERA")
                obj.data = obj.data.copy()
                if action.get("lens") is not None:
                    obj.data.lens = action["lens"]
                if action.get("f_stop") is not None:
                    obj.data.dof.use_dof = True
                    obj.data.dof.aperture_fstop = action["f_stop"]
                if action.get("focus_distance") is not None:
                    obj.data.dof.focus_distance = action["focus_distance"]
                if action.get("exposure") is not None:
                    scene.view_settings.exposure = action["exposure"]
            elif kind == "light":
                obj = target(obj.name, "LIGHT")
                obj.data = obj.data.copy()
                obj.data.energy = action["power"]
                obj.data.color = action["rgb"]


def product_showcase(params):
    scene = bpy.context.scene
    meshes = [obj for obj in scene.objects if obj.type == "MESH" and not obj.hide_render]
    low, high = bounds(meshes)
    center = (low + high) / 2
    diameter = max((high - low).length, 0.01)
    name = params.get("camera_name", "V2 Product Camera")
    data = bpy.data.cameras.new(name)
    camera = bpy.data.objects.new(name, data)
    scene.collection.objects.link(camera)
    scene.camera = camera
    data.type = "ORTHO"
    data.ortho_scale = diameter * 1.30
    data.clip_start = max(0.00001, diameter / 1000)
    data.clip_end = max(100.0, diameter * 20)
    angle = math.radians(params["angle"])
    end_angle = angle + math.radians(params["orbit"])
    for frame, azimuth in ((params["start"], angle), (params["end"], end_angle)):
        offset = diameter * Vector((2 * math.cos(azimuth), 2 * math.sin(azimuth), 1.4))
        camera.location = center + offset
        camera.rotation_euler = (-offset).to_track_quat("-Z", "Y").to_euler()
        camera.keyframe_insert(data_path="location", frame=frame)
        camera.keyframe_insert(data_path="rotation_euler", frame=frame)
    for obj in scene.objects:
        if obj.type == "LIGHT":
            obj.hide_render = True
    if params.get("color"):
        for mesh in meshes:
            for slot in range(max(1, len(mesh.material_slots))):
                material_edit(mesh, {"rgb": params["color"], "roughness": 0.35, "metallic": 0, "slot": slot})
    for name, direction, power in [
        ("V2 Key", (1, -2, 3), 220), ("V2 Fill", (-2, 1, 1.5), 100), ("V2 Rim", (1, 2, 2), 160)
    ]:
        light = bpy.data.lights.new(name, "AREA")
        light.energy = power * diameter**2
        light.shape = "DISK"
        light.size = diameter
        obj = bpy.data.objects.new(name, light)
        scene.collection.objects.link(obj)
        offset = diameter * Vector(direction)
        obj.location = center + offset
        obj.rotation_euler = (-offset).to_track_quat("-Z", "Y").to_euler()
    scene.frame_start, scene.frame_end = params["start"], params["end"]
    scene.frame_set(params["start"])


def configure(scene, config):
    if config.get("camera"):
        scene.camera = target(config["camera"], "CAMERA")
    if scene.camera is None:
        raise ValueError("No active camera. First run product_showcase or select an existing camera.")
    scene.render.engine = ENGINES[config["engine"]]
    scene.render.resolution_x, scene.render.resolution_y = config["width"], config["height"]
    scene.render.resolution_percentage = 100
    scene.render.fps = config["fps"]
    scene.render.fps_base = 1
    scene.render.image_settings.file_format = "PNG"
    scene.render.use_file_extension = True
    if hasattr(scene.render, "use_motion_blur"):
        scene.render.use_motion_blur = config["motion_blur"]
    elif config["motion_blur"]:
        raise ValueError("This engine build does not expose motion blur")
    if config["engine"] == "workbench":
        scene.display.render_aa = "OFF"
        scene.display.shading.light = "STUDIO"
        scene.display.shading.color_type = "MATERIAL"
        scene.display.shading.show_cavity = True
    if config["engine"] == "eevee" and hasattr(scene, "eevee"):
        scene.eevee.taa_render_samples = config["samples"]
    if config["engine"] == "cycles":
        scene.cycles.samples = config["samples"]
        scene.cycles.use_denoising = config["denoise"]
        scene.cycles.device = "CPU"
    if config["device"] == "GPU":
        raise ValueError("No GPU worker is configured")


def render(config, frames, numbered=False):
    scene = bpy.context.scene
    configure(scene, config)
    for index, frame in enumerate(frames, 1):
        scene.frame_set(frame)
        name = f"seq-{index:06d}.png" if numbered else f"frame-{frame}.png"
        scene.render.filepath = str(OUTPUT / name)
        bpy.ops.render.render(write_still=True)
        (OUTPUT / "progress.json").write_text(json.dumps({"done": index, "total": len(frames)}))


def main():
    spec = json.loads((INPUT / "job.json").read_text())
    actual = {"blender": bpy.app.version_string, "python": sys.version.split()[0],
              "engine": (spec.get("render") or {}).get("engine"), "device": "CPU"}
    write_json("environment.json", actual)
    required = spec.get("blender_series")
    if required and not bpy.app.version_string.startswith(required + "."):
        raise ValueError(f"Task requires Blender {required}; worker is {bpy.app.version_string}")
    operation = spec["operation"]
    if operation == "probe":
        if spec.get("render"):
            bpy.ops.wm.read_factory_settings(use_empty=False)
            render(spec["render"], [1])
        return
    if operation == "import":
        import_model(OUTPUT / "converted.glb" if spec.get("cad") else INPUT / spec["model"])
        missing = dependency_errors()
        report(missing)
        if missing:
            raise ValueError("Missing or unpackable file dependencies: " + ", ".join(missing))
    else:
        open_blend(INPUT / "project.blend")
        if operation == "inspect":
            report(dependency_errors())
        elif operation == "product_showcase":
            product_showcase(spec["parameters"])
        elif operation == "edit_objects":
            apply_edits(spec["actions"])
        elif operation in {"render_image", "render_animation", "extract_frames"}:
            render(spec["render"], spec["frames"], numbered=operation == "render_animation")
        else:
            raise ValueError("Unknown approved operation")
    if operation in {"import", "product_showcase", "edit_objects"}:
        bpy.context.preferences.filepaths.save_version = 0
        bpy.context.scene.render.filepath = "//render"
        bpy.ops.wm.save_as_mainfile(filepath=str(OUTPUT / "scene.blend"))


if __name__ == "__main__":
    main()
