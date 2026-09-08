import argparse

import pytest

from experiment.create_equal_scale_baselines import (
    _optimization_cases,
    _parse_seed_list,
)


def test_seed_list_parser_deduplicates_and_preserves_order():
    assert _parse_seed_list("42, 43,42,44") == [42, 43, 44]


def test_seed_list_parser_rejects_empty_or_non_integer_values():
    with pytest.raises(argparse.ArgumentTypeError):
        _parse_seed_list("")
    with pytest.raises(argparse.ArgumentTypeError, match="invalid seed"):
        _parse_seed_list("42,test")


def test_seeded_cases_have_unique_ids_and_seed_metadata(tmp_path):
    records = [
        {
            "status": "ok",
            "key": "troy",
            "city": "Troy",
            "artifacts": {"cfg": str(tmp_path / "sim.sumocfg")},
            "experiment_seed": 43,
        }
    ]

    cases = _optimization_cases(records, seed_suffix=43)

    assert cases[0]["id"] == "troy_equal_scale_delay_seed_43"
    assert cases[0]["seed"] == 43
