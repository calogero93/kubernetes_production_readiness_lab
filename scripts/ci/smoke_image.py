"""Run inside the production container to verify the shipped application."""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
import urllib.request
from pathlib import Path

from kubeproof.core.plans import EvaluationPlan


def run_cli(*arguments: str, expected: tuple[int, ...] = (0,)) -> str:
    result = subprocess.run(["kubeproof", *arguments], capture_output=True, text=True, timeout=60)
    assert result.returncode in expected, (
        arguments,
        result.returncode,
        result.stdout,
        result.stderr,
    )
    return result.stdout


def get(path: str) -> bytes:
    with urllib.request.urlopen(f"http://127.0.0.1:8000{path}", timeout=5) as response:
        assert response.status == 200
        body = response.read()
        assert isinstance(body, bytes)
        return body


def main() -> None:
    assert os.getuid() == 10001, "production image must run as the unprivileged application user"
    assert os.statvfs("/").f_flag & os.ST_RDONLY, "root filesystem must be read-only"
    status = Path("/proc/self/status").read_text()
    assert re.search(r"^CapEff:\s+0+$", status, re.MULTILINE), (
        "effective capabilities must be empty"
    )
    assert re.search(r"^NoNewPrivs:\s+1$", status, re.MULTILINE)
    with tempfile.NamedTemporaryFile(dir="/data") as handle:
        handle.write(b"writable application data")

    for path in ("/healthz", "/readyz"):
        assert get(path).strip() == b"ok"
    assert b"kubeproof_http_requests_total" in get("/metrics")
    page = get("/").decode()
    assets = re.findall(r'(?:src|href)="(/assets/[^\"]+)"', page)
    assert any(asset.endswith(".js") for asset in assets), "built frontend script is missing"
    for asset in assets:
        assert get(asset), f"empty frontend asset: {asset}"
    json.loads(get("/api/chart/config"))
    catalog = json.loads(run_cli("catalog"))
    assert {item["name"] for item in catalog["capabilities"]} == {
        "http_service",
        "cpu_load",
        "resource_sample",
        "dns_observation",
        "pod_recovery",
    }

    with tempfile.TemporaryDirectory(prefix="kubeproof-ci-") as directory:
        bundle = Path(directory) / "evaluation"
        run_cli(
            "inspect",
            "/fixtures/http-fixture",
            "--profile",
            "/fixtures/profiles/enterprise-strict.yaml",
            "--plan",
            "/fixtures/plans/http-composition.json",
            "--output",
            str(bundle),
            expected=(0, 2),
        )
        evaluation = json.loads((bundle / "evaluation.json").read_text())
        submitted = json.loads(Path("/fixtures/plans/http-composition.json").read_text())
        assert EvaluationPlan.model_validate(
            evaluation["execution_options"]["test_plan"]
        ).digest() == (EvaluationPlan.model_validate(submitted).digest())
        assert evaluation["environment"] is None, "static inspection must not create a cluster"
        assert evaluation["plan_execution"] is None
        assert "sealed" in run_cli("verify", str(bundle))
        assert "Imported evaluation" in run_cli(
            "history", "import", str(bundle), "--data-dir", "/data"
        )
        assert evaluation["evaluation_id"] in run_cli("history", "list", "--data-dir", "/data")
        entries = json.loads(get("/api/evaluations"))
        assert evaluation["evaluation_id"] in {item["evaluation_id"] for item in entries}
        assert json.loads(get(f"/api/evaluations/{evaluation['evaluation_id']}")) == evaluation
        with (bundle / "report.md").open("a") as report:
            report.write("\nCI tamper detection probe\n")
        run_cli("verify", str(bundle), expected=(1,))

    print(
        "Production smoke passed: non-root, read-only, API, frontend assets, "
        "CLI, plan, seal and history."
    )


if __name__ == "__main__":
    main()
