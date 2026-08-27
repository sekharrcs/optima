"""Generate a deterministic CycloneDX inventory from the active environment."""

from __future__ import annotations

import argparse
import json
import sys
from importlib.metadata import distributions
from pathlib import Path
from typing import Any
from urllib.parse import quote
from uuid import NAMESPACE_URL, uuid5

PROJECT_VERSION = "0.1.0"


def _normalized_name(name: str) -> str:
    """Return one PEP 503-compatible package name."""
    return "-".join(filter(None, name.lower().replace("_", "-").split("-")))


def installed_components() -> list[dict[str, Any]]:
    """Inventory each installed Python distribution exactly once."""
    packages: dict[str, str] = {}
    for distribution in distributions():
        name = distribution.metadata["Name"]
        if not name:
            raise ValueError("installed distribution is missing its package name")
        normalized_name = _normalized_name(name)
        if normalized_name == "optima":
            continue
        existing = packages.get(normalized_name)
        if existing is not None and existing != distribution.version:
            raise ValueError(
                f"installed package has conflicting versions: {normalized_name}"
            )
        packages[normalized_name] = distribution.version

    return [
        {
            "type": "library",
            "bom-ref": f"pkg:pypi/{quote(name)}@{quote(version)}",
            "name": name,
            "version": version,
            "purl": f"pkg:pypi/{quote(name)}@{quote(version)}",
        }
        for name, version in sorted(packages.items())
    ]


def generate_sbom(component: str, output: Path) -> dict[str, Any]:
    """Write one reproducible CycloneDX document for an OPTIMA runtime."""
    if component not in {"api", "ui"}:
        raise ValueError("component must be api or ui")
    components = installed_components()
    identity = json.dumps(
        {"component": component, "components": components},
        sort_keys=True,
        separators=(",", ":"),
    )
    application_ref = f"pkg:generic/optima-{component}@{PROJECT_VERSION}"
    document: dict[str, Any] = {
        "bomFormat": "CycloneDX",
        "specVersion": "1.6",
        "serialNumber": f"urn:uuid:{uuid5(NAMESPACE_URL, identity)}",
        "version": 1,
        "metadata": {
            "component": {
                "type": "application",
                "bom-ref": application_ref,
                "name": f"optima-{component}",
                "version": PROJECT_VERSION,
                "purl": application_ref,
                "properties": [
                    {"name": "optima:runtime-image", "value": component},
                    {
                        "name": "optima:inventory-source",
                        "value": "active-python-environment",
                    },
                ],
            }
        },
        "components": components,
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(document, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return document


def create_parser() -> argparse.ArgumentParser:
    """Create the command-line parser."""
    parser = argparse.ArgumentParser(
        description="Generate an exact-environment CycloneDX SBOM",
    )
    parser.add_argument("--component", choices=("api", "ui"), required=True)
    parser.add_argument("--output", type=Path, required=True)
    return parser


def main() -> int:
    """Generate the requested SBOM and return a process exit code."""
    arguments = create_parser().parse_args()
    generate_sbom(arguments.component, arguments.output)
    return 0


if __name__ == "__main__":
    sys.exit(main())
