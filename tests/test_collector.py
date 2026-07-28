import json
import subprocess
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
COLLECTOR = REPO_ROOT / "collector.ps1"
POWERSHELL = "powershell.exe"
PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00" * 24
JPEG_BYTES = b"\xff\xd8\xff\xe0" + b"\x00" * 24


def _target(target_id: int, name: str = "原神") -> dict:
    return {
        "target_game_id": target_id,
        "name": name,
        "category_id": 9,
        "icon_local": False,
    }


def _write_manifest(
    path: Path, targets: list[dict], *, bundle_id: str = "fixture-bundle"
):
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "bundle_id": bundle_id,
                "max_screenshots": 1,
                "delay_seconds": 0,
                "targets": targets,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8-sig",
    )


def _write_success_fixture(root: Path, target_id: int, name: str = "原神"):
    target_root = root / str(target_id)
    target_root.mkdir(parents=True)
    (target_root / "responses.json").write_text(
        json.dumps(
            {
                "public_ip": "203.0.113.10",
                "bootstrap_xsrf": 200,
                "search_by_name": 200,
                "game_detail": 200,
                "download_icon": 200,
                "download_screenshot": 200,
            }
        ),
        encoding="utf-8",
    )
    (target_root / "search.json").write_text(
        json.dumps(
            {
                "data": {
                    "list": [
                        {
                            "list": [
                                {
                                    "type": "brand",
                                    "brand": {
                                        "app": {"id": 999, "title": f"{name}：云游戏"}
                                    },
                                },
                                {
                                    "type": "brand",
                                    "brand": {"app": {"id": 168332, "title": name}},
                                },
                            ]
                        }
                    ]
                }
            },
            ensure_ascii=False,
        ),
        encoding="utf-8-sig",
    )
    (target_root / "detail.json").write_text(
        json.dumps(
            {
                "data": {
                    "app": {
                        "id": 168332,
                        "title": name,
                        "identifier": "com.miHoYo.Yuanshen",
                        "stat": {"rating": {"score": 7.9}, "review_count": 100},
                        "tags": [{"value": "开放世界"}, {"value": "二次元"}],
                        "platform_info": {
                            "supported_platforms": [
                                {"key": "android"},
                                {"key": "ios"},
                                {"key": "pc"},
                            ]
                        },
                        "description": {"text": "fixture description"},
                        "icon": {"original_url": "https://img.example/icon.png"},
                        "screenshots": [
                            {"original_url": "https://img.example/shot-01.jpg"}
                        ],
                    }
                }
            },
            ensure_ascii=False,
        ),
        encoding="utf-8-sig",
    )
    (target_root / "icon.png").write_bytes(PNG_BYTES)
    (target_root / "screenshot-01.jpg").write_bytes(JPEG_BYTES)


def _run_collector(
    manifest: Path,
    output_root: Path,
    fixture_root: Path,
    *,
    check: bool = False,
) -> subprocess.CompletedProcess:
    try:
        result = subprocess.run(
            [
                POWERSHELL,
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(COLLECTOR),
                "-Manifest",
                str(manifest),
                "-OutputRoot",
                str(output_root),
                "-FixtureRoot",
                str(fixture_root),
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=30,
            check=False,
        )
    except FileNotFoundError:
        pytest.skip("Windows PowerShell is required for collector tests")
    if check and result.returncode != 0:
        raise AssertionError(
            f"collector failed:\nstdout={result.stdout}\nstderr={result.stderr}"
        )
    return result


def test_rejects_more_than_twenty_targets_before_fixture_access(tmp_path):
    manifest = tmp_path / "targets.json"
    _write_manifest(manifest, [_target(i, f"game-{i}") for i in range(1, 22)])

    result = _run_collector(manifest, tmp_path / "out", tmp_path / "missing")

    assert result.returncode != 0
    assert "between 1 and 20" in (result.stdout + result.stderr)
    assert not (tmp_path / "out" / "fixture-bundle").exists()


def test_uses_exact_brand_and_writes_integrity_bundle(tmp_path):
    manifest = tmp_path / "targets.json"
    fixtures = tmp_path / "fixtures"
    output = tmp_path / "out"
    _write_manifest(manifest, [_target(42)])
    _write_success_fixture(fixtures, 42)

    _run_collector(manifest, output, fixtures, check=True)

    bundle_root = output / "fixture-bundle"
    bundle_manifest = json.loads(
        (bundle_root / "manifest.json").read_text(encoding="utf-8-sig")
    )
    game_payload = json.loads(
        (bundle_root / "games" / "42" / "game.json").read_text(encoding="utf-8-sig")
    )
    assert game_payload["game"]["game_id"] == "168332"
    assert game_payload["game"]["name"] == "原神"
    assert bundle_manifest["targets"][0]["status"] == "success"
    assert {entry["role"] for entry in bundle_manifest["files"]} == {
        "game",
        "icon",
        "screenshot",
    }
    assert all(len(entry["sha256"]) == 64 for entry in bundle_manifest["files"])


def test_stops_after_block_response(tmp_path):
    manifest = tmp_path / "targets.json"
    fixtures = tmp_path / "fixtures"
    output = tmp_path / "out"
    _write_manifest(manifest, [_target(1, "blocked"), _target(2, "later")])
    blocked_root = fixtures / "1"
    blocked_root.mkdir(parents=True)
    (blocked_root / "responses.json").write_text(
        json.dumps({"public_ip": "203.0.113.10", "bootstrap_xsrf": 429}),
        encoding="utf-8",
    )
    _write_success_fixture(fixtures, 2, "later")

    result = _run_collector(manifest, output, fixtures)

    assert result.returncode == 3
    report = json.loads(
        (output / "fixture-bundle" / "report.json").read_text(encoding="utf-8-sig")
    )
    assert report["blocked"] is True
    assert report["targets"][0]["status"] == "blocked"
    assert all(item["target_game_id"] != 2 for item in report["targets"])


def test_resume_skips_success_and_outputs_no_session_secret(tmp_path):
    manifest = tmp_path / "targets.json"
    fixtures = tmp_path / "fixtures"
    output = tmp_path / "out"
    _write_manifest(manifest, [_target(42)])
    _write_success_fixture(fixtures, 42)
    (fixtures / "42" / "secret.txt").write_text(
        "XSRF-TOKEN=DO_NOT_PERSIST", encoding="utf-8"
    )

    _run_collector(manifest, output, fixtures, check=True)
    second = _run_collector(manifest, output, fixtures, check=True)

    report = json.loads(
        (output / "fixture-bundle" / "report.json").read_text(encoding="utf-8-sig")
    )
    all_text = "\n".join(
        path.read_text(encoding="utf-8-sig")
        for path in (output / "fixture-bundle").rglob("*.json")
    )
    assert report["skipped_success"] == 1
    assert "DO_NOT_PERSIST" not in all_text
    assert "skipped_success=1" in second.stdout
