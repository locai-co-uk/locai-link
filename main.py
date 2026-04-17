# SPDX-FileCopyrightText: 2026 Loc.ai Ltd.
# SPDX-License-Identifier: BUSL-1.1

import sys
from pathlib import Path

# Add 'src' to python path so we can import the package without installing it
sys.path.insert(0, str(Path(__file__).parent / "src"))

from link.main import main

if __name__ == "__main__":
    main()
