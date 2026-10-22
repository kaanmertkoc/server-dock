"""The dependency audit must fail on advisories, missing hashes, or incomplete scans."""
from io import BytesIO
import json
import sys
import unittest
from unittest.mock import patch
from urllib.error import URLError

if sys.version_info >= (3, 11):
    from scripts import audit_dependencies as audit


@unittest.skipUnless(sys.version_info >= (3, 11), "Audit runs on Python 3.12 in CI")
class DependencyAuditTests(unittest.TestCase):
    def response(self, results):
        return patch.object(audit, "urlopen", return_value=BytesIO(json.dumps({"results": results}).encode()))

    def test_clean_scan_queries_every_locked_version(self):
        dependencies = [("example", "1.0"), ("example", "2.0")]
        with self.response([{}, {}]) as request:
            self.assertEqual(audit.scan(dependencies), [])
        queries = json.loads(request.call_args.args[0].data)["queries"]
        self.assertEqual([item["version"] for item in queries], ["1.0", "2.0"])
        self.assertTrue(all(item["package"]["ecosystem"] == "PyPI" for item in queries))

    def test_advisory_reports_affected_package(self):
        with self.response([{"vulns": [{"id": "GHSA-test-advisory"}]}]):
            self.assertEqual(audit.scan([("example", "1.0")]), ["example==1.0: GHSA-test-advisory"])

    def test_missing_partial_and_invalid_results_do_not_pass(self):
        for results in ([], [None], [{"next_page_token": "more"}], [{"error": "busy"}],
                        [{"vulns": None}], [{"vulns": [{}]}]):
            with self.subTest(results=results), self.response(results), self.assertRaises(ValueError):
                audit.scan([("example", "1.0")])

    def test_network_failure_does_not_pass(self):
        with patch.object(audit, "urlopen", side_effect=URLError("offline")), self.assertRaises(URLError):
            audit.scan([("example", "1.0")])

    def test_real_lock_covers_build_tools_and_alternate_python_versions(self):
        lock = audit.tomllib.loads((audit.Path(__file__).parent / "uv.lock").read_text())
        dependencies = audit.locked_dependencies(lock)
        project = audit.tomllib.loads((audit.Path(__file__).parent / "pyproject.toml").read_text())
        for requirement in project["dependency-groups"]["build"]:
            self.assertIn(tuple(requirement.split("==")), dependencies)
        for package in lock["package"]:
            if "resolution-markers" in package:
                self.assertIn((package["name"], package["version"]), dependencies)
        package = next(package for package in lock["package"] if "wheels" in package)
        package["wheels"][0]["hash"] = ""
        with self.assertRaises(ValueError):
            audit.locked_dependencies(lock)

    def test_unreviewed_dependency_sources_do_not_pass(self):
        lock = audit.tomllib.loads((audit.Path(__file__).parent / "uv.lock").read_text())
        package = next(package for package in lock["package"] if "wheels" in package)
        package["source"] = {"git": "https://example.invalid/dependency"}
        with self.assertRaises(ValueError):
            audit.locked_dependencies(lock)


if __name__ == "__main__":
    unittest.main()
