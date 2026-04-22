"""Thin HTTP client around the AutoClaude server API."""

from __future__ import annotations

from typing import Any

import httpx

from autoclaude.config import Profile


class ApiError(Exception):
    def __init__(self, message: str, *, status_code: int | None = None, payload: Any = None) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.payload = payload


class ApiClient:
    def __init__(self, profile: Profile, *, timeout: float = 30.0) -> None:
        if not profile.api_base:
            raise ApiError(f"api_base is empty for profile {profile.name!r}")
        self._client = httpx.Client(
            base_url=profile.api_base.rstrip("/"),
            timeout=timeout,
            headers={"Authorization": f"Api-Key {profile.api_key}"} if profile.api_key else {},
        )
        self._profile = profile

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> ApiClient:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def _unwrap(self, response: httpx.Response) -> Any:
        if response.status_code >= 400:
            try:
                payload = response.json()
            except Exception:  # noqa: BLE001
                payload = response.text
            raise ApiError(
                f"{response.request.method} {response.request.url} -> {response.status_code}",
                status_code=response.status_code,
                payload=payload,
            )
        if not response.content:
            return None
        return response.json()

    def get(self, path: str, **kwargs: Any) -> Any:
        return self._unwrap(self._client.get(path, **kwargs))

    def post(self, path: str, json: dict | None = None, **kwargs: Any) -> Any:
        return self._unwrap(self._client.post(path, json=json, **kwargs))

    def patch(self, path: str, json: dict | None = None, **kwargs: Any) -> Any:
        return self._unwrap(self._client.patch(path, json=json, **kwargs))

    # --- AutoClaude endpoints -------------------------------------------------

    def context(self) -> dict[str, Any]:
        return self.get("/api/ac/runner/context/")

    def plugin_refs(self) -> list[str]:
        payload = self.get("/api/ac/plugin/")
        return list(payload.get("plugin_refs") or [])

    def open_tick(self, *, runner_version: str, project_id: int | None = None) -> dict[str, Any]:
        body: dict[str, Any] = {"runner_version": runner_version}
        if project_id is not None:
            body["project_id"] = project_id
        return self.post("/api/ac/runner/tick/", json=body)

    def close_tick(self, tick_id: int, *, status: str, outcome: str = "", error_log: str = "", cost_usd: float = 0.0) -> dict[str, Any]:
        return self.patch(
            f"/api/ac/runner/{tick_id}/tick_close/",
            json={
                "status": status,
                "outcome": outcome,
                "error_log": error_log,
                "claude_cost_usd": cost_usd,
            },
        )

    def open_step(self, *, tick_id: int, agent_slug: str, ordinal: int, name: str) -> dict[str, Any]:
        return self.post(
            "/api/ac/runner/tick_step/",
            json={"tick_id": tick_id, "agent_slug": agent_slug, "ordinal": ordinal, "name": name},
        )

    def close_step(self, step_id: int, *, summary: str = "", error_log: str = "") -> dict[str, Any]:
        return self.patch(
            f"/api/ac/runner/{step_id}/tick_step_close/",
            json={"summary": summary, "error_log": error_log},
        )
