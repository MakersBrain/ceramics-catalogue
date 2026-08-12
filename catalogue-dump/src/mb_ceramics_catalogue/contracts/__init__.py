"""Building an OpenAPI document from Pydantic models, and checking it for drift.

`ateliera-app` settled this question already: zod registries under
`packages/api-contract/src/`, one command that emits `generated/openapi.json`, a
`--check` mode that fails when the checked-in file has drifted, and the rule —
stated outright in AGENTS.md — that the generated document is never hand-edited.
This is the same shape on the Python side.

The properties that make it worth having, rather than a document someone writes
and hopes is true:

* **The schemas come from the models the service serialises with**, so a
  response cannot drift from its description without the build noticing.
* **The document is checked in**, so an API change appears in the diff of the
  pull request that makes it, where a reviewer sees it.
* **`--check` fails the build on drift**, which is what stops "regenerate it
  later" from becoming "the spec is six months old".

Shared by both services because there are two documents, not one — a tenant
reading the catalogue must not be handed a document containing a cancel-run
endpoint (§10.1).
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic import BaseModel
from pydantic.json_schema import GenerateJsonSchema, JsonSchemaValue, models_json_schema

#: OpenAPI 3.1 aligns JSON Schema properly, which matters here because the
#: catalogue is full of nullable fields and 3.0's `nullable: true` cannot
#: express a union.
OPENAPI_VERSION = "3.1.0"

#: Where `$ref`s point. Pydantic defaults to `#/$defs/`, which is correct for a
#: standalone schema and wrong inside an OpenAPI document.
REF_TEMPLATE = "#/components/schemas/{model}"


class _Refs(GenerateJsonSchema):
    def generate(self, schema: Any, mode: Any = "validation") -> JsonSchemaValue:
        produced = super().generate(schema, mode=mode)
        produced.pop("$defs", None)
        return produced


@dataclass
class Parameter:
    name: str
    location: str = "query"
    description: str = ""
    required: bool = False
    schema: dict[str, Any] = field(default_factory=lambda: {"type": "string"})

    def render(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "in": self.location,
            "description": self.description,
            "required": self.required or self.location == "path",
            "schema": self.schema,
        }


@dataclass
class Operation:
    """One method on one path."""

    method: str
    path: str
    operation_id: str
    summary: str
    description: str = ""
    tags: Sequence[str] = ()
    parameters: Sequence[Parameter] = ()
    request: type[BaseModel] | None = None
    response: type[BaseModel] | None = None
    status: int = 200
    #: Status codes this operation can return as `application/problem+json`.
    errors: Sequence[int] = (400, 404)
    media_type: str = "application/json"
    deprecated: bool = False


class Problem(BaseModel):
    """RFC 9457 `application/problem+json`.

    One error schema referenced from every operation, in place of an
    undocumented `{"error": "..."}` string that each client had to guess at.
    """

    type: str = "about:blank"
    title: str
    status: int
    detail: str | None = None


class Registry:
    """The operations one service publishes."""

    def __init__(
        self,
        title: str,
        version: str,
        description: str,
        *,
        servers: Sequence[dict[str, str]] = (),
        security: bool = False,
    ) -> None:
        self.title = title
        self.version = version
        self.description = description
        self.servers = list(servers)
        self.security = security
        self.operations: list[Operation] = []
        self.extra_models: list[type[BaseModel]] = []

    def add(self, operation: Operation) -> Operation:
        self.operations.append(operation)
        return operation

    def declare(self, *models: type[BaseModel]) -> None:
        """Publish a schema that no operation returns directly.

        SSE payloads need this. OpenAPI 3.1 can name a `text/event-stream`
        response's media type but not the schema of each named event, so the
        payloads are defined in `components/schemas` and the operation
        description maps event names onto them. Inventing a fake path per
        payload would put endpoints in the document that do not exist, which is
        a worse lie than the one it fixes.
        """
        self.extra_models.extend(models)

    def models(self) -> list[type[BaseModel]]:
        seen: dict[str, type[BaseModel]] = {}
        for operation in self.operations:
            for model in (operation.request, operation.response):
                if model is not None:
                    seen[model.__name__] = model
        for model in self.extra_models:
            seen[model.__name__] = model
        seen[Problem.__name__] = Problem
        return [seen[name] for name in sorted(seen)]

    def build(self) -> dict[str, Any]:
        components = _component_schemas(self.models())

        paths: dict[str, Any] = {}
        for operation in self.operations:
            entry = paths.setdefault(operation.path, {})
            entry[operation.method.lower()] = _render(operation)

        document: dict[str, Any] = {
            "openapi": OPENAPI_VERSION,
            "info": {
                "title": self.title,
                "version": self.version,
                "description": self.description,
            },
            "paths": paths,
            "components": {"schemas": components},
        }
        if self.servers:
            document["servers"] = self.servers
        if self.security:
            document["components"]["securitySchemes"] = {
                "bearer": {"type": "http", "scheme": "bearer"}
            }
            document["security"] = [{"bearer": []}]
        return document

    # -- generation and drift ---------------------------------------------

    def write(self, target: Path) -> bool:
        """Write the document. Returns whether the file changed."""
        rendered = dumps(self.build())
        existing = target.read_text(encoding="utf-8") if target.exists() else ""
        if rendered == existing:
            return False
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(rendered, encoding="utf-8")
        return True

    def check(self, target: Path) -> list[str]:
        """Report why the checked-in document differs from this registry.

        A list rather than a boolean, because "the spec has drifted" is not a
        useful build failure and "GET /v1/manufacturers is in the code and not
        in the document" is.
        """
        if not target.exists():
            return [f"{target} does not exist; run the generator and commit it"]
        try:
            checked_in = json.loads(target.read_text(encoding="utf-8"))
        except json.JSONDecodeError as error:
            return [f"{target} is not valid JSON: {error}"]

        current = self.build()
        if checked_in == current:
            return []

        problems = []
        here = {(path, method) for path, entry in current["paths"].items() for method in entry}
        there = {(path, method) for path, entry in checked_in.get("paths", {}).items() for method in entry}
        for path, method in sorted(here - there):
            problems.append(f"{method.upper()} {path} is in the code but not in the document")
        for path, method in sorted(there - here):
            problems.append(f"{method.upper()} {path} is in the document but not in the code")

        schemas_here = set(current["components"]["schemas"])
        schemas_there = set(checked_in.get("components", {}).get("schemas", {}))
        for name in sorted(schemas_here - schemas_there):
            problems.append(f"schema {name} is new")
        for name in sorted(schemas_there - schemas_here):
            problems.append(f"schema {name} was removed")

        return problems or ["the document has drifted; regenerate and commit it"]


def _render(operation: Operation) -> dict[str, Any]:
    responses: dict[str, Any] = {}
    if operation.response is not None:
        responses[str(operation.status)] = {
            "description": operation.summary,
            "content": {
                operation.media_type: {"schema": {"$ref": REF_TEMPLATE.format(model=operation.response.__name__)}}
            },
        }
    else:
        responses[str(operation.status)] = {
            "description": operation.summary,
            "content": {operation.media_type: {}},
        }

    for status in operation.errors:
        responses[str(status)] = {
            "description": _REASONS.get(status, "Error"),
            "content": {
                "application/problem+json": {"schema": {"$ref": REF_TEMPLATE.format(model="Problem")}}
            },
        }

    rendered: dict[str, Any] = {
        "operationId": operation.operation_id,
        "summary": operation.summary,
        "responses": responses,
    }
    if operation.description:
        rendered["description"] = operation.description
    if operation.tags:
        rendered["tags"] = list(operation.tags)
    if operation.parameters:
        rendered["parameters"] = [parameter.render() for parameter in operation.parameters]
    if operation.request is not None:
        rendered["requestBody"] = {
            "required": True,
            "content": {
                "application/json": {
                    "schema": {"$ref": REF_TEMPLATE.format(model=operation.request.__name__)}
                }
            },
        }
    if operation.deprecated:
        rendered["deprecated"] = True
    return rendered


_REASONS = {
    400: "Bad Request",
    401: "Unauthorized",
    404: "Not Found",
    409: "Conflict",
    422: "Unprocessable Content",
    503: "Service Unavailable",
}


def _component_schemas(models: Sequence[type[BaseModel]]) -> dict[str, Any]:
    if not models:
        return {}
    _, definitions = models_json_schema(
        [(model, "serialization") for model in models],
        ref_template=REF_TEMPLATE,
        schema_generator=_Refs,
    )
    return dict(sorted(definitions.get("$defs", {}).items()))


def dumps(document: dict[str, Any]) -> str:
    """Stable JSON, so the only diffs are real changes.

    Sorted keys and a trailing newline: without both, regenerating produces a
    diff every time and people stop reading them.
    """
    return json.dumps(document, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def assert_read_only(document: dict[str, Any]) -> list[str]:
    """Every operation must be a `get`.

    `catalogue-service` promised read-only in a docstring, which nothing
    enforced. After the move to a generated spec it is this assertion, and it
    fails the build if anyone adds a write path — so the property is stronger
    than it was, not weaker (§10.2).
    """
    offenders = []
    for path, entry in document.get("paths", {}).items():
        for method in entry:
            if method.lower() != "get":
                offenders.append(f"{method.upper()} {path}")
    return offenders
