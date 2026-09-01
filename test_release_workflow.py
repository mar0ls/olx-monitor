"""Testy regresyjne konfiguracji procesu release."""

from pathlib import Path

ROOT = Path(__file__).resolve().parent


def _read(relative_path: str) -> str:
    return (ROOT / relative_path).read_text(encoding="utf-8")


def test_tag_push_uses_pat_persisted_by_checkout():
    workflow = _read(".github/workflows/check-districts.yml")

    assert "token: ${{ secrets.PAT_TOKEN }}" in workflow
    assert "git remote set-url origin https://x-access-token:" not in workflow
    assert "gh release delete" not in workflow


def test_dev_sync_uses_a_separate_job():
    workflow = _read(".github/workflows/check-districts.yml")

    assert "sync-dev:" in workflow
    assert "needs.check.outputs.changed == 'true'" in workflow
    assert workflow.count("uses: actions/checkout@v4") == 2


def test_manual_release_checks_out_requested_tag():
    workflow = _read(".github/workflows/release.yml")
    requested_ref = "ref: ${{ github.event_name == 'workflow_dispatch' && inputs.tag || github.ref }}"

    assert workflow.count(requested_ref) == 2
    assert "ref: ${{ github.workflow_sha }}" in workflow
    assert "SOURCE_ROOT=\"$GITHUB_WORKSPACE\" bash .release-tooling/scripts/build_release.sh" in workflow


def test_publish_downloads_only_release_artifacts():
    workflow = _read(".github/workflows/release.yml")

    assert "uses: actions/download-artifact@v8" in workflow
    assert "pattern: olx-monitor-*" in workflow
    assert "merge-multiple: true" in workflow


def test_macos_build_does_not_use_iconutil():
    build_script = _read("scripts/build_release.sh")

    assert "\n  iconutil " not in build_script
    assert "ensure_icns" not in build_script
    assert '--icon "$ROOT_DIR/assets/icon.png"' in build_script
    assert 'PYINSTALLER_CONFIG_DIR="${PYINSTALLER_CONFIG_DIR:-$WORK_ROOT/pyinstaller-cache}"' in build_script
