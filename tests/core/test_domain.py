from __future__ import annotations

from inspect import getmembers, isclass

import pytest
from pydantic import BaseModel, ValidationError

from kubeproof.core import domain
from kubeproof.core.domain import Finding, Severity


def test_finding_without_observation_is_invalid() -> None:
    with pytest.raises(ValidationError, match="must reference at least one observation"):
        Finding(
            id="finding-1",
            check_id="static.security",
            severity=Severity.WARNING,
            title="Unsupported assertion",
            description="This has no supporting observation.",
            observation_ids=(),
        )


def test_all_domain_model_fields_have_descriptions() -> None:
    domain_models = (
        model
        for _, model in getmembers(domain, isclass)
        if issubclass(model, BaseModel)
        and model is not domain.StrictModel
        and model.__module__ == domain.__name__
    )

    missing_descriptions = [
        f"{model.__name__}.{field_name}"
        for model in domain_models
        for field_name, field in model.model_fields.items()
        if not field.description
    ]

    assert missing_descriptions == []
