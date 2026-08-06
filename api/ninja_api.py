"""Small Django Ninja API used to evaluate typed API documentation."""

from typing import Any, Literal

from ninja import Field, NinjaAPI, Schema


api = NinjaAPI(
    title="ToolBox API",
    version="0.1.0",
    description="Typed ToolBox API experiment powered by Django Ninja.",
)


class HelloResponse(Schema):
    """Response returned by the typed query-parameter example."""

    message: str = Field(description="Greeting text")
    repeated: list[str] = Field(description="Greeting repeated the requested number of times")


class ProjectPreviewIn(Schema):
    """A project payload that is validated without writing to CloudBase."""

    name: str = Field(min_length=1, description="Project name")
    aircraft_type: Literal["A320", "B787"] = Field(
        default="A320",
        description="Supported aircraft type",
    )
    team: str = Field(default="", description="Team responsible for the project")
    sections: list[dict[str, Any]] = Field(
        default_factory=list,
        description="Free-form section data used by the current frontend",
    )


class ProjectPreviewOut(Schema):
    ok: bool
    data: ProjectPreviewIn
    note: str


@api.get(
    "/hello",
    response=HelloResponse,
    summary="Try typed query parameters",
    description="Change `name` and `repeat` in Swagger to see validation and hints.",
)
def hello(request, name: str = "ToolBox", repeat: int = 1):
    repeat = min(max(repeat, 1), 5)
    message = f"Hello, {name}!"
    return {"message": message, "repeated": [message] * repeat}


@api.post(
    "/project-preview",
    response=ProjectPreviewOut,
    summary="Validate a project payload",
    description="Validates the request body and returns it without writing to CloudBase.",
)
def project_preview(request, payload: ProjectPreviewIn):
    return {
        "ok": True,
        "data": payload,
        "note": "Preview only; no data was persisted.",
    }
