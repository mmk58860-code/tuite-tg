from __future__ import annotations

import os
from dataclasses import dataclass

import httpx


class DockerManagerError(RuntimeError):
    pass


@dataclass
class ContainerInfo:
    container_id: str
    status: str


DOCKER_SOCKET = os.getenv("DOCKER_SOCKET", "/var/run/docker.sock")
DOCKER_NETWORK = os.getenv("DOCKER_NETWORK", "tuite-tg_default")
RSSHUB_IMAGE = os.getenv("MANAGED_RSSHUB_IMAGE", "diygod/rsshub:latest")


def docker_available() -> bool:
    return os.path.exists(DOCKER_SOCKET)


def _client() -> httpx.Client:
    if not docker_available():
        raise DockerManagerError("未挂载 Docker socket，无法在网页中管理 RSSHub 容器。")
    transport = httpx.HTTPTransport(uds=DOCKER_SOCKET)
    return httpx.Client(transport=transport, base_url="http://docker", timeout=30.0)


def _request(method: str, path: str, **kwargs) -> httpx.Response:
    with _client() as client:
        resp = client.request(method, path, **kwargs)
    if resp.status_code >= 400:
        raise DockerManagerError(f"Docker API {resp.status_code}: {resp.text[:500]}")
    return resp


def create_rsshub_container(
    name: str,
    host_port: int,
    twitter_auth_token: str = "",
    third_party_api: str = "",
    proxy_uri: str = "",
) -> ContainerInfo:
    env = [
        "CACHE_EXPIRE=30",
        f"TWITTER_AUTH_TOKEN={twitter_auth_token}",
        f"TWITTER_THIRD_PARTY_API={third_party_api}",
        f"PROXY_URI={proxy_uri}",
    ]
    payload = {
        "Image": RSSHUB_IMAGE,
        "Env": env,
        "ExposedPorts": {"1200/tcp": {}},
        "Labels": {
            "managed-by": "tuite-tg",
            "tuite-tg-rsshub": "true",
        },
        "HostConfig": {
            "RestartPolicy": {"Name": "unless-stopped"},
            "PortBindings": {
                "1200/tcp": [
                    {
                        "HostIp": "127.0.0.1",
                        "HostPort": str(host_port),
                    }
                ]
            },
            "NetworkMode": DOCKER_NETWORK,
        },
        "NetworkingConfig": {
            "EndpointsConfig": {
                DOCKER_NETWORK: {
                    "Aliases": [name],
                }
            }
        },
    }
    resp = _request("POST", f"/containers/create?name={name}", json=payload)
    container_id = resp.json()["Id"]
    _request("POST", f"/containers/{container_id}/start")
    return ContainerInfo(container_id=container_id, status="running")


def remove_container(container_id: str) -> None:
    if not container_id:
        return
    try:
        _request("POST", f"/containers/{container_id}/stop?t=10")
    except DockerManagerError:
        pass
    _request("DELETE", f"/containers/{container_id}?force=true")


def inspect_container(container_id: str) -> ContainerInfo:
    resp = _request("GET", f"/containers/{container_id}/json")
    data = resp.json()
    return ContainerInfo(
        container_id=container_id,
        status=str(data.get("State", {}).get("Status", "unknown")),
    )
