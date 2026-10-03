"""GitHub repository analysis endpoints."""

from __future__ import annotations

from fastapi import APIRouter, status

from app.agents.github_repository_analysis import analyze_github_repository
from app.models.github import (
    GitHubRepositoryAnalysisRequest,
    GitHubRepositoryAnalysisResponse,
)

router = APIRouter(prefix="/github", tags=["github"])


@router.post(
    "/repository/analyze",
    response_model=GitHubRepositoryAnalysisResponse,
    status_code=status.HTTP_200_OK,
    summary="Analyse a GitHub repository",
    description=(
        "Builds a repository profile and a deployment-readiness assessment for a GitHub "
        "repository, entirely through read-only MCP tools: metadata, file tree, "
        "manifests, recent commits and, on request, open issues and pull requests.\n\n"
        "Nothing is written. No commit, branch or pull request is created, and the "
        "GitHub server exposes no tool that could. Credentials come from the "
        "environment and are never returned or logged."
    ),
)
async def analyze_github_repository_endpoint(
    payload: GitHubRepositoryAnalysisRequest,
) -> GitHubRepositoryAnalysisResponse:
    """Analyse ``payload.owner``/``payload.repository``.

    Async because every GitHub interaction is an awaited MCP round trip to a
    subprocess.
    """
    profile, readiness = await analyze_github_repository(payload)
    return GitHubRepositoryAnalysisResponse(profile=profile, readiness=readiness)
