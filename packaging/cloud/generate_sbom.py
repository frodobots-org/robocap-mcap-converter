from __future__ import annotations

import argparse
import hashlib
import json
import re
import tomllib
from datetime import datetime, timezone
from pathlib import Path


def _spdx_id(kind: str, name: str, version: str) -> str:
    token = re.sub(r"[^A-Za-z0-9.-]", "-", f"{name}-{version}")
    return f"SPDXRef-{kind}-{token}"


def _package(name: str, version: str, supplier: str, kind: str, comment: str = "") -> dict:
    package = {
        "name": name,
        "SPDXID": _spdx_id(kind, name, version),
        "versionInfo": version,
        "downloadLocation": "NOASSERTION",
        "filesAnalyzed": False,
        "licenseConcluded": "NOASSERTION",
        "licenseDeclared": "NOASSERTION",
        "copyrightText": "NOASSERTION",
        "supplier": supplier,
    }
    if comment:
        package["comment"] = comment
    return package


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--lock", type=Path, required=True)
    parser.add_argument("--debian", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--version", required=True)
    args = parser.parse_args()

    packages = [
        _package(
            "robocap-mcap-converter",
            args.version,
            "Organization: FrodoBots",
            "Application",
            "Compiled PyInstaller application; project source is absent from the runtime image.",
        )
    ]
    lock = tomllib.loads(args.lock.read_text(encoding="utf-8"))
    for item in sorted(lock.get("package", []), key=lambda row: (row["name"], row["version"])):
        packages.append(_package(
            item["name"],
            item["version"],
            "NOASSERTION",
            "Python",
            "Locked build dependency; PyInstaller includes only modules reachable by the converter.",
        ))
    for line in args.debian.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        name, version = line.split("\t", 1)
        packages.append(_package(name, version, "Organization: Debian", "Debian"))

    namespace_seed = hashlib.sha256(
        "\n".join(f"{item['name']}={item['versionInfo']}" for item in packages).encode()
    ).hexdigest()
    document = {
        "spdxVersion": "SPDX-2.3",
        "dataLicense": "CC0-1.0",
        "SPDXID": "SPDXRef-DOCUMENT",
        "name": f"RoboCap-MCAP-Cloud-{args.version}-linux-amd64",
        "documentNamespace": f"https://bitrobot.ai/spdx/robocap-mcap-cloud/{namespace_seed}",
        "creationInfo": {
            "created": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "creators": ["Tool: robocap-cloud-generate-sbom-1"],
        },
        "packages": packages,
        "relationships": [
            {
                "spdxElementId": "SPDXRef-DOCUMENT",
                "relationshipType": "DESCRIBES",
                "relatedSpdxElement": item["SPDXID"],
            }
            for item in packages
        ],
    }
    args.output.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
