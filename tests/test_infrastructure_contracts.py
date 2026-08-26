"""Static regression contracts for Slice 11B Azure deployment readiness."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def read(relative_path: str) -> str:
    """Read one repository file as normalized UTF-8 text."""
    return (ROOT / relative_path).read_text(encoding="utf-8")


def test_application_parameter_files_target_eastus2_with_deployment_disabled() -> None:
    """Keep application resources in East US 2 without enabling deployment."""
    for relative_path in (
        "infra/environments/hackathon.bicepparam",
        "infra/environments/hackathon.runtime.bicepparam",
    ):
        content = read(relative_path)
        assert "param location = 'eastus2'" in content
        assert "param location = 'eastus'" not in content
        assert "param deployContainerApps = false" in content
        assert "param deployRuntimeAccess = false" in content


def test_container_apps_require_separate_immutable_image_digests() -> None:
    """Reference each runtime image by manifest digest rather than a mutable tag."""
    main = read("infra/main.bicep")
    resources = read("infra/resource-group.bicep")

    assert "param apiImageDigest string" in main
    assert "param uiImageDigest string" in main
    assert "imageTag" not in main
    assert "optima-api@${validatedApiImageDigest}" in resources
    assert "optima-ui@${validatedUiImageDigest}" in resources
    assert "optima-api:${" not in resources
    assert "optima-ui:${" not in resources
    assert "placeholderImageDigest" in main
    assert "validatedApiImageDigest" in main
    assert "validatedUiImageDigest" in main
    assert "fail('Container Apps deployment requires" in main
    assert "apiImageDigest == toLower(apiImageDigest)" in main
    assert "uiImageDigest == toLower(uiImageDigest)" in main
    for character in "0123456789abcdef":
        assert f"'{character}', '')" in main
    assert "placeholderImageDigest" in resources
    assert "fail('Container Apps deployment requires" in resources
    assert "apiImageDigest == toLower(apiImageDigest)" in resources
    assert "uiImageDigest == toLower(uiImageDigest)" in resources


def test_bicep_entry_points_restrict_application_location_to_eastus2() -> None:
    """Reject an unreviewed application region instead of falling back silently."""
    for relative_path in ("infra/main.bicep", "infra/resource-group.bicep"):
        content = read(relative_path)
        assert "@allowed([\n  'eastus2'\n])" in content
        assert "param location string = 'eastus2'" in content
        assert "resourceGroup().location" not in content


def test_container_apps_map_production_runtime_environment_contract() -> None:
    """Supply the API and UI deployment environment with existing Azure settings."""
    module = read("infra/modules/container-apps.bicep")

    assert module.count("name: 'OPTIMA_DEPLOYMENT_ENVIRONMENT'") == 2
    assert "name: 'OPTIMA_PRODUCTION_EVALUATOR_MODE'" in module
    assert "name: 'OPTIMA_PRODUCTION_REQUIRE_REFERENCE_OUTPUT'" in module
    assert "name: 'OPTIMA_REQUIRE_REFERENCE_OUTPUT'" in module
    assert "name: 'OPTIMA_FOUNDRY_MANAGED_IDENTITY_CLIENT_ID'" in module
    assert "name: 'OPTIMA_COSMOS_MANAGED_IDENTITY_CLIENT_ID'" in module
    assert "name: 'OPTIMA_REDIS_MANAGED_IDENTITY_CLIENT_ID'" in module
    assert "name: 'OPTIMA_API_BASE_URL'" in module
