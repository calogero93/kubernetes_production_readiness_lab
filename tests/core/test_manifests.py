from __future__ import annotations

import pytest

from kubeproof.core.manifests import ManifestError, parse_manifests


def test_crd_enum_literal_equals_is_parsed_without_unsafe_yaml_tags() -> None:
    manifest = b"""\
apiVersion: apiextensions.k8s.io/v1
kind: CustomResourceDefinition
metadata: {name: matchers.example.com}
spec:
  enum:
    - =
    - '!='
"""
    resources = parse_manifests(manifest)

    assert resources[0]["spec"]["enum"] == ["=", "!="]


def test_render_with_too_many_workloads_is_rejected_before_runtime() -> None:
    pod = b"apiVersion: v1\nkind: Pod\nmetadata: {name: example}\nspec: {}\n---\n"
    with pytest.raises(ManifestError, match="500 workloads"):
        parse_manifests(pod * 501)
