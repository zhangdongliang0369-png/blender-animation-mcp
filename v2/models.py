"""Schemas exposed by the production MCP tools."""
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

Name = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=120)]
Frame = Annotated[int, Field(ge=0, le=100000)]
Unit = Annotated[float, Field(ge=0, le=1, allow_inf_nan=False)]
Coordinate = Annotated[float, Field(ge=-10000, le=10000, allow_inf_nan=False)]
RGB = tuple[Unit, Unit, Unit]
Vector3 = tuple[Coordinate, Coordinate, Coordinate]
Engine = Literal["workbench", "eevee", "cycles"]


class Schema(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RenderSettings(Schema):
    engine: Engine = "workbench"
    mode: Literal["preview", "sample", "production"] = "preview"
    width: int = Field(854, ge=64, le=3840, multiple_of=2)
    height: int = Field(480, ge=64, le=2160, multiple_of=2)
    fps: int = Field(24, ge=1, le=60)
    samples: int = Field(16, ge=1, le=1024)
    denoise: bool = False
    device: Literal["CPU", "GPU"] = "CPU"
    motion_blur: bool = False
    camera: Name | None = None


class Showcase(Schema):
    angle: float = Field(-45, ge=-360, le=360)
    orbit: float = Field(24, ge=-180, le=180)
    start: Frame = 1
    end: Frame = 48
    color: RGB | None = (0.34, 0.38, 0.43)
    camera_name: Name = "V2 Product Camera"

    @model_validator(mode="after")
    def frame_range(self):
        if self.end <= self.start:
            raise ValueError("End frame must follow the start frame")
        return self


class Targeted(Schema):
    object_name: Name


class Transform(Targeted):
    kind: Literal["transform"]
    frame: Frame | None = None
    location: Vector3 | None = None
    rotation_euler: Vector3 | None = None
    scale: Vector3 | None = None

    @model_validator(mode="after")
    def channels(self):
        if self.location is None and self.rotation_euler is None and self.scale is None:
            raise ValueError("Supply a transform channel")
        return self


class Material(Targeted):
    kind: Literal["material"]
    rgb: RGB
    slot: int = Field(0, ge=0, le=127)
    metallic: Unit = 0
    roughness: Unit = 0.35
    texture_asset_id: str | None = None


class Parent(Targeted):
    kind: Literal["parent"]
    parent_name: Name


class Follow(Targeted):
    kind: Literal["follow"]
    mode: Literal["location", "look_at"]
    target_name: Name


class Camera(Targeted):
    kind: Literal["camera"]
    lens: float | None = Field(None, ge=1, le=250)
    f_stop: float | None = Field(None, ge=0.5, le=64)
    focus_distance: Coordinate | None = None
    exposure: float | None = Field(None, ge=-12, le=12)


class Light(Targeted):
    kind: Literal["light"]
    power: float = Field(ge=0, le=10000)
    rgb: RGB = (1, 1, 1)


class Marker(Schema):
    kind: Literal["marker"]
    name: Name
    frame: Frame


class CameraCut(Marker):
    kind: Literal["camera_cut"]
    object_name: Name


class Group(Schema):
    kind: Literal["group"]
    name: Name
    objects: list[Name] = Field(min_length=1, max_length=64)


class Environment(Schema):
    kind: Literal["environment"]
    texture_asset_id: str
    strength: float = Field(1, ge=0, le=20)


Edit = Annotated[
    Transform | Material | Parent | Follow | Camera | Light | Marker | CameraCut | Group | Environment,
    Field(discriminator="kind"),
]
