"""Thin Confluence Data Center REST client.

Data Center, not Cloud: the v1 `/rest/api` surface with a Personal Access Token.
Cloud's v2 API differs enough that this class would need replacing, which is why
it stays thin and behind an interface the publish stage injects.
"""

from __future__ import annotations

import logging

import httpx

log = logging.getLogger(__name__)

_TIMEOUT = 30.0


class ConfluenceError(RuntimeError):
    """Confluence rejected a request or was unreachable."""


class ConfluenceClient:
    def __init__(self, base_url: str, pat: str, http: httpx.Client | None = None) -> None:
        self._base = base_url.rstrip("/")
        self._owns_http = http is None
        self._http = http or httpx.Client(base_url=self._base, timeout=_TIMEOUT)
        self._headers = {"Authorization": f"Bearer {pat}", "Content-Type": "application/json"}

    def close(self) -> None:
        if self._owns_http:
            self._http.close()

    def _request(self, method: str, path: str, **kwargs) -> dict:
        try:
            response = self._http.request(method, f"{self._base}{path}", headers=self._headers, **kwargs)
        except httpx.HTTPError as exc:
            raise ConfluenceError(f"{method} {path} failed: {exc}") from exc
        if response.status_code >= 400:
            raise ConfluenceError(f"{method} {path} returned {response.status_code}: {response.text[:300]}")
        if not response.content:
            return {}
        try:
            return response.json()
        except ValueError as exc:
            raise ConfluenceError(f"{method} {path} returned non-JSON body") from exc

    def create_page(self, space_key: str, parent_id: str, title: str, body: str) -> str:
        payload = {
            "type": "page",
            "title": title,
            "space": {"key": space_key},
            "ancestors": [{"id": parent_id}],
            "body": {"storage": {"value": body, "representation": "storage"}},
        }
        data = self._request("POST", "/rest/api/content", json=payload)
        page_id = data.get("id")
        if not page_id:
            raise ConfluenceError("create_page response contained no page id")
        log.info("created Confluence page %s", page_id)
        return str(page_id)

    def _current_version(self, page_id: str) -> int:
        data = self._request("GET", f"/rest/api/content/{page_id}?expand=version")
        try:
            return int(data["version"]["number"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ConfluenceError(f"cannot read version of page {page_id}") from exc

    def update_page(self, page_id: str, title: str, body: str) -> None:
        payload = {
            "id": page_id,
            "type": "page",
            "title": title,
            "version": {"number": self._current_version(page_id) + 1},
            "body": {"storage": {"value": body, "representation": "storage"}},
        }
        self._request("PUT", f"/rest/api/content/{page_id}", json=payload)
        log.info("updated Confluence page %s", page_id)

    def set_read_restrictions(self, page_id: str, usernames: list[str], group: str | None) -> None:
        restrictions: dict[str, list[dict]] = {"user": [{"type": "known", "username": u} for u in usernames]}
        if group:
            restrictions["group"] = [{"type": "group", "name": group}]
        payload = [{"operation": "read", "restrictions": restrictions}]
        self._request("PUT", f"/rest/api/content/{page_id}/restriction", json=payload)
        log.info("restricted page %s to %d user(s) and group %s", page_id, len(usernames), group)

    def delete_page(self, page_id: str) -> None:
        self._request("DELETE", f"/rest/api/content/{page_id}")
        log.info("deleted Confluence page %s", page_id)

    def page_url(self, page_id: str) -> str:
        return f"{self._base}/pages/viewpage.action?pageId={page_id}"
