# SPDX-FileCopyrightText: 2026 Loc.ai Ltd.
# SPDX-License-Identifier: BUSL-1.1

import pytest

from link.config.templating import resolve_templates

CTX = {
    "identity": {"device_id": "dev_abc", "api_key": "sk_xxx", "api_url": "https://api.test"},
    "api_url": "https://api.test",
}


# --- Scalar substitution ---


@pytest.mark.parametrize(
    "template,expected",
    [
        ("${identity.device_id}", "dev_abc"),
        ("prefix/${identity.device_id}/suffix", "prefix/dev_abc/suffix"),
        ("${identity.device_id}@${identity.api_url}", "dev_abc@https://api.test"),
        ("${api_url}", "https://api.test"),
    ],
)
def test_scalar_substitution(template, expected):
    assert resolve_templates(template, CTX) == expected


@pytest.mark.parametrize(
    "template,ctx",
    [
        ("${missing.key}", CTX),  # unknown top-level key
        ("${identity.nope}", CTX),  # unknown nested path
        ("/commands/{cid}/status", CTX),  # non-dollar runtime placeholder
        ("${identity.device_id}", {}),  # empty context
    ],
)
def test_placeholder_left_intact(template, ctx):
    assert resolve_templates(template, ctx) == template


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
