"""Workspace management, grouped by what each verb acts on.

These speak to ``/console/api`` and authenticate as an account, not as an app.
Creating an app, publishing it and minting its key are all account-level, which
is why they are here and not on :class:`~dify_client.DifyApp`.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any, List

import httpx

from ..catalog import ModelProvider
from ..exceptions import TransportError
from ..lifecycle import Deployment
from ..results import Page
from ._base import Console, Resource

if TYPE_CHECKING:
    from ..app import DifyApp
    from ..console import App

__all__ = ["Agents", "Apps", "Keys", "ManagedApp", "Models", "Skills", "Triggers"]

#: The app modes Dify serves `/apps/<id>/workflows/publish` for. Every other
#: mode keeps its configuration on the app itself, so importing it is the whole
#: deployment and asking to publish answers 400.
_PUBLISHABLE_MODES = frozenset({"workflow", "advanced-chat"})


class Keys(Resource):
    """An app's Service-API keys.

    Dify reveals a key once, when it is minted. There is no reading one back,
    which is why :meth:`Apps.open` mints rather than fetches.
    """

    def list(self, app: Any) -> List[Any]:
        """The app's keys — ids and prefixes, never the secrets."""
        return self._client._list_api_keys(_app_id(app))

    def create(self, app: Any) -> Any:
        """Mint a key. This is the only moment its secret is visible."""
        return self._client._create_api_key(_app_id(app))

    def delete(self, app: Any, key: Any) -> None:
        """Revoke one key."""
        key_id = key if isinstance(key, str) else getattr(key, "id", key)
        self._client._delete_api_key(_app_id(app), key_id)


class Triggers(Resource):
    """The ways a published workflow starts by itself.

    A trigger node in a draft is only a drawing; Dify materialises the trigger,
    and mints a webhook's URL, on publish.
    """

    def list(self, app: Any) -> List[Any]:
        return self._client._triggers(_app_id(app))

    def webhook(self, app: Any, node_id: str = "trigger_webhook") -> Any:
        """The URL Dify minted for one webhook trigger node."""
        return self._client._webhook_trigger(_app_id(app), node_id)

    def set_enabled(self, app: Any, trigger: Any, enabled: bool = True) -> Any:
        """Pause or resume one trigger without unpublishing the app."""
        trigger_id = trigger if isinstance(trigger, str) else trigger.id
        return self._client._enable_trigger(_app_id(app), trigger_id, enabled=enabled)


class Skills(Resource):
    """The workspace's agent skills."""

    def list(self) -> List[Any]:
        return self._client._skills()

    def retrieve(self, name_or_id: str) -> Any:
        return self._client._skill(name_or_id)

    def create(self, skill: Any, *, publish: bool = True) -> Any:
        """Upload a skill, and publish it so agents can reference it.

        An unpublished skill exists in the workspace but no agent can use it,
        which is why publishing is the default.
        """
        return self._client._import_skill(skill, publish=publish)

    def delete(self, skill: Any) -> None:
        self._client._delete_skill(skill)


class Agents(Resource):
    """The workspace's agents. Dify keeps these off the app list."""

    def list(self) -> List[Any]:
        return self._client._agents()

    def retrieve(self, name_or_id: str) -> Any:
        return self._client._agent(name_or_id)


class Models(Resource):
    """What the workspace can call, and whether its credentials are in place."""

    def providers(self) -> List[ModelProvider]:
        """Every provider configured here, whether or not it has credentials."""
        return self._client._model_providers()

    def list(self, model_type: str = "llm") -> List[ModelProvider]:
        """Providers that offer this type, each carrying its models."""
        return self._client._models(model_type)

    def names(self, model_type: str = "llm") -> List[str]:
        """Model references spelled the way the workflow builder wants them."""
        return self._client._model_names(model_type)


class Tools(Resource):
    """Tool providers installed in the workspace, and their plugins."""

    def catalog(self) -> Any:
        return self._client._tools()

    def plugins(self) -> List[dict[str, Any]]:
        return self._client._plugins()

    def identifier(self, plugin: str) -> str:
        """The `name:version@hash` a workflow declares to have it installed."""
        return self._client._plugin_identifier(plugin)


def _app_id(app: Any) -> str:
    for attribute in ("app_id", "id"):
        value = getattr(app, attribute, None)
        if value:
            return str(value)
    return str(app)


class ManagedApp:
    """One app on Dify, from the account's side.

    Holds what management knows — the id, the mode, whether this session
    created it — and hands over a :class:`~dify_client.DifyApp` to actually
    call it.
    """

    def __init__(self, management: Any, deployment: Deployment) -> None:
        self._management = management
        self.deployment = deployment

    @property
    def id(self) -> str:
        return self.deployment.app_id

    @property
    def mode(self) -> str:
        return self.deployment.app_mode

    @property
    def created(self) -> bool:
        """Whether this session created it — what makes deleting it safe."""
        return self.deployment.created

    @property
    def api_key(self) -> str:
        """The Service-API key on hand, if one was minted or given."""
        return self.deployment.api_key

    def client(self, *, user: str = "dify-python-sdk") -> DifyApp:
        """A :class:`~dify_client.DifyApp` keyed for this app."""
        from ..app import DifyApp

        if not self.deployment.api_key:
            from ..exceptions import ValidationError

            msg = (
                "This app has no key on hand. Dify reveals a key only when it "
                "is minted: management.keys.create(app) makes one."
            )
            raise ValidationError(msg)
        return DifyApp(
            self.deployment.api_key,
            base_url=f"{self._management.base_url}/v1",
            user=user,
        )

    def export(self, *, include_secret: bool = False) -> str:
        """The app's DSL, ready to commit."""
        return self._management._export_app(self.id, include_secret=include_secret)

    def publish(self) -> Deployment:
        """Publish the current draft, making it the version that runs."""
        return self._management.apps.publish(self.id)

    def delete(self) -> None:
        self._management._delete_app(self.id)

    def __repr__(self) -> str:
        return f"ManagedApp(id={self.id!r}, mode={self.mode!r}, stage={self.deployment.stage})"


class Apps(Resource):
    """The workspace's apps, and the steps between a definition and a run."""

    def __init__(self, client: Console) -> None:
        super().__init__(client)
        self.keys = Keys(client)
        self.triggers = Triggers(client)

    # -- finding -----------------------------------------------------------

    def list(
        self, *, mode: str | None = None, name: str | None = None, **paging: Any
    ) -> Page[App]:
        """List the workspace's apps. ``page.all()`` walks every page."""
        return self._client._apps(mode=mode, name=name, **paging)

    def retrieve(self, name_or_id: str):
        """Find one app by id, or by exact name."""
        return self._client._app(name_or_id)

    def open(self, name_or_id: str, *, api_key: str | None = None) -> ManagedApp:
        """Take hold of an app that already exists.

        Mints a Service-API key unless one is given, because Dify reveals a key
        only at the moment it is created.
        """
        found = self.retrieve(name_or_id)
        return ManagedApp(
            self._client,
            Deployment(
                imported=True,
                published=True,
                app_id=found.id,
                app_mode=found.mode,
                api_key=api_key or self.keys.create(found.id).token,
                created=False,
            ),
        )

    def export(self, app: Any, *, include_secret: bool = False) -> str:
        """One app's DSL, ready to commit.

        Secrets are blanked unless asked for, so the default output is safe in
        a repository.
        """
        return self._client._export_app(_app_id(app), include_secret=include_secret)

    def delete(self, app: Any) -> None:
        """Delete an app and everything in it."""
        self._client._delete_app(_app_id(app))

    # -- the lifecycle, one step at a time ---------------------------------

    def import_definition(
        self, definition: Any, *, app_id: str | None = None, name: str | None = None
    ) -> Deployment:
        """Write a definition to Dify as a **draft**.

        Nothing runs this yet: the Service API runs the published version.
        :meth:`publish` is the next step, and :meth:`deploy` does both.

        Args:
            definition: A ``Workflow``, an ``Agent``, or DSL as a string.
            app_id: Overwrite this app instead of creating one.
            name: Override the name in the definition.
        """
        # A caller mistake is raised, not reported: it is not a state Dify is
        # in, and no retry or cleanup would help.
        if app_id and getattr(definition, "mode", None) == "agent":
            from ..exceptions import ValidationError

            msg = (
                "Dify's importer only creates new Agent apps; it cannot "
                f"overwrite {app_id}. Delete the app and deploy again, or "
                "deploy under a new name."
            )
            raise ValidationError(msg)

        try:
            if isinstance(definition, str):
                result = self._client._import_app(definition, app_id=app_id, name=name)
            else:
                result = self._client._deploy(definition, app_id=app_id, name=name)
        except (TransportError, httpx.TransportError) as failure:
            # Nothing came back. Whether Dify created the app is unknown, and
            # guessing either way would be wrong. The console client speaks to
            # httpx directly, so both spellings of the failure arrive here.
            return Deployment(
                indeterminate=True, error=str(failure), app_id=app_id or ""
            )
        except Exception as failure:  # noqa: BLE001 - Dify said no; that is a state
            return Deployment(imported=False, error=str(failure), app_id=app_id or "")

        # An import that answered 200 can still have failed. Passing an app_id
        # used to hide that: the id came back from the argument rather than the
        # reply, and the deploy carried on to publish whatever draft was there.
        if not _import_succeeded(result):
            return Deployment(
                imported=False,
                app_id=app_id or "",
                error=result.error or f"Dify reported the import as {result.status!r}.",
                warnings=tuple(result.warnings or ()),
                payload={"import": result.__dict__},
            )

        resolved = result.app_id or app_id or ""
        if not resolved:
            return Deployment(
                imported=False, error="Dify accepted the import but returned no app id."
            )
        return Deployment(
            imported=True,
            app_id=resolved,
            app_mode=str(result.app_mode or getattr(definition, "mode", "") or ""),
            created=app_id is None,
            warnings=tuple(result.warnings or ()),
            payload={"import": result.__dict__},
        )

    def publish(self, app: Any) -> Deployment:
        """Publish an app's draft, making it the version the Service API runs."""
        app_id = _app_id(app)
        published = self._client._publish_workflow(app_id, name="dify-python-sdk")
        return Deployment(
            imported=True,
            published=True,
            app_id=app_id,
            version=str((published or {}).get("id") or ""),
            payload={"publish": published or {}},
        )

    def deploy(
        self,
        definition: Any,
        *,
        app_id: str | None = None,
        name: str | None = None,
        api_key: str | None = None,
        publish: bool = True,
        key: bool = True,
    ) -> Deployment:
        """Import, publish, and mint a key — reporting what actually happened.

        Each step is optional, and each outcome is recorded as its own fact
        rather than a position on a ladder, so a caller can tell "nothing
        happened" from "the app exists but is unpublished" from "published but
        I have no key"::

            result = management.apps.deploy(workflow)
            result.raise_for_stage()          # or inspect result
        """
        imported = self.import_definition(definition, app_id=app_id, name=name)
        if not imported.imported:
            return imported

        # Read off what Dify imported, not off what was handed in: the same
        # Agent passed as exported YAML is a string, and a string has no
        # `.mode`. Deciding from the definition sent an Agent to
        # `/workflows/publish`, which only takes the two modes that have a
        # draft — so deploying an Agent from its DSL reported a failure and
        # never minted a key.
        mode = imported.app_mode or getattr(definition, "mode", "") or ""
        has_draft = mode in _PUBLISHABLE_MODES or not mode
        published, version, error = False, "", ""
        if publish:
            try:
                if mode == "agent":
                    # An Agent publishes through the roster, not through
                    # `/workflows/publish` — and it does need publishing:
                    # minting a key before it answers "Publish the Agent
                    # before enabling Web App or API access".
                    version = self._client._publish_agent(imported.app_id)
                    published = True
                elif not has_draft:
                    # A chat or completion app arrives live: its configuration
                    # is on the app itself, with no draft to publish.
                    published = True
                else:
                    result = self.publish(imported.app_id)
                    published, version = True, result.version
            except Exception as failure:  # noqa: BLE001 - reported as a fact
                # The app exists and is unpublished. The caller can retry the
                # publish or delete what was created.
                return _with(imported, error=str(failure))

        minted = api_key or ""
        if key and not minted:
            try:
                minted = self.keys.create(imported.app_id).token
            except Exception as failure:  # noqa: BLE001 - reported as a fact
                error = str(failure)

        return _with(
            imported,
            published=published,
            version=version,
            api_key=minted,
            error=error,
        )

    @contextmanager
    def temporary(
        self, definition: Any, *, name: str | None = None
    ) -> Iterator[ManagedApp]:
        """Deploy an app for the block, then delete it.

        What makes a billed test self-contained: no app to create by hand, no
        key to paste in, nothing left behind. Deleted even when the block
        raises, and even when the deploy itself fails partway — an app that was
        created but could not be published is still an app.
        """
        result = self.deploy(definition, name=name)
        app = ManagedApp(self._client, result)
        try:
            result.raise_for_stage()
            yield app
        finally:
            # An indeterminate deploy may or may not have created an app, so it
            # is swept by prefix rather than deleted by id that may not exist.
            if result.imported and result.created and result.app_id:
                try:
                    app.delete()
                # A cleanup failure must not replace the failure the caller
                # is already dealing with.
                except Exception:  # noqa: BLE001  # nosec B110
                    pass


#: Import statuses Dify reports as usable. Anything else is a failure, even
#: when the HTTP status was 200.
_IMPORT_OK = frozenset({"completed", "completed-with-warnings"})


def _import_succeeded(result: Any) -> bool:
    status = str(getattr(result, "status", "") or "")
    # An older Dify omits the field; an app id coming back is then the signal.
    return status in _IMPORT_OK if status else bool(getattr(result, "app_id", ""))


def _with(base: Deployment, **changes: Any) -> Deployment:
    """A copy of the deployment with these fields changed."""
    from dataclasses import replace

    return replace(base, **changes)
