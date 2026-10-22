#!/usr/bin/env python3
"""Check every locked runtime/build version against OSV using only Python 3.11+."""
import json
from pathlib import Path
import re
import sys
import tomllib
from urllib.error import URLError
from urllib.request import Request, urlopen

OSV_URL = "https://api.osv.dev/v1/querybatch"


def locked_dependencies(lock):
    packages = lock.get("package", [])
    dependencies = []
    project_count = 0
    for package in packages:
        if package.get("name") == "server-dock" and package.get("source") == {"editable": "."}:
            project_count += 1
            continue
        name, version = package.get("name"), package.get("version")
        if not name or not version or package.get("source") != {"registry": "https://pypi.org/simple"}:
            raise ValueError("Every third-party package must have a pinned PyPI source.")
        wheels = package.get("wheels", [])
        artifacts = wheels + ([package["sdist"]] if "sdist" in package else [])
        if not wheels or any(
            not re.fullmatch(r"sha256:[0-9a-f]{64}", artifact.get("hash", ""))
            or not artifact.get("url", "").startswith("https://files.pythonhosted.org/")
            for artifact in artifacts
        ):
            raise ValueError(f"Missing or invalid PyPI wheel/hash for {name}=={version}.")
        dependencies.append((name, version))
    if project_count != 1 or not dependencies:
        raise ValueError("Expected one local project and a nonempty dependency lock.")
    return sorted(set(dependencies))


def scan(dependencies):
    payload = {"queries": [
        {"package": {"name": name, "ecosystem": "PyPI"}, "version": version}
        for name, version in dependencies
    ]}
    request = Request(OSV_URL, json.dumps(payload).encode(), headers={"Content-Type": "application/json"})
    with urlopen(request, timeout=60) as response:
        results = json.load(response).get("results")
    if not isinstance(results, list) or len(results) != len(dependencies):
        raise ValueError("OSV returned an incomplete response; scan did not pass.")
    findings = []
    for (name, version), result in zip(dependencies, results):
        if not isinstance(result, dict) or result.get("next_page_token") or set(result) - {"vulns", "next_page_token"}:
            raise ValueError("OSV returned an incomplete or unexpected result; scan did not pass.")
        vulnerabilities = result.get("vulns", [])
        if not isinstance(vulnerabilities, list):
            raise ValueError("OSV returned invalid vulnerability data.")
        for vulnerability in vulnerabilities:
            if not isinstance(vulnerability, dict) or not vulnerability.get("id"):
                raise ValueError("OSV returned an advisory without an identifier.")
            findings.append(f"{name}=={version}: {vulnerability['id']}")
    return findings


def main():
    root = Path(__file__).resolve().parent.parent
    try:
        lock = tomllib.loads((root / "uv.lock").read_text())
        project = tomllib.loads((root / "pyproject.toml").read_text())
        build = project["build-system"]["requires"]
        if build != project["dependency-groups"]["build"] or any("==" not in item for item in build):
            raise ValueError("Build requirements must match the exact pins in the build group.")
        dependencies = locked_dependencies(lock)
        findings = scan(dependencies)
    except (OSError, URLError, ValueError, KeyError, TypeError, AttributeError) as error:
        print(f"Dependency audit failed: {error}", file=sys.stderr)
        return 1
    for finding in findings:
        print(finding)
    print(f"Checked {len(dependencies)} locked versions, including build tools and Python-specific alternatives; "
          f"{len(findings)} known advisories.")
    return int(bool(findings))


if __name__ == "__main__":
    raise SystemExit(main())
