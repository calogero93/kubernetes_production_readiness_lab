"""Generate separate validated AI commentary from an existing sealed evaluation."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from kubeproof.core.domain import EvaluationResult
from kubeproof.evidence.bundle import canonical_json_bytes, verify_bundle
from kubeproof.intelligence.evaluation import EvaluationAI


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("bundle", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args()
    if arguments.output.exists():
        raise FileExistsError(f"Commentary output already exists: {arguments.output}")
    verified = verify_bundle(arguments.bundle)
    if verified.bundle_sha256 is None:
        raise ValueError("AI commentary requires a sealed source bundle")
    evaluation = verified.evaluation.model_copy(update={"ai_interpretation": None})
    manifest = arguments.bundle / "input/rendered-manifests.redacted.yaml"
    context = {
        "evaluation": evaluation.model_dump(mode="json"),
        "redacted_manifest": manifest.read_text(),
    }
    if len(canonical_json_bytes(context)) > 1_000_000:
        raise ValueError("AI interpretation context exceeds the 1 MiB limit")
    interpretation = EvaluationAI().interpret(context)
    if interpretation.status != "completed" or not interpretation.points:
        raise ValueError("Model did not produce a completed, nonempty interpretation")
    EvaluationResult.model_validate(
        {**evaluation.model_dump(mode="python"), "ai_interpretation": interpretation}
    )
    payload = {
        "source_evaluation_id": evaluation.evaluation_id,
        "source_bundle_sha256": verified.bundle_sha256,
        "selected_model": os.environ.get("KUBEPROOF_AI_MODEL"),
        "ai_interpretation": interpretation.model_dump(mode="json"),
    }
    # Exclusive creation keeps both the original sealed bundle and prior commentary intact.
    with arguments.output.open("x") as output:
        output.write(json.dumps(payload, indent=2) + "\n")
    print(f"Validated {len(interpretation.points)} AI points: {arguments.output}")


if __name__ == "__main__":
    main()
