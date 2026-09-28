"""What this Dify can actually do, asked rather than assumed.

The running version comes from ``GET /v1/``, the Service API's own index: it
needs no credential, it is always served, and it carries ``server_version``.
Two other places look like they would answer and do not —
``/openapi/v1/_version`` is off unless ``OPENAPI_ENABLED`` is set, and
``/console/api/version`` reports the *latest released* version rather than the
one in front of you, which is a confidently wrong answer rather than a missing
one.

A version is still a poor proxy for what a server can do: self-hosted
deployments turn features off individually, so two on the same version do not
agree. So the version is reported, and the capabilities are probed.

So this asks. Each capability is probed against the route that serves it, and
the answer is one of three things — available, absent, or not determined,
because probing it needed a credential that was not supplied.

    compat = probe(host="https://dify.example", management=management)
    print(compat.summary())
    compat.require("triggers")        # raises, naming what is missing and why

Used by the live test harness to skip what a server cannot do, rather than
reporting a failure that is really an absence.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

import httpx

__all__ = ["Capability", "Compatibility", "KNOWN_CAPABILITIES", "probe"]

#: DSL version this SDK writes. Dify rejects a document from the future and
#: asks to confirm one from far enough in the past.
DSL_VERSION = "0.7.0"

#: Every capability this SDK knows to ask about, and what it means.
KNOWN_CAPABILITIES: dict[str, str] = {
    "service_api": "the /v1 surface answers, and reports its version",
    "openapi": "the /openapi/v1 surface is served (OPENAPI_ENABLED)",
    "console": "the console API answers, so apps can be created",
    "console_csrf": "console writes need a CSRF token alongside the access token",
    "triggers": "workflows can start themselves on a schedule or a webhook",
    "human_input": "runs can pause for a person to fill in a form",
    "workflow_events": "a run's event stream can be reopened after a pause or a drop",
    "agents": "the workspace keeps agents, on their own roster",
    "skills": "agents can be given skills",
    "child_chunks": "documents support parent-child chunking",
}


@dataclass(frozen=True)
class Capability:
    """Whether one thing works here, and how that was determined."""

    name: str
    #: True, False, or None when it could not be determined.
    available: bool | None
    detail: str = ""

    @property
    def known(self) -> bool:
        return self.available is not None

    def __bool__(self) -> bool:
        return self.available is True

    def __str__(self) -> str:
        mark = {True: "yes", False: "no", None: "?"}[self.available]
        return f"{self.name}: {mark}" + (f" ({self.detail})" if self.detail else "")


@dataclass(frozen=True)
class Compatibility:
    """What one Dify deployment supports."""

    host: str
    #: The running version, when the server would say. Empty otherwise — an
    #: empty string here means "not reported", never "old".
    version: str = ""
    edition: str = ""
    #: The DSL version this SDK writes, for comparing against an import's reply.
    dsl_version: str = DSL_VERSION
    capabilities: dict[str, Capability] = field(default_factory=dict)

    def __getitem__(self, name: str) -> Capability:
        try:
            return self.capabilities[name]
        except KeyError:
            known = ", ".join(sorted(KNOWN_CAPABILITIES))
            msg = f"No capability called {name!r}. Known: {known}."
            raise KeyError(msg) from None

    def supports(self, name: str) -> bool:
        """Whether this Dify has it. Unknown counts as no."""
        return bool(self[name])

    def undetermined(self) -> list[str]:
        """Capabilities that could not be probed with the credentials given."""
        return sorted(n for n, c in self.capabilities.items() if not c.known)

    def missing(self) -> list[str]:
        """Capabilities this Dify does not have."""
        return sorted(n for n, c in self.capabilities.items() if c.available is False)

    def require(self, *names: str) -> Compatibility:
        """Raise unless every named capability is available here.

        The point of a pre-flight: a workflow that uses a trigger node deploys
        into an app that never starts, on a Dify without triggers, and nothing
        says so until it silently does not run.
        """
        lacking = [n for n in names if not self.supports(n)]
        if not lacking:
            return self
        from .exceptions import ValidationError

        detail = "; ".join(
            f"{n} — {self[n].detail or KNOWN_CAPABILITIES[n]}" for n in lacking
        )
        msg = f"{self.host} does not support: {detail}."
        raise ValidationError(msg)

    def summary(self) -> str:
        """A readable report, for putting in a log or a test header."""
        head = f"Dify at {self.host}"
        if self.version:
            head += f" — {self.version} {self.edition}".rstrip()
        else:
            head += " — version not reported (could not reach GET /v1/)"
        lines = [head, f"  SDK writes DSL {self.dsl_version}"]
        lines += [f"  {cap}" for _, cap in sorted(self.capabilities.items())]
        return "\n".join(lines)

    def __str__(self) -> str:
        return self.summary()


def _ask(probe: Callable[[], bool | None], unknown: str) -> Capability:
    """Run one probe, turning a transport failure into "not determined"."""
    try:
        return Capability("", probe(), "")
    except Exception as failure:  # noqa: BLE001 - an absent answer, not a crash
        return Capability("", None, f"{unknown}: {failure}"[:120])


def probe(
    host: str | None = None,
    *,
    management: Any = None,
    app: Any = None,
    timeout: float = 5.0,
) -> Compatibility:
    """Ask a Dify what it supports.

    Args:
        host: Where Dify is. Left out, taken from ``DIFY_HOST``, or from
            ``management`` if one is given.
        management: A :class:`~dify_client.DifyManagement`. Without one, the
            console-side capabilities cannot be determined.
        app: A :class:`~dify_client.DifyApp`. Without one, the Service-API
            capabilities cannot be determined.
        timeout: Seconds to wait on each probe.

    Nothing here changes anything on the server: every probe is a read, or a
    call whose rejection is the answer.
    """
    from .secrets import resolve_host

    resolved = (
        host
        or getattr(management, "base_url", None)
        or getattr(app, "base_url", "").removesuffix("/v1")
    )
    resolved = resolve_host(resolved or None, "https://cloud.dify.ai")

    found: dict[str, Capability] = {}

    def record(name: str, available: bool | None, detail: str = "") -> None:
        found[name] = Capability(name, available, detail)

    with httpx.Client(base_url=resolved, timeout=timeout) as http:
        version = edition = ""
        # The Service API index. No credential, always served, and it is the
        # only place the running version is reliably reported.
        try:
            reply = http.get("/v1/")
            if reply.status_code == 200:
                version = str(reply.json().get("server_version") or "")
                record("service_api", True)
            else:
                record("service_api", False, f"HTTP {reply.status_code}")
        except httpx.HTTPError as failure:
            record("service_api", None, f"could not reach {resolved}: {failure}")

        # The edition is only on the /openapi/v1 probe, which is also how that
        # surface's presence is told.
        try:
            reply = http.get("/openapi/v1/_version")
            if reply.status_code == 200:
                body = reply.json()
                version = version or str(body.get("version") or "")
                edition = str(body.get("edition") or "")
                record("openapi", True)
            else:
                record("openapi", False, "OPENAPI_ENABLED is off")
        except httpx.HTTPError as failure:
            record("openapi", None, f"could not reach {resolved}: {failure}")

        try:
            record("console", http.get("/console/api/setup").status_code == 200)
        except httpx.HTTPError as failure:
            record("console", None, str(failure)[:80])

    _probe_console(management, record)
    _probe_service_api(app, record)

    return Compatibility(
        host=resolved, version=version, edition=edition, capabilities=found
    )


def _probe_console(management: Any, record: Callable[..., None]) -> None:
    """Capabilities only an account can see."""
    if management is None:
        for name in ("console_csrf", "triggers", "agents", "skills"):
            record(name, None, "needs management=…")
        return

    record(
        "console_csrf",
        bool(getattr(management, "_csrf_token", "")),
        (
            "this session carries one"
            if getattr(management, "_csrf_token", "")
            else "no CSRF token on this session; writes may be refused on Dify 1.17+"
        ),
    )

    for name, call in (
        ("agents", lambda: management.agents.list()),
        ("skills", lambda: management.skills.list()),
    ):
        try:
            call()
            record(name, True)
        except Exception as failure:  # noqa: BLE001 - absence is the answer
            record(name, False, str(failure)[:80])

    # Triggers are read per app, so this needs one to ask about.
    try:
        apps = management.apps.list(mode="workflow", limit=1)
    except Exception as failure:  # noqa: BLE001
        record("triggers", None, f"could not list apps: {failure}"[:100])
        return
    if not apps:
        record("triggers", None, "no workflow app in this workspace to ask about")
        return
    try:
        management.apps.triggers.list(apps[0].id)
        record("triggers", True)
    except Exception as failure:  # noqa: BLE001
        record("triggers", False, str(failure)[:80])


def _probe_service_api(app: Any, record: Callable[..., None]) -> None:
    """Capabilities an app key can see.

    Each of these is asked with a request the server rejects either way. What
    separates a Dify that has the feature from one that does not is *how* it
    refuses: a route that exists answers "not found, no such form"; a route
    that does not exist answers 404 from the router, with no message of its own.
    """
    if app is None:
        for name in ("human_input", "workflow_events", "child_chunks"):
            record(name, None, "needs app=…")
        return

    def route_exists(method: str, path: str, **kwargs: Any) -> bool:
        response = app._client.request(
            method,
            path,
            headers={"Authorization": f"Bearer {app.api_key}"},
            **kwargs,
        )
        if response.status_code == 404:
            try:
                body = response.json()
            except ValueError:
                return False
            # Dify's own 404 carries a code; the router's does not.
            return bool(body.get("code") or body.get("message"))
        return response.status_code < 500

    for name, method, path in (
        ("human_input", "GET", "/form/human_input/probe-not-a-real-token"),
        (
            "workflow_events",
            "GET",
            "/workflow/00000000-0000-0000-0000-000000000000/events",
        ),
    ):
        try:
            record(name, route_exists(method, path, params={"user": "sdk-probe"}))
        except Exception as failure:  # noqa: BLE001
            record(name, None, str(failure)[:80])

    record(
        "child_chunks",
        None,
        "needs a dataset key; ask with DifyKnowledge",
    )
