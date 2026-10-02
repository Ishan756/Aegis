"""Repository analysis endpoints."""

from __future__ import annotations

from fastapi import APIRouter, status

from app.agents.repository_analysis import analyze_repository_path
from app.models.repository import RepositoryAnalysisRequest, RepositoryProfile

router = APIRouter(prefix="/repository", tags=["repository"])


@router.post(
    "/analyze",
    response_model=RepositoryProfile,
    status_code=status.HTTP_200_OK,
    summary="Analyse a local repository",
    description=(
        "Performs a static, read-only inspection of a repository directory: "
        "languages, frameworks, package manager, container and CI configuration, "
        "tests, entry points, environment files and database usage. No repository "
        "code is executed and nothing is written to disk. The path must resolve "
        "inside the configured repository root."
    ),
)
def analyze_repository(payload: RepositoryAnalysisRequest) -> RepositoryProfile:
    """Analyse the repository at ``payload.path``.

    Synchronous because a scan of a bounded directory tree is fast and the
    result is returned inline. A larger analysis becomes a background job.
    """
    return analyze_repository_path(payload.path)
