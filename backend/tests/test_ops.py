"""Ops helpers: health, cluster probe, log tail, stub_status parsing."""

import json
import os

from app.ops import parse_stub_status, _tail_lines

from .conftest import auth

STUB_SAMPLE = """Active connections: 3
server accepts handled requests
 128 125 999
Reading: 0 Writing: 1 Waiting: 2
"""


def test_parse_stub_status():
    g = parse_stub_status(STUB_SAMPLE)
    assert g["active"] == 3
    assert g["accepts"] == 128
    assert g["handled"] == 125
    assert g["requests"] == 999
    assert g["waiting"] == 2
    # accepts - handled is the number of connections Nginx DROPPED, which is
    # the single most useful figure on that page.
    assert g["dropped"] == 3


def test_parse_stub_status_on_garbage_returns_empty():
    """The ops page must render even if stub_status is unreachable."""
    assert parse_stub_status("") == {}
    assert parse_stub_status("<html>404</html>") == {}


def test_tail_lines_reads_from_the_end(tmp_path):
    p = tmp_path / "access.json.log"
    p.write_text("".join(f'{{"n":{i}}}\n' for i in range(5000)), encoding="utf-8")
    lines = _tail_lines(str(p), 3)
    assert len(lines) == 3
    assert [json.loads(l)["n"] for l in lines] == [4997, 4998, 4999]


def test_tail_lines_handles_a_short_file(tmp_path):
    p = tmp_path / "short.log"
    p.write_text('{"n":1}\n', encoding="utf-8")
    assert len(_tail_lines(str(p), 50)) == 1


async def test_health_is_public_and_names_the_instance(client):
    r = await client.get("/api/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"
    assert r.json()["database"] == "up"
    assert r.json()["instance"]


async def test_health_response_carries_the_instance_header(client):
    """The middleware stamps every response; the ops page counts these headers
    to show Nginx's round robin."""
    r = await client.get("/api/health")
    assert r.headers["X-Instance"]


async def test_ops_logs_degrades_gracefully_without_nginx(client, admin_token):
    """Running the API without Nginx in front (exactly this test environment)
    must not 500 -- it reports available:false."""
    r = await client.get("/api/ops/logs", headers=auth(admin_token))
    assert r.status_code == 200
    body = r.json()
    if not os.path.exists(body["path"]):
        assert body["available"] is False
        assert body["entries"] == []


async def test_ops_logs_limit_is_bounded(client, admin_token):
    r = await client.get("/api/ops/logs?limit=9999", headers=auth(admin_token))
    assert r.status_code == 422


async def test_cluster_probe_reports_down_peers(client, admin_token, monkeypatch):
    """With no reachable peers the probe must still answer, with up:0 -- a
    dashboard that hangs on a dead node is worse than useless."""
    from app import config as cfg
    monkeypatch.setattr(cfg, "PEERS", ["http://127.0.0.1:9"])   # discard port
    r = await client.get("/api/ops/cluster", headers=auth(admin_token))
    assert r.status_code == 200
    assert r.json()["up"] == 0
    assert r.json()["total"] == 1
    assert r.json()["peers"][0]["up"] is False
