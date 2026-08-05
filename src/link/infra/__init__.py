# SPDX-FileCopyrightText: 2026 Loc.ai Ltd.
# SPDX-License-Identifier: BUSL-1.1

"""Infrastructure layer: OS service management, Zenoh router, provisioning, serving proxy."""

from link.infra.serving_proxy import ServingProxy

__all__ = ["ServingProxy"]
