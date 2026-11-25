import sys

import bpy
from mathutils import Vector

shape, color, output = sys.argv[sys.argv.index("--") + 1:]
bpy.ops.object.select_all(action="SELECT")
bpy.ops.object.delete()
mesh = {
    "cube": bpy.ops.mesh.primitive_cube_add,
    "sphere": bpy.ops.mesh.primitive_uv_sphere_add,
    "torus": bpy.ops.mesh.primitive_torus_add,
}
mesh[shape]()
obj = bpy.context.object
if shape == "cube":
    bevel = obj.modifiers.new("Rounded edges", "BEVEL")
    bevel.width = 0.12
    bevel.segments = 4
    obj.modifiers.new("Weighted normals", "WEIGHTED_NORMAL")
else:
    for polygon in obj.data.polygons:
        polygon.use_smooth = True
material = bpy.data.materials.new(color)
material.diffuse_color = {
    "blue": (0.06, 0.30, 0.9, 1),
    "red": (0.8, 0.05, 0.07, 1),
    "gold": (0.9, 0.48, 0.07, 1),
}[color]
obj.data.materials.append(material)
bpy.ops.object.camera_add(location=(5, -7, 4))
camera = bpy.context.object
camera.rotation_euler = (-Vector(camera.location)).to_track_quat("-Z", "Y").to_euler()
camera.data.type = "ORTHO"
camera.data.ortho_scale = 4
scene = bpy.context.scene
scene.camera = camera
scene.render.engine = "BLENDER_WORKBENCH"
scene.display.render_aa = "OFF"
shading = scene.display.shading
shading.light = "STUDIO"
shading.color_type = "MATERIAL"
shading.background_type = "WORLD"
shading.show_cavity = True
scene.render.resolution_percentage = 100
scene.render.resolution_x = 320
scene.render.resolution_y = 180
scene.render.fps = 24
scene.frame_end = 48
obj.rotation_euler = (0.3, 0, 0)
obj.keyframe_insert(data_path="rotation_euler", frame=1)
obj.rotation_euler = (0.3, 0, 6.283185307)
obj.keyframe_insert(data_path="rotation_euler", frame=49)
for curve in obj.animation_data.action.fcurves:
    for keyframe in curve.keyframe_points:
        keyframe.interpolation = "LINEAR"
scene.frame_set(1)
scene.render.image_settings.file_format = "FFMPEG"
scene.render.ffmpeg.format = "MPEG4"
scene.render.ffmpeg.codec = "H264"
scene.render.filepath = "//preview.mp4"
bpy.ops.wm.save_as_mainfile(filepath=output)
bpy.ops.render.render(animation=True)
