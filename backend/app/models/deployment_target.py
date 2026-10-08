"""Where a deployment ran.

A separate module rather than part of ``deployment_record`` because the target
now appears on planning contracts too, and ``deployment_record`` imports them —
defining it there would make the import circular. Both re-export it from here.
"""

from __future__ import annotations

from pydantic import BaseModel


class DeploymentTarget(BaseModel):
    """Where a deployment ran.

    History asked "which repository" and could not ask "which machine", so two
    runs of the same commit — one local, one on an EC2 instance — were
    indistinguishable in the list. This records the answer without carrying
    anything secret: the key file's *name* and path at most, never its contents,
    and never the environment values SSH was forwarded.

    A missing ``target`` simply means the deployment ran on the local daemon.
    """

    #: ``local`` or ``ec2``.
    kind: str = "local"
    host: str | None = None
    instance_id: str | None = None
    region: str | None = None
    ssh_user: str | None = None
    #: The key file as configured, for provenance only. Never the key material.
    key_file: str | None = None


__all__ = ["DeploymentTarget"]
