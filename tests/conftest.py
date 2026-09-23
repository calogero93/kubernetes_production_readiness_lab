from __future__ import annotations

from typing import Any

import pytest

from kubeproof.core.profile import CompanyProfile


@pytest.fixture
def strict_profile_data() -> dict[str, Any]:
    return {
        "schema_version": "0.1",
        "name": "enterprise-strict",
        "constraints": {
            "security": {
                "allow_cluster_admin": False,
                "allow_wildcard_rbac": False,
                "allow_privileged": False,
                "allow_host_network": False,
                "allow_host_pid": False,
                "allow_host_path": False,
                "require_run_as_non_root": True,
                "require_read_only_root_filesystem": True,
                "allowed_added_capabilities": [],
            },
            "resources": {
                "require_requests": True,
                "require_limits": True,
                "max_memory_limit_per_pod": "2Gi",
                "max_cpu_limit_per_pod": "2",
                "max_sampled_memory_per_pod": "2Gi",
                "max_sampled_cpu_per_pod": "2",
            },
            "reliability": {"minimum_replicas": 2},
            "networking": {"allow_public_egress": False, "allowed_external_domains": []},
        },
    }


@pytest.fixture
def strict_profile(strict_profile_data: dict[str, Any]) -> CompanyProfile:
    return CompanyProfile.model_validate(strict_profile_data)


@pytest.fixture
def unsafe_manifest() -> bytes:
    return b"""\
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRole
metadata:
  name: broad-controller
rules:
  - apiGroups: [\"*\"]
    resources: [\"*\"]
    verbs: [\"*\"]
---
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRoleBinding
metadata:
  name: controller-admin
roleRef:
  apiGroup: rbac.authorization.k8s.io
  kind: ClusterRole
  name: cluster-admin
subjects:
  - kind: ServiceAccount
    name: controller
    namespace: product
---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: controller
  namespace: product
spec:
  replicas: 1
  selector:
    matchLabels: {app: controller}
  template:
    metadata:
      labels: {app: controller}
    spec:
      hostNetwork: true
      volumes:
        - name: host
          hostPath: {path: /var/run}
      containers:
        - name: controller
          image: example/controller:1.0
          args: [\"--endpoint=https://api.vendor.example/v1\"]
          securityContext:
            privileged: true
            runAsUser: 0
            capabilities:
              add: [SYS_ADMIN]
"""
