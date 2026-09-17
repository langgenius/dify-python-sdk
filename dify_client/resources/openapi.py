"""Apps through ``/openapi/v1``, shaped like the console's.

The same verbs, so moving between the two surfaces does not mean relearning
them. What differs is what each can do, and that is said rather than hidden:
``/openapi/v1`` has no publish endpoint, so :meth:`OpenApiApps.publish` refers
the caller to the console.
"""

from __future__ import annotations

from typing import Any

from ..lifecycle import Deployment
from ._base import Resource

__all__ = ["OpenApiApps"]


class OpenApiApps(Resource):
    """Apps as ``/openapi/v1`` reports and accepts them."""

    def list(self, *, mode: str | None = None, page: int = 1, limit: int = 20):
        """List the workspace's apps."""
        return self._client._apps(mode=mode, page=page, limit=limit)

    def retrieve(self, name_or_id: str):
        """Find one app by id, or by exact name."""
        from ..exceptions import ValidationError

        found = [
            app
            for app in self.list(limit=100)
            if app.id == name_or_id or app.name == name_or_id
        ]
        if not found:
            msg = f"No app called {name_or_id!r} in this workspace."
            raise ValidationError(msg)
        if len(found) > 1:
            ids = ", ".join(a.id for a in found)
            msg = f"{len(found)} apps are called {name_or_id!r}. Pass one of: {ids}."
            raise ValidationError(msg)
        return found[0]

    def export(self, app: Any, *, include_secret: bool = False) -> str:
        """One app's DSL."""
        return self._client._export_app(_app_id(app), include_secret=include_secret)

    def import_definition(
        self, definition: Any, *, app_id: str | None = None, name: str | None = None
    ) -> Deployment:
        """Write a definition to Dify as a draft."""
        result = self._client._deploy(definition, app_id=app_id, name=name)
        resolved = result.app_id or app_id or ""
        if not resolved:
            return Deployment(
                imported=False,
                error=result.error or "Dify returned no app id.",
            )
        return Deployment(
            imported=True,
            app_id=resolved,
            app_mode=str(result.app_mode or ""),
            created=app_id is None,
            warnings=tuple(result.warnings or ()),
            payload={"import": result.__dict__},
        )

    def deploy(
        self, definition: Any, *, app_id: str | None = None, name: str | None = None
    ) -> Deployment:
        """Import a definition. It stops at a draft — see :meth:`publish`."""
        return self.import_definition(definition, app_id=app_id, name=name)

    def publish(self, app: Any) -> Deployment:
        """Not available here. ``/openapi/v1`` has no publish endpoint."""
        from ..exceptions import ValidationError

        msg = (
            "/openapi/v1 can import a definition but not publish it, and the "
            "Service API runs only published work. Publish through the console:"
            " DifyManagement.apps.publish(app_id)."
        )
        raise ValidationError(msg)

    def check_dependencies(self, app: Any) -> dict[str, Any]:
        """Plugins the app needs that this workspace does not have."""
        return self._client._check_dependencies(_app_id(app))

    def run(self, app: Any, inputs: Any = None, **kwargs: Any) -> Any:
        """Run an app. One path serves every mode; Dify picks the handler."""
        return self._client._run(_app_id(app), inputs, **kwargs)

    def upload_file(self, app: Any, file: Any, **kwargs: Any) -> dict[str, Any]:
        """Upload a file for an app's file inputs."""
        return self._client._upload_file(_app_id(app), file, **kwargs)


def _app_id(app: Any) -> str:
    for attribute in ("app_id", "id"):
        value = getattr(app, attribute, None)
        if value:
            return str(value)
    return str(app)
