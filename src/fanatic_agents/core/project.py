"""Project identity and repository configuration models."""

from typing import Annotated

from pydantic import BaseModel, ConfigDict, StringConstraints


NonEmptyStrictString = Annotated[
    str,
    StringConstraints(strict=True, strip_whitespace=True, min_length=1),
]


class StrictModel(BaseModel):
    """Base model that rejects unknown fields and implicit type coercion."""

    model_config = ConfigDict(extra="forbid", strict=True)


class ProjectInfo(StrictModel):
    """Human-readable project metadata."""

    name: NonEmptyStrictString


class RepositoryConfig(StrictModel):
    """Location and primary branch of the managed repository."""

    path: NonEmptyStrictString
    main_branch: NonEmptyStrictString = "main"

