# SPDX-FileCopyrightText: 2026 Loc.ai Ltd.
# SPDX-License-Identifier: BUSL-1.1

import json

import pytest

from link.config.loader import load_config


def test_load_valid_config(tmp_path, valid_config_dict):
    p = tmp_path / "config.json"
    p.write_text(json.dumps(valid_config_dict))

    config = load_config(p)

    assert config.pipelines[0].sink is not None
    assert config.pipelines[0].sink.type == "console"


def test_optional_sink(tmp_path, valid_config_dict):
    """Test that sink is nullable."""
    valid_config_dict["pipelines"][0]["sink"] = None

    p = tmp_path / "no_sink.json"
    p.write_text(json.dumps(valid_config_dict))

    config = load_config(p)
    assert config.pipelines[0].sink is None


def test_validation_error(tmp_path):
    bad_data = {"version": 2.2, "pipelines": []}  # Missing device
    p = tmp_path / "bad.json"
    p.write_text(json.dumps(bad_data))

    with pytest.raises(ValueError) as exc:
        load_config(p)
    assert "validation failed" in str(exc.value)


def test_missing_file_raises_filenotfound(tmp_path):
    with pytest.raises(FileNotFoundError):
        load_config(tmp_path / "does_not_exist.json")


def test_invalid_json_raises_valueerror(tmp_path):
    p = tmp_path / "broken.json"
    p.write_text("{not valid json")

    with pytest.raises(ValueError) as exc:
        load_config(p)
    assert "Invalid JSON syntax" in str(exc.value)


def test_pipeline_source_requires_type(tmp_path, valid_config_dict):
    """A source without a `type` must fail validation (GenericConfig.type is required)."""
    del valid_config_dict["pipelines"][0]["source"]["type"]
    p = tmp_path / "no_source_type.json"
    p.write_text(json.dumps(valid_config_dict))

    with pytest.raises(ValueError) as exc:
        load_config(p)
    assert "validation failed" in str(exc.value)
