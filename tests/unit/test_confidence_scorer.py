"""Encoding shape and fitting behaviour of the hand-rolled logistic scorer."""

import pytest

from crossfoot.confidence.scorer import encode, fit, probability
from crossfoot.constants import ExtractionRoute, FieldFamily
from crossfoot.models.extraction import FieldSignals

OPTIONAL_SIGNALS = (
    "self_consistency",
    "det_llm_agreement",
    "validator_pass",
    "grammar_match",
    "crossfoot_ok",
)
# Five (indicator, value) pairs, the residual flag, char ambiguity, one slot per
# route. The route slots replaced one slot per quality tier: the tier was a
# generator degradation label, and no real document carries one.
EXPECTED_FEATURES = 2 * len(OPTIONAL_SIGNALS) + 2 + len(ExtractionRoute)

BASE = FieldSignals(route=ExtractionRoute.DIGITAL_PDF)


def _with(name: str, value: float | None) -> FieldSignals:
    return BASE.model_copy(update={name: value})


def test_encoding_width_is_stable() -> None:
    assert len(encode(BASE)) == EXPECTED_FEATURES
    assert len(encode(_with("self_consistency", 1.0))) == EXPECTED_FEATURES


@pytest.mark.parametrize("name", OPTIONAL_SIGNALS)
def test_every_optional_signal_separates_absence_from_a_present_zero(name: str) -> None:
    present = encode(_with(name, 0.0))
    absent = encode(_with(name, None))
    assert present != absent
    assert sum(present) - sum(absent) == pytest.approx(1.0)


def test_route_is_one_hot() -> None:
    for route in ExtractionRoute:
        route_slots = encode(FieldSignals(route=route))[-len(ExtractionRoute) :]
        assert sum(route_slots) == pytest.approx(1.0)


def test_an_unrouted_field_lights_no_route_slot() -> None:
    # Absence is a state the model can learn, exactly like the (indicator, value)
    # pairs above: all-zero says "no route known", not "this route".
    route_slots = encode(FieldSignals())[-len(ExtractionRoute) :]
    assert sum(route_slots) == pytest.approx(0.0)


def test_residual_suspect_changes_the_encoding() -> None:
    assert encode(BASE.model_copy(update={"crossfoot_residual_suspect": True})) != encode(BASE)


def test_fit_without_samples_refuses_to_invent_a_model() -> None:
    with pytest.raises(ValueError, match="no training samples"):
        fit(FieldFamily.AMOUNT, [])


def test_model_remembers_its_family() -> None:
    samples = [(_with("validator_pass", 1.0), True), (_with("validator_pass", 0.0), False)]
    assert fit(FieldFamily.DATE, samples).field_family is FieldFamily.DATE


def test_char_ambiguity_pushes_confidence_down() -> None:
    clean = _with("validator_pass", 1.0)
    noisy = clean.model_copy(update={"char_ambiguity": 1.0})
    model = fit(FieldFamily.REFERENCE, [(clean, True)] * 8 + [(noisy, False)] * 8)
    assert model.predict(clean) > model.predict(noisy)


def test_probability_is_the_logistic_link() -> None:
    assert probability(0.0) == pytest.approx(0.5)
    assert probability(100.0) > 0.99
    assert probability(-100.0) < 0.01
