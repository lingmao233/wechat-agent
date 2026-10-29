from __future__ import annotations

"""Optional OpenViking REST bridge for scoped WeChat memories.

OpenViking's native session and search APIs are used instead of introducing a
second vector service.  SQL keeps the exact scope/audit ledger; this adapter
stores the extracted memory payload and the bounded original chat records in
separate scope directories, and treats the remote service as a semantic index.
If the service is absent or temporarily unavailable, callers keep working with
the local fallback.
"""

from dataclasses import dataclass
import hashlib
import json
import logging
import re
import time
from typing import Any

import httpx

from .config import Settings

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class OpenVikingHit:
    memory_id: str
    value: str
    scope: dict[str, str]
    score: float = 0.0
    uri: str = ""
    source_message_ids: tuple[str, ...] = ()
    memory_type: str = ""


def _is_unknown_member(value: str) -> bool:
    return str(value or "").strip().casefold() in {
        "",
        "unknown",
        "我",
        "自己",
        "me",
        "you",
        "you (me)",
    }


def _safe_segment(value: str, fallback: str = "unknown") -> str:
    # Keep Unicode wxids/display names readable while still blocking path
    # separators and control characters.  Stable WeChat wxids are normally
    # ASCII; preserving CJK names also prevents two different Chinese group
    # names from collapsing into the same ``_`` segment during migration.
    text = re.sub(r"[^\w._~-]+", "_", str(value or "").strip(), flags=re.UNICODE)
    if text in {"", ".", ".."}:
        return fallback
    return text[:180]


def peer_id_for_scope(
    *,
    account_id: str,
    group_id: str,
    member_id: str = "",
    task_id: str = "",
) -> str:
    """Return the single-segment OpenViking peer for one memory scope.

    OpenViking stores peer memories below ``peers/<peer_id>/memories`` and
    rejects peer IDs containing path separators.  The readable prefix helps
    operators identify a directory, while the digest keeps IDs deterministic
    and prevents sanitized names from colliding (for example two different
    non-ASCII wxids becoming the same underscore string).

    A member scope is keyed by account + group + member.  A group scope is
    keyed by account + group.  Task scopes add the task ID and, when present,
    the member ID so a member-owned task cannot collide with the shared group
    task of the same name.
    """

    account = str(account_id or "").strip()
    group = str(group_id or "").strip()
    member = str(member_id or "").strip()
    task = str(task_id or "").strip()
    if task:
        role = "task-member" if member else "task"
    elif member:
        role = "member"
    else:
        role = "group"

    canonical = json.dumps(
        {
            "account_id": account,
            "group_id": group,
            "member_id": member,
            "task_id": task,
            "role": role,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]
    labels = [
        "wechat-agent",
        role,
        _safe_segment(account, "account")[:32],
        _safe_segment(group, "group")[:48],
    ]
    if task:
        labels.append(_safe_segment(task, "task")[:48])
    if member:
        labels.append(_safe_segment(member, "member")[:48])
    readable = "-".join(labels).strip("-")
    # Keep room for the digest even if a caller supplies very long IDs.  The
    # digest remains the collision-resistant part of the identity.
    return f"{readable[:140].rstrip('-')}-{digest}"


def scope_uri(
    *,
    namespace: str,
    account_id: str,
    group_id: str,
    member_id: str = "",
    task_id: str = "",
    kind: str = "profiles",
) -> str:
    """Build a stable, human-auditable OpenViking scope URI."""

    root_peer = _safe_segment(f"user-{namespace}-peers-wechatagent", "wechatagent")
    parts = [
        "viking://user",
        _safe_segment(namespace),
        "peers",
        root_peer,
        "memories",
        _safe_segment(kind, "profiles"),
        "accounts",
        _safe_segment(account_id),
        "groups",
        _safe_segment(group_id),
    ]
    if member_id:
        parts += ["members", _safe_segment(member_id)]
    if task_id:
        parts += ["tasks", _safe_segment(task_id)]
    return "/".join(parts)


class OpenVikingMemoryClient:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.base_url = (settings.openviking_url or "").strip().rstrip("/")
        self.api_key = (settings.openviking_api_key or "").strip()
        self.namespace = (settings.openviking_namespace or "ds").strip() or "ds"
        self._disabled_until = 0.0
        self._last_failure_log = 0.0

    @property
    def configured(self) -> bool:
        # OpenViking deployments used by this project authenticate with an
        # API key.  Requiring both values keeps an accidentally partial .env
        # from producing a request on every incoming message.
        return bool(self.settings.openviking_enabled and self.base_url and self.api_key)

    def _headers(self) -> dict[str, str]:
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        if self.api_key:
            headers["Authorization"] = "Bearer " + self.api_key
        return headers

    def _pause_after_failure(self, path: str, error: str) -> None:
        """Trip the remote-memory cooldown without logging secrets or bodies."""

        cooldown = max(0.0, float(self.settings.openviking_failure_cooldown_seconds))
        self._disabled_until = time.monotonic() + cooldown
        # A bad token or unavailable host used to produce one warning and one
        # network wait for every incoming message. Log the first failure and
        # then keep the local SQL/vector fallback quiet until the cooldown.
        if time.monotonic() - self._last_failure_log >= 5.0:
            log.warning(
                "OpenViking request failed path=%s error=%s; remote memory paused for %.0fs",
                path,
                error,
                cooldown,
            )
            self._last_failure_log = time.monotonic()

    def _request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any] | None:
        if not self.configured:
            return None
        now = time.monotonic()
        if now < self._disabled_until:
            return None
        headers = dict(self._headers())
        headers.update(kwargs.pop("headers", {}) or {})
        try:
            total_timeout = max(1.0, float(self.settings.openviking_timeout_seconds))
            timeout = httpx.Timeout(
                timeout=total_timeout,
                connect=min(
                    total_timeout,
                    max(1.0, float(self.settings.openviking_connect_timeout_seconds)),
                ),
            )
            with httpx.Client(
                base_url=self.base_url,
                timeout=timeout,
                trust_env=False,
            ) as client:
                response = client.request(method, path, headers=headers, **kwargs)
                response.raise_for_status()
                if response.status_code == 204 or not response.content:
                    return {}
                body = response.json()
        except (httpx.HTTPError, ValueError, TypeError) as exc:
            # Remote semantic memory is deliberately best-effort.  The exact
            # SQL ledger remains available and no secret/response body is
            # logged.
            self._pause_after_failure(path, type(exc).__name__)
            return None
        if not isinstance(body, dict):
            return None
        if body.get("status") == "error":
            error = body.get("error")
            code = error.get("code") if isinstance(error, dict) else "remote_error"
            self._pause_after_failure(path, str(code))
            return None
        result = body.get("result", body)
        return result if isinstance(result, dict) else {"value": result}

    def _write_content(self, uri: str, content: str, *, wait: bool = False) -> bool:
        """Write one deterministic file through OpenViking's filesystem API."""

        result = self._request(
            "POST",
            "/api/v1/content/write",
            json={
                "uri": uri,
                "content": content,
                "mode": "replace",
                "wait": wait,
                "telemetry": False,
            },
        )
        return result is not None

    def _peer_uri(self, peer_id: str, *parts: str) -> str:
        segments = [
            "viking://user",
            _safe_segment(self.namespace),
            "peers",
            peer_id,
            *(_safe_segment(part, "item") for part in parts if str(part or "").strip()),
        ]
        return "/".join(segments)

    def _root_peer_id(self) -> str:
        """Return the single application peer that owns the scope tree."""

        # Peer IDs are one safe path segment.  Account/group/member levels
        # are represented by directories below this peer so operators can
        # inspect a real hierarchy without creating a flat peer per member.
        return _safe_segment(f"user-{self.namespace}-peers-wechatagent", "wechatagent")

    def _scope_uri(
        self,
        *,
        account_id: str,
        group_id: str,
        member_id: str = "",
        task_id: str = "",
        kind: str = "profiles",
        parts: tuple[str, ...] = (),
    ) -> str:
        # OpenViking's semantic search accepts only a peer's ``memories``
        # root.  Keep the account/group/member hierarchy below that root so
        # every file remains searchable and auditable.
        path_parts = ["memories", _safe_segment(kind, "profiles"), "accounts", account_id, "groups", group_id]
        if member_id:
            path_parts += ["members", member_id]
        if task_id:
            path_parts += ["tasks", task_id]
        path_parts.extend(parts)
        return self._peer_uri(self._root_peer_id(), *path_parts)

    @staticmethod
    def _stable_file_id(*parts: str) -> str:
        seed = "\x1f".join(str(part or "") for part in parts)
        return hashlib.sha256(seed.encode("utf-8")).hexdigest()[:32]

    def put_profile(
        self,
        *,
        profile_kind: str,
        scope: dict[str, Any],
        profile: dict[str, Any],
        source_message_ids: list[str] | tuple[str, ...] = (),
    ) -> bool:
        """Persist the current structured profile at its exact scope.

        Profiles are written as deterministic JSON files rather than one
        session per update.  This keeps the visible tree stable while the
        original messages remain available below ``memories/messages/``.
        """

        if not self.configured:
            return False
        account_id = str(scope.get("account_id") or "")
        group_id = str(scope.get("group_id") or "")
        member_id = str(scope.get("member_id") or "")
        task_id = str(scope.get("task_id") or "")
        if not account_id or not group_id or not profile_kind:
            return False
        memory_id = "profile-" + self._stable_file_id(
            profile_kind,
            account_id,
            group_id,
            member_id,
            task_id,
        )
        payload = {
            "memory_id": memory_id,
            "memory_type": f"{profile_kind}_profile",
            "scope": {
                key: str(value)
                for key, value in {
                    "account_id": account_id,
                    "group_id": group_id,
                    "member_id": member_id,
                    "task_id": task_id,
                }.items()
                if value
            },
            "value": profile,
            "source_message_ids": [str(value) for value in source_message_ids if str(value)],
            "status": "active",
            "migration_source": "wechat-agent-runtime",
        }
        return self._write_content(
            self._scope_uri(
                account_id=account_id,
                group_id=group_id,
                member_id=member_id,
                task_id=task_id,
                kind="profiles",
                parts=(f"{_safe_segment(profile_kind)}_profile.md",),
            ),
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
        )

    def put_raw_message(
        self,
        *,
        account_id: str,
        group_id: str,
        member_id: str,
        member_name: str,
        content: str,
        message_id: str,
        observed_at: str = "",
        source: str = "",
        thread_id: str = "",
        task_id: str = "",
        is_self: bool = False,
        image_count: int = 0,
    ) -> bool:
        """Store one original message in group and known-member scopes.

        Raw messages are kept under ``memories/messages/`` so they are available for
        future profile refreshes without being confused with curated facts.
        Unknown senders still enter the group corpus, but never create a
        member peer.
        """

        if not self.configured or not account_id or not group_id or not message_id:
            return False
        scope = {
            "account_id": account_id,
            "group_id": group_id,
            "member_id": member_id,
            "task_id": task_id,
        }
        payload = {
            "memory_id": "raw-" + self._stable_file_id(account_id, group_id, message_id),
            "memory_type": "raw_message",
            "scope": {key: value for key, value in scope.items() if value},
            "member_name": member_name,
            "content": str(content or "")[:12_000],
            "message_id": str(message_id),
            "source_message_ids": [str(message_id)],
            "observed_at": observed_at,
            "source": source,
            "thread_id": thread_id,
            "task_id": task_id,
            "is_self": bool(is_self),
            "image_count": max(0, int(image_count or 0)),
        }
        raw_name = f"{self._stable_file_id(account_id, group_id, message_id)}.json"
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        ok = self._write_content(
            self._scope_uri(
                account_id=account_id,
                group_id=group_id,
                kind="messages",
                parts=(raw_name,),
            ),
            encoded,
        )
        if not _is_unknown_member(member_id) and not is_self:
            ok = self._write_content(
                self._scope_uri(
                    account_id=account_id,
                    group_id=group_id,
                    member_id=member_id,
                    kind="messages",
                    parts=(raw_name,),
                ),
                encoded,
            ) and ok
        return ok

    def health(self) -> bool:
        return self._request("GET", "/health") is not None

    def put_memory(self, payload: dict[str, Any]) -> bool:
        """Append one extracted memory to a deterministic OV session.

        The payload contains a stable ``memory_id`` and scope marker.  The
        local ledger only calls this for a newly inserted memory, making a
        repeated message idempotent on the application side.  A commit is
        optional because a deployment may prefer OpenViking's threshold
        extractor; enabling it gives smoke tests immediate durable indexing.
        """

        if not self.configured:
            return False
        memory_id = str(payload.get("memory_id") or "").strip()
        if not memory_id:
            return False
        scope = payload.get("scope") if isinstance(payload.get("scope"), dict) else {}
        account_id = str(scope.get("account_id") or "")
        group_id = str(scope.get("group_id") or "")
        member_id = str(scope.get("member_id") or "")
        task_id = str(scope.get("task_id") or "")
        uri = scope_uri(
            namespace=self.namespace,
            account_id=account_id,
            group_id=group_id,
            member_id=member_id,
            task_id=task_id,
            kind="facts",
        )
        # JSON is useful to the extractor and makes scope auditing possible on
        # retrieval, while the value is already an extracted candidate rather
        # than the original group message.
        content = json.dumps(
            {
                "memory_id": memory_id,
                "memory_type": payload.get("memory_type", "fact"),
                "scope": scope,
                "value": str(payload.get("value") or "")[:6000],
                "source_message_ids": list(payload.get("source_message_ids") or [])[:20],
                "confidence": payload.get("confidence", 0.5),
                "importance": payload.get("importance", 0.5),
                "status": payload.get("status", "active"),
                "embedding_model": payload.get("embedding_model", ""),
                "scope_uri": uri,
                "peer_id": self._root_peer_id(),
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
        memory_type = _safe_segment(str(payload.get("memory_type") or "fact"), "fact")
        return self._write_content(
            self._scope_uri(
                account_id=account_id,
                group_id=group_id,
                member_id=member_id,
                task_id=task_id,
                kind="facts",
                parts=(memory_type, f"{memory_id}.json"),
            ),
            content,
        )

    def search(
        self,
        query: str,
        *,
        account_id: str,
        group_id: str,
        member_id: str = "",
        task_id: str = "",
        limit: int = 8,
    ) -> list[OpenVikingHit]:
        if not self.configured or not str(query or "").strip():
            return []
        # Search the exact hierarchical directory so profiles and original
        # messages remain scoped to the current account/group/member.
        targets = [self._peer_uri(self._root_peer_id(), "memories")]
        expected = {
            "account_id": account_id,
            "group_id": group_id,
            "member_id": member_id,
            "task_id": task_id,
        }

        def rows_for_target(target_uri: str) -> list[dict[str, Any]]:
            body = {
                "query": str(query).strip(),
                "target_uri": target_uri,
                "limit": max(1, min(int(limit) * 3, 50)),
                "read_content": True,
            }
            result = self._request("POST", "/api/v1/search/find", json=body)
            if result is None:
                return []
            rows: Any = result if isinstance(result, list) else None
            if rows is None and isinstance(result, dict):
                rows = result.get("memories") or result.get("items") or result.get("results")
                if rows is None and isinstance(result.get("value"), list):
                    rows = result["value"]
            return [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []

        def hits_for_rows(rows: list[dict[str, Any]]) -> list[OpenVikingHit]:
            hits: list[OpenVikingHit] = []
            seen_memory_ids: set[str] = set()
            for row in rows:
                uri = str(row.get("uri") or row.get("path") or "")
                text = str(
                    row.get("content")
                    or row.get("text")
                    or row.get("abstract")
                    or row.get("overview")
                    or ""
                )
                parsed = self._parse_payload(text)
                if not parsed:
                    continue
                scope = parsed.get("scope")
                if not isinstance(scope, dict):
                    continue
                scope_task = str(scope.get("task_id") or "")
                mismatched = False
                for key, value in expected.items():
                    if not value:
                        continue
                    actual = str(scope.get(key) or "")
                    if key == "member_id" and task_id and not actual and scope_task == task_id:
                        # Group-level task memories may be used by a member who
                        # asks about that task, matching the local SQL fallback.
                        continue
                    if actual != value:
                        mismatched = True
                        break
                if mismatched:
                    continue
                memory_id = str(parsed.get("memory_id") or "")
                if memory_id and memory_id in seen_memory_ids:
                    continue
                try:
                    score = float(row.get("score") or row.get("similarity") or 0.0)
                except (TypeError, ValueError):
                    score = 0.0
                raw_value = parsed.get("value")
                if isinstance(raw_value, (dict, list)):
                    raw_value = json.dumps(raw_value, ensure_ascii=False, separators=(",", ":"))
                if not str(raw_value or "").strip() and parsed.get("memory_type") == "raw_message":
                    raw_value = parsed.get("content") or ""
                hits.append(
                    OpenVikingHit(
                        memory_id=memory_id,
                        value=str(raw_value or ""),
                        scope={key: str(value) for key, value in scope.items() if value},
                        score=score,
                        uri=uri,
                        source_message_ids=tuple(
                            str(item) for item in parsed.get("source_message_ids", []) if item
                        ),
                        memory_type=str(parsed.get("memory_type") or ""),
                    )
                )
                if memory_id:
                    seen_memory_ids.add(memory_id)
                if len(hits) >= max(1, int(limit)):
                    break
            return hits

        # The new hierarchical paths are authoritative.  Old flat peers are
        # intentionally not consulted after migration.
        rows: list[dict[str, Any]] = []
        for target in targets:
            rows.extend(rows_for_target(target))
        return hits_for_rows(rows)

    @staticmethod
    def _parse_payload(text: Any) -> dict[str, Any] | None:
        if isinstance(text, dict):
            return text if text.get("memory_id") else None
        value = str(text or "").strip()
        if not value:
            return None
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError):
            return None
        return parsed if isinstance(parsed, dict) and parsed.get("memory_id") else None

