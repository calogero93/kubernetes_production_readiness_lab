from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from kubeproof.profile import CompanyProfile
from kubeproof.quantities import InvalidQuantity, parse_quantity


def test_profile_accepts_documented_schema(strict_profile_data: dict[str, Any]) -> None:
    profile = CompanyProfile.model_validate(strict_profile_data)

    assert profile.name == "enterprise-strict"
    assert profile.constraints.resources.max_memory_limit_per_pod == "2Gi"


def test_profile_rejects_unknown_fields(strict_profile_data: dict[str, Any]) -> None:
    strict_profile_data["constraints"]["security"]["magic"] = True

    with pytest.raises(ValidationError, match="magic"):
        CompanyProfile.model_validate(strict_profile_data)


def test_profile_rejects_urls_in_domain_allowlist(strict_profile_data: dict[str, Any]) -> None:
    strict_profile_data["constraints"]["networking"]["allowed_external_domains"] = [
        "https://vendor.example"
    ]

    with pytest.raises(ValidationError, match="invalid DNS name"):
        CompanyProfile.model_validate(strict_profile_data)


@pytest.mark.parametrize(
    ("quantity", "base_units"),
    [("500m", "0.500"), ("2", "2"), ("2Gi", str(2 * 1024**3)), ("250Mi", str(250 * 1024**2))],
)
def test_quantity_parser(quantity: str, base_units: str) -> None:
    assert parse_quantity(quantity) == parse_quantity(base_units)


def test_quantity_parser_rejects_negative_values() -> None:
    with pytest.raises(InvalidQuantity):
        parse_quantity("-1Gi")
