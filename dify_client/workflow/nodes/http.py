"""The HTTP node: calling an API from a workflow, and how Dify stores the call.

graphon owns this node's schema and runs it, so what is here is the shaping
Dify's editor does around that schema — the credential in a config of its own,
the headers as text rather than a mapping, the body as one of five kinds.
"""

from __future__ import annotations

import json as json_lib
from collections.abc import Mapping, Sequence
from typing import Any, cast

from graphon.nodes.http_request.entities import (
    BodyData,
    HttpRequestNodeAuthorization,
    HttpRequestNodeBody,
    HttpRequestNodeData,
    HttpRequestNodeTimeout,
)

from ..refs import Node, Text, VarRef, render
from ._errors import NodeError

__all__ = [
    "METHODS",
    "api_key",
    "basic",
    "bearer",
    "lines",
    "request_data",
]

#: The methods Dify's node accepts, lowercased as its editor writes them.
METHODS = ("get", "post", "put", "patch", "delete", "head", "options")


def _json_reference(value: Any) -> str:
    """Write a reference inside a JSON body as Dify's editor does.

    Dify substitutes the template into the body's text and then parses it, so
    ``{"q": "{{#start.q#}}"}`` sends the value as a JSON string. That is what
    this writes; for a number or an object sent unquoted, pass the body as a
    string. Anything else that is not JSON is still refused, rather than
    turned into its ``str()``.
    """
    if isinstance(value, (VarRef, Node)):
        return render(value)
    msg = f"Object of type {type(value).__name__} is not JSON serializable."
    raise TypeError(msg)


def bearer(token: Text) -> HttpRequestNodeAuthorization:
    """``Authorization: Bearer <token>`` on an HTTP node."""
    return _auth("bearer", token)


def basic(token: Text) -> HttpRequestNodeAuthorization:
    """``Authorization: Basic <token>`` on an HTTP node."""
    return _auth("basic", token)


def api_key(header: str, value: Text) -> HttpRequestNodeAuthorization:
    """A credential in a header of its own, such as ``X-API-Key``."""
    return _auth("custom", value, header=header)


def _auth(kind: str, value: Text, *, header: str = "") -> HttpRequestNodeAuthorization:
    # The model validates `config` before constructing it and rejects an
    # already-built one, so the config is handed over as a mapping.
    return HttpRequestNodeAuthorization(
        type="api-key",
        config=cast(Any, {"type": kind, "api_key": render(value), "header": header}),
    )


def lines(value: Mapping[str, Text] | str | None) -> str:
    """Render headers or query parameters the way Dify stores them.

    One ``name: value`` per line — the editor's own format, which the node
    parses by splitting each line on the first colon.
    """
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return "\n".join(f"{name}: {render(item)}" for name, item in value.items())


def request_data(
    *,
    url: Text,
    method: str,
    headers: Mapping[str, Text] | str | None,
    params: Mapping[str, Text] | str | None,
    json: Any = None,
    text: Text | None = None,
    form: Mapping[str, Text] | None = None,
    auth: HttpRequestNodeAuthorization | None,
    timeout: Sequence[int] | None,
    ssl_verify: bool,
    title: str,
) -> HttpRequestNodeData:
    """One HTTP node, with at most one body.

    Dify keeps the body kind and its content in one field, so "a request has
    one body" is a rule rather than a convention: sending two would silently
    keep whichever was written last.
    """
    if method.lower() not in METHODS:
        accepted = ", ".join(METHODS)
        msg = f"method={method!r} is not one Dify sends. Use one of: {accepted}."
        raise NodeError(msg)

    given = [
        name
        for name, value in (("json", json), ("text", text), ("form", form))
        if value is not None
    ]
    if len(given) > 1:
        msg = f"A request has one body, and {', '.join(given)} were given."
        raise NodeError(msg)

    if json is not None:
        payload = (
            json
            if isinstance(json, str)
            else json_lib.dumps(json, ensure_ascii=False, default=_json_reference)
        )
        body = HttpRequestNodeBody(
            type="json", data=[BodyData(key="", type="text", value=render(payload))]
        )
    elif text is not None:
        body = HttpRequestNodeBody(
            type="raw-text", data=[BodyData(key="", type="text", value=render(text))]
        )
    elif form is not None:
        body = HttpRequestNodeBody(
            type="form-data",
            data=[
                BodyData(key=key, type="text", value=render(value))
                for key, value in form.items()
            ],
        )
    else:
        body = HttpRequestNodeBody(type="none", data=[])

    return HttpRequestNodeData(
        title=title,
        method=cast(Any, method.lower()),
        url=render(url),
        authorization=auth or HttpRequestNodeAuthorization(type="no-auth"),
        headers=lines(headers),
        params=lines(params),
        body=body,
        timeout=(
            HttpRequestNodeTimeout(
                connect=timeout[0], read=timeout[1], write=timeout[2]
            )
            if timeout is not None
            else None
        ),
        ssl_verify=ssl_verify,
    )
