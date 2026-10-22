"""Trusted GitHub verifier for the R1 threshold decision record.

The verifier is the only network boundary for threshold approval. It reads the
Product Owner authority issue and the current decision issue through the
authenticated GitHub API; the pure threshold gate receives only typed evidence.
"""

from __future__ import annotations

from datetime import datetime
import hashlib
import json
import re
from typing import Protocol
from urllib.parse import urlsplit

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from spine.evaluations.release_gates.thresholds import (
    MandatorySafetyResult,
    ThresholdGateResult,
    ThresholdEvidenceV2,
    ThresholdPolicyV2,
    VerifiedThresholdApproval,
    RuntimeIdentityBinding,
    _issue_verified_threshold_approval,
    R1_PRODUCT_OWNER_GITHUB_ACTOR_ID,
    evaluate_threshold_policy,
    policy_digest,
)


REPOSITORY = "alkselsv/spine"
AUTHORITY_ISSUE = 17
DECISION_ISSUE = 557
_RECORD_PATTERN = re.compile(
    r"```spine-r1-threshold-decision:v1\s*\n(.*?)\n```", re.DOTALL
)


class _DecisionRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: str = Field(pattern=r"^spine-r1-threshold-decision:v1$")
    decision_id: str = Field(min_length=1, max_length=128)
    status: str = Field(pattern=r"^(approved|rejected|revoked|superseded)$")
    policy_id: str = Field(min_length=1, max_length=128)
    policy_version: str = Field(min_length=1, max_length=128)
    policy_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    dataset_id: str = Field(min_length=1, max_length=128)
    dataset_version: str = Field(min_length=1, max_length=128)
    dataset_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    runtime_binding: RuntimeIdentityBinding
    release_scope: str = Field(min_length=1, max_length=128)
    decided_at: datetime
    supersedes_decision_ids: tuple[str, ...] = ()
    revokes_decision_ids: tuple[str, ...] = ()


class GitHubDecisionComment(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    comment_id: int = Field(gt=0)
    actor_id: int = Field(gt=0)
    body: str
    created_at: datetime
    updated_at: datetime | None
    html_url: str


class GitHubTimelineEvent(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    event: str
    comment_id: int | None = None


class ThresholdDecisionSource(Protocol):
    async def issue_author_id(self, issue_number: int) -> int: ...

    async def comments(self, issue_number: int) -> tuple[GitHubDecisionComment, ...]: ...

    async def timeline(self, issue_number: int) -> tuple[GitHubTimelineEvent, ...]: ...


class GitHubRestThresholdDecisionSource:
    """Authenticated, paginated GitHub REST source for issues and decision records."""

    def __init__(self, *, token: str, client: httpx.AsyncClient | None = None) -> None:
        if not token.strip():
            raise ValueError("GitHub token is required for trusted approval verification")
        self._headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        self._client = client or httpx.AsyncClient(
            base_url="https://api.github.com",
            timeout=httpx.Timeout(10.0, connect=3.0),
            headers=self._headers,
        )
        self._owns_client = client is None

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def _get(self, path: str) -> httpx.Response:
        parsed = urlsplit(path)
        if parsed.scheme and (
            parsed.scheme != "https" or parsed.netloc != "api.github.com"
        ):
            raise ValueError("GitHub pagination must remain on the authenticated API host")
        response = await self._client.get(path, headers=self._headers)
        response.raise_for_status()
        return response

    async def issue_author_id(self, issue_number: int) -> int:
        response = await self._get(f"/repos/{REPOSITORY}/issues/{issue_number}")
        payload = response.json()
        actor_id = payload.get("user", {}).get("id")
        if not isinstance(actor_id, int) or actor_id <= 0:
            raise ValueError("GitHub issue author identity is unavailable")
        return actor_id

    async def _pages(self, path: str) -> tuple[dict[str, object], ...]:
        items: list[dict[str, object]] = []
        next_url: str | None = path
        while next_url is not None:
            response = await self._get(next_url)
            payload = response.json()
            if not isinstance(payload, list) or any(not isinstance(item, dict) for item in payload):
                raise ValueError("GitHub decision source returned an invalid collection")
            items.extend(payload)
            next_url = response.links.get("next", {}).get("url")
        return tuple(items)

    async def comments(self, issue_number: int) -> tuple[GitHubDecisionComment, ...]:
        values = await self._pages(
            f"/repos/{REPOSITORY}/issues/{issue_number}/comments?per_page=100"
        )
        result = []
        for item in values:
            user = item.get("user")
            result.append(
                GitHubDecisionComment(
                    comment_id=item.get("id"),
                    actor_id=user.get("id") if isinstance(user, dict) else None,
                    body=item.get("body"),
                    created_at=item.get("created_at"),
                    updated_at=item.get("updated_at"),
                    html_url=item.get("html_url"),
                )
            )
        return tuple(result)

    async def timeline(self, issue_number: int) -> tuple[GitHubTimelineEvent, ...]:
        values = await self._pages(
            f"/repos/{REPOSITORY}/issues/{issue_number}/timeline?per_page=100"
        )
        return tuple(
            GitHubTimelineEvent(
                event=str(item.get("event", "")),
                comment_id=item.get("issue_comment_id")
                if isinstance(item.get("issue_comment_id"), int)
                else None,
            )
            for item in values
        )


def _record(comment: GitHubDecisionComment) -> _DecisionRecord | None:
    match = _RECORD_PATTERN.search(comment.body)
    if match is None:
        return None
    try:
        return _DecisionRecord.model_validate_json(match.group(1))
    except ValidationError:
        return None


class GitHubThresholdApprovalVerifier:
    """Fail-closed verifier for one exact R1 threshold policy and runtime."""

    def __init__(self, source: ThresholdDecisionSource) -> None:
        self._source = source

    async def verify(
        self, *, policy: ThresholdPolicyV2, evidence: ThresholdEvidenceV2
    ) -> VerifiedThresholdApproval | None:
        try:
            authority_actor_id = await self._source.issue_author_id(AUTHORITY_ISSUE)
            comments = await self._source.comments(DECISION_ISSUE)
            events = await self._source.timeline(DECISION_ISSUE)
            if authority_actor_id != R1_PRODUCT_OWNER_GITHUB_ACTOR_ID:
                return None

            all_records = tuple(
                (comment, record)
                for comment in comments
                if (record := _record(comment)) is not None
            )
            records = tuple(
                pair for pair in all_records
                if pair[1].policy_id == policy.policy_id
                and pair[1].policy_version == policy.version
            )
            exact_scope_records = tuple(
                (comment, record)
                for comment, record in records
                if record.release_scope == policy.runtime_scope
            )
            if len(exact_scope_records) != 1:
                return None
            comment, record = exact_scope_records[0]
            if (
                record.status != "approved"
                or comment.actor_id != authority_actor_id
                or comment.updated_at != comment.created_at
                or record.decided_at.tzinfo is None
                or comment.created_at != record.decided_at
                or comment.html_url
                != f"https://github.com/{REPOSITORY}/issues/{DECISION_ISSUE}#issuecomment-{comment.comment_id}"
                or any(
                    event.event == "comment_deleted" and event.comment_id == comment.comment_id
                    for event in events
                )
                or any(
                    record.decision_id in other.supersedes_decision_ids
                    or record.decision_id in other.revokes_decision_ids
                    for _, other in all_records
                    if other.decision_id != record.decision_id
                )
            ):
                return None

            expected_binding = policy.runtime_binding
            if (
                record.policy_digest != policy_digest(policy)
                or (record.dataset_id, record.dataset_version, record.dataset_digest)
                != (policy.dataset_id, policy.dataset_version, policy.dataset_digest)
                or record.runtime_binding != expected_binding
                or record.release_scope != policy.runtime_scope
                or (evidence.dataset_id, evidence.dataset_version, evidence.dataset_digest)
                != (policy.dataset_id, policy.dataset_version, policy.dataset_digest)
                or evidence.runtime_binding != expected_binding
            ):
                return None

            snapshot = json.dumps(
                {
                    "authority_actor_id": authority_actor_id,
                    "comments": [
                        {
                            "id": item.comment_id,
                            "actor_id": item.actor_id,
                            "created_at": item.created_at.isoformat(),
                            "updated_at": item.updated_at.isoformat() if item.updated_at else None,
                            "body_sha256": hashlib.sha256(item.body.encode("utf-8")).hexdigest(),
                        }
                        for item in comments
                    ],
                    "events": [event.model_dump(mode="json") for event in events],
                },
                sort_keys=True,
                separators=(",", ":"),
            )
            return _issue_verified_threshold_approval(
                decision_id=record.decision_id,
                record_reference=comment.html_url,
                actor_id=comment.actor_id,
                authority_reference=f"https://github.com/{REPOSITORY}/issues/{AUTHORITY_ISSUE}",
                policy_id=record.policy_id,
                policy_version=record.policy_version,
                policy_digest=record.policy_digest,
                dataset_id=record.dataset_id,
                dataset_version=record.dataset_version,
                dataset_digest=record.dataset_digest,
                runtime_binding=expected_binding,
                release_scope=record.release_scope,
                decision_status="approved",
                decided_at=record.decided_at,
                source_snapshot_digest=hashlib.sha256(snapshot.encode("utf-8")).hexdigest(),
            )
        except Exception:
            # Network, API, parse and currentness uncertainty all fail closed.
            return None


async def evaluate_threshold_policy_with_github_approval(
    *,
    verifier: GitHubThresholdApprovalVerifier,
    policy: ThresholdPolicyV2,
    evidence: ThresholdEvidenceV2,
    safety_results: tuple[MandatorySafetyResult, ...],
    required_safety_gates: tuple[str, ...],
) -> ThresholdGateResult:
    """Trusted composition: verify GitHub evidence, then call the pure gate."""
    approval = await verifier.verify(policy=policy, evidence=evidence)
    return evaluate_threshold_policy(
        policy=policy,
        evidence=evidence,
        safety_results=safety_results,
        required_safety_gates=required_safety_gates,
        verified_approval=approval,
    )


__all__ = [
    "GitHubDecisionComment",
    "GitHubRestThresholdDecisionSource",
    "GitHubThresholdApprovalVerifier",
    "GitHubTimelineEvent",
    "ThresholdDecisionSource",
    "evaluate_threshold_policy_with_github_approval",
]
