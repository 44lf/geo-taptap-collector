from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from urllib.error import HTTPError
from urllib.request import Request

import pytest

from geo_game_collector.transport import (
    GatewayHttpClient,
    _RejectGatewayRedirects,
)

NOW = datetime(2026, 7, 29, 8, 0, tzinfo=UTC)


class _Response:
    status = 200

    def __init__(self, payload: dict):
        self._body = json.dumps(payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return self._body


def test_gateway_client_registers_completed_run_with_scoped_bearer():
    requests = []

    def opener(request, *, timeout):
        requests.append((request, timeout))
        return _Response(
            {
                "created": True,
                "collector_id": "collector-1",
                "run_id": "run-1",
                "job_id": "job-1",
                "status": "partial",
            }
        )

    client = GatewayHttpClient(
        base_url="https://gateway.test/",
        bearer_credential="credential-1:secret",
        opener=opener,
    )

    result = client.complete_run(
        run_id="run-1",
        job_id="job-1",
        status="partial",
        summary={"success": 3, "blocked": 1},
        started_at=NOW,
        finished_at=NOW + timedelta(minutes=1),
    )

    request, timeout = requests[0]
    assert request.full_url == "https://gateway.test/api/collector/runs/complete"
    assert request.method == "POST"
    assert request.headers["Authorization"] == "Bearer credential-1:secret"
    payload = json.loads(request.data)
    assert payload["run_id"] == "run-1"
    assert payload["started_at"].endswith("Z")
    assert result.collector_id == "collector-1"
    assert result.status == "partial"
    assert timeout == 15


def test_gateway_client_rejects_naive_run_timestamp_before_network():
    client = GatewayHttpClient(
        base_url="https://gateway.test/",
        bearer_credential="credential-1:secret",
        opener=lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("network must not run")
        ),
    )

    with pytest.raises(ValueError, match="UTC offset"):
        client.complete_run(
            run_id="run-1",
            job_id="job-1",
            status="succeeded",
            summary={},
            started_at=datetime(2026, 7, 29, 8, 0),
            finished_at=NOW,
        )


def test_gateway_client_sends_heartbeat_and_idempotent_events():
    requests = []
    responses = iter(
        (
            {
                "collector_id": "collector-1",
                "received_at": "2026-07-29T08:00:01Z",
            },
            {
                "accepted_event_ids": ["event-1"],
                "duplicate_event_ids": [],
            },
        )
    )

    def opener(request, *, timeout):
        requests.append((request, timeout))
        return _Response(next(responses))

    client = GatewayHttpClient(
        base_url="https://gateway.test/",
        bearer_credential="credential-1:secret",
        opener=opener,
    )

    received_at = client.send_heartbeat(
        platform="Windows",
        agent_version="0.1.0",
        enabled_sources=("baidu", "taptap"),
        current_run_id="run-1",
        current_stage="collect",
        spool_pending_count=2,
    )
    outcome = client.send_events(
        (
            {
                "event_id": "event-1",
                "run_id": "run-1",
                "job_id": "job-1",
                "transport_id": None,
                "bundle_id": None,
                "source": "baidu",
                "component": "collector-agent",
                "component_version": "0.1.0",
                "stage": "collect",
                "event_type": "item_completed",
                "level": "info",
                "payload": {"status": "success"},
                "occurred_at": "2026-07-29T08:00:00Z",
            },
        )
    )

    assert received_at == datetime(2026, 7, 29, 8, 0, 1, tzinfo=UTC)
    assert outcome.accepted_event_ids == ("event-1",)
    assert requests[0][0].full_url.endswith("/api/collector/heartbeat")
    assert requests[1][0].full_url.endswith("/api/collector/events")
    assert json.loads(requests[1][0].data)["events"][0]["event_id"] == "event-1"


def test_gateway_client_accepts_bounded_unknown_receipt_classification():
    client = GatewayHttpClient(
        base_url="https://gateway.test/",
        bearer_credential="credential-1:secret",
        opener=lambda *_args, **_kwargs: _Response(
            {
                "transport_id": "transport-1",
                "state": "dead_letter",
                "object_key": "incoming/transport-1.tar.zst",
                "archive_sha256": "a" * 64,
                "receipt_status": "dead_letter",
                "receipt_completed_at": "2026-07-29T08:00:00Z",
                "error_classification": "identity",
                "error_summary": "remote identity conflict",
            }
        ),
    )

    remote = client.get_transfer_status("transport-1")

    assert remote.receipt is not None
    assert remote.receipt.error_classification == "identity"


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
def test_gateway_redirects_are_rejected_before_bearer_can_cross_origin(
    status: int,
) -> None:
    request = Request(
        "https://gateway.test/api/collector/jobs/claim",
        headers={"Authorization": "Bearer credential-1:secret"},
        method="POST",
    )

    with pytest.raises(HTTPError) as caught:
        _RejectGatewayRedirects().redirect_request(
            request,
            None,
            status,
            "redirect",
            {"Location": "https://attacker.invalid/capture"},
            "https://attacker.invalid/capture",
        )

    assert caught.value.code == status
    assert request.full_url == "https://gateway.test/api/collector/jobs/claim"
