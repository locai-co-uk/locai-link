# SPDX-FileCopyrightText: 2026 Loc.ai Ltd.
# SPDX-License-Identifier: BUSL-1.1

from link.config.templating import resolve_templates

CTX = {
    "identity": {"device_id": "dev_abc", "api_key": "sk_xxx", "api_url": "https://api.test"},
    "api_url": "https://api.test",
}


# --- Scalar substitution ---


def test_resolve_single_placeholder():
    assert resolve_templates("${identity.device_id}", CTX) == "dev_abc"


def test_resolve_placeholder_embedded_in_string():
    assert resolve_templates("prefix/${identity.device_id}/suffix", CTX) == "prefix/dev_abc/suffix"


def test_resolve_multiple_placeholders_in_one_string():
    result = resolve_templates("${identity.device_id}@${identity.api_url}", CTX)
    assert result == "dev_abc@https://api.test"


def test_unknown_placeholder_left_intact():
    assert resolve_templates("${missing.key}", CTX) == "${missing.key}"


def test_unknown_nested_path_left_intact():
    assert resolve_templates("${identity.nope}", CTX) == "${identity.nope}"


def test_runtime_placeholders_not_touched():
    """Non-dollar placeholders like {cid} pass through unchanged."""
    assert resolve_templates("/commands/{cid}/status", CTX) == "/commands/{cid}/status"


def test_top_level_key_lookup():
    assert resolve_templates("${api_url}", CTX) == "https://api.test"


# --- Structure traversal ---


def test_resolve_dict():
    obj = {
        "url": "${identity.api_url}/agent/${identity.device_id}/logs",
        "api_key": "${identity.api_key}",
    }
    result = resolve_templates(obj, CTX)
    assert result == {
        "url": "https://api.test/agent/dev_abc/logs",
        "api_key": "sk_xxx",
    }


def test_resolve_nested_dict():
    obj = {"outer": {"inner": "${identity.device_id}"}}
    assert resolve_templates(obj, CTX) == {"outer": {"inner": "dev_abc"}}


def test_resolve_list():
    obj = ["${identity.device_id}", "${identity.api_key}", "literal"]
    assert resolve_templates(obj, CTX) == ["dev_abc", "sk_xxx", "literal"]


def test_resolve_mixed_structure():
    obj = {
        "pipelines": [
            {
                "id": "p1",
                "source": {"type": "http_poll", "args": {"url": "${identity.api_url}/x"}},
            }
        ]
    }
    result = resolve_templates(obj, CTX)
    assert result["pipelines"][0]["source"]["args"]["url"] == "https://api.test/x"


def test_non_string_values_unchanged():
    obj = {"interval": 30, "active": True, "handlers": []}
    assert resolve_templates(obj, CTX) == obj


def test_empty_context():
    assert resolve_templates("${identity.device_id}", {}) == "${identity.device_id}"
