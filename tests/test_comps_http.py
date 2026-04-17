# SPDX-FileCopyrightText: 2026 Loc.ai Ltd.
# SPDX-License-Identifier: BUSL-1.1

from link.components.http import HttpPoller, HttpPublisher

PATCH_TARGET = "link.components.http.HttpClient"


def test_rest_polling_source(mocker):
    """Test that RestPolling uses the HttpClient adapter correctly."""

    mock_client_cls = mocker.patch(PATCH_TARGET)
    mock_instance = mock_client_cls.return_value

    mock_instance.get.return_value = {"temperature_celsius": 42}

    source = HttpPoller(url="http://test.local", interval=0.1)
    data = source()

    assert data == {"temperature_celsius": 42}
    mock_instance.get.assert_called()


def test_rest_publisher_sink(mocker):
    """Test that RestPublisher calls post on the adapter."""

    mock_client_cls = mocker.patch(PATCH_TARGET)
    mock_instance = mock_client_cls.return_value

    # Simulate a successful POST
    mock_instance.post.return_value = True

    sink = HttpPublisher(url="http://test.local", endpoint="/api/v1")

    payload = {"foo": "bar"}
    result = sink(payload)

    # Now result is not None because the mock returned True
    assert result is not None
    assert result
    mock_instance.post.assert_called_with(json_data=payload)
