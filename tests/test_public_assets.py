"""Distribution contracts checked inside the standalone export as well."""

from pathlib import Path

import yaml


ROOT = Path(__file__).parents[1]
PLACEHOLDERS = {
    "HEMS_URL": "http://hems-controller.example.invalid",
    "HEMS_USER": "replace-with-hems-user",
    "HEMS_PASSWORD": "replace-with-hems-password",
    "HEMS_API_KEY_READ": "replace-with-random-read-key",
    "HEMS_API_KEY_CONTROL": "replace-with-random-control-key",
    "HEMS_DISABLE_LOCK": "false",
}


def test_env_example_contains_only_approved_placeholders():
    lines = (ROOT / ".env.example").read_text().splitlines()
    assignments = [line for line in lines if line and not line.startswith("#")]
    assert len(assignments) == len(PLACEHOLDERS)
    assert dict(line.split("=", 1) for line in assignments) == PLACEHOLDERS


def test_compose_builds_locally_and_keeps_browser_runtime_contract():
    compose = ROOT / "compose.yaml"
    if not compose.exists():
        compose = ROOT / "compose.public.yaml"  # preparatory monorepo filename
    config = yaml.safe_load(compose.read_text())
    service = config["services"]["hems-api"]
    assert service["build"] == "."
    assert "image" not in service
    assert service["env_file"] == [".env"]
    assert service["init"] is True
    assert service["shm_size"] == "256mb"
    assert service["security_opt"] == ["seccomp:unconfined"]
    assert service["stop_grace_period"] == "35s"


def test_ha_example_uses_secret_references_and_generic_host():
    text = (ROOT / "configuration_hems.yaml.example").read_text()
    key_lines = [line.strip() for line in text.splitlines() if "X-API-Key:" in line]
    assert key_lines.count("X-API-Key: !secret hems_api_key_read") == 3
    assert key_lines.count("X-API-Key: !secret hems_api_key_control") == 3
    assert text.count("http://hems-api.example.invalid:5000/") == 6


def test_release_pushes_one_build_only_after_smoke_and_identity_check():
    config = yaml.safe_load((ROOT / ".github/workflows/release.yml").read_text())
    assert config["permissions"] == {"contents": "read", "packages": "write"}
    steps = config["jobs"]["release"]["steps"]
    commands = "\n".join(step.get("run", "") for step in steps)
    assert commands.count("docker build --no-cache") == 1
    build = commands.index("docker build --no-cache")
    before_id = commands.index("built_id=")
    smoke = commands.index('scripts/smoke-image.sh "$image_ref"')
    after_id = commands.index("smoked_id=")
    compare = commands.index('[[ "$built_id" == "$smoked_id" ]]')
    push = commands.index('docker push "$image_ref"')
    digest = commands.index("RepoDigests")
    assert build < before_id < smoke < after_id < compare < push < digest
    assert "set -euo pipefail" in commands
    assert "docker login" in commands
