"""Small HTTP client for Loom's generation load/export routes."""

import json
import logging
import pathlib
from hashlib import sha256
from typing import Any, Dict, Iterator
from urllib.parse import quote

import requests


REQUEST_TIMEOUT_SECONDS = 30
UPLOAD_TIMEOUT_SECONDS = 60 * 60


def generation_id(project_id: str, files: list[pathlib.Path]) -> str:
    """Return a retry-stable generation ID for one hydrated META snapshot."""
    digest = sha256()
    for path in sorted(files, key=lambda item: str(item)):
        digest.update(str(path.name).encode("utf-8"))
        digest.update(b"\0")
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        digest.update(b"\0")
    return f"etl-{sha256(project_id.encode('utf-8')).hexdigest()[:12]}-{digest.hexdigest()[:32]}"


def _headers(access_token: str) -> Dict[str, str]:
    return {"Authorization": f"bearer {access_token}"}


def _error(response: requests.Response) -> RuntimeError:
    try:
        body: Any = response.json()
        message = body.get("error", body) if isinstance(body, dict) else body
    except ValueError:
        message = response.text[:1000]
    return RuntimeError(f"Loom request failed ({response.status_code}): {message}")


def load_generation(
    loom_url: str,
    project_id: str,
    generation: str,
    files: list[pathlib.Path],
    access_token: str,
    auth_resource_path: str = "",
) -> Dict[str, Any]:
    """Upload a complete META snapshot to Loom."""
    url = (
        f"{loom_url.rstrip('/')}/api/v1/datasets/"
        f"{quote(project_id, safe='')}/generations/{quote(generation, safe='')}"
    )
    handles = []
    multipart = []
    try:
        for path in sorted(files, key=lambda item: str(item)):
            handle = path.open("rb")
            handles.append(handle)
            multipart.append(("file", (path.name, handle, "application/x-ndjson")))
        data = {"project": project_id, "generation": generation}
        if auth_resource_path:
            data["auth_resource_path"] = auth_resource_path
        logging.info("Uploading %d META files to Loom generation %s", len(files), generation)
        response = requests.post(
            url,
            headers=_headers(access_token),
            data=data,
            files=multipart,
            timeout=UPLOAD_TIMEOUT_SECONDS,
        )
        if not response.ok:
            raise _error(response)
        return response.json()
    finally:
        for handle in handles:
            handle.close()


def export_generation(
    loom_url: str,
    project_id: str,
    generation: str,
    access_token: str,
) -> Iterator[Dict[str, Any]]:
    """Stream raw FHIR resources from one Loom generation."""
    url = (
        f"{loom_url.rstrip('/')}/api/v1/datasets/"
        f"{quote(project_id, safe='')}/generations/{quote(generation, safe='')}/export"
    )
    with requests.get(
        url,
        headers={**_headers(access_token), "Accept": "application/x-ndjson"},
        stream=True,
        timeout=(REQUEST_TIMEOUT_SECONDS, UPLOAD_TIMEOUT_SECONDS),
    ) as response:
        if not response.ok:
            raise _error(response)
        for line in response.iter_lines(decode_unicode=True):
            if not line or not line.strip():
                continue
            try:
                resource = json.loads(line)
            except json.JSONDecodeError as exc:
                raise RuntimeError("Loom export returned invalid NDJSON") from exc
            if not isinstance(resource, dict):
                raise RuntimeError("Loom export returned a non-object resource")
            yield resource
