Loc.ai:Link
Copyright 2026 Loc.ai Ltd.
This product includes software developed at Loc.ai Ltd. (https://www.locai.co.uk).

### THIRD-PARTY SOFTWARE NOTICES

This software includes components licensed under various open-source licenses.
The full text of these licenses and required attribution notices are provided
in the `LICENSES-external/` directory and the `THIRDPARTYLICENSES` file.

#### Runtime dependencies

1. NumPy (numpy.org)
   License: BSD 3-Clause
   Copyright (c) 2005-2026, NumPy Developers

2. psutil (github.com/giampaolo/psutil)
   License: BSD 3-Clause
   Copyright (c) 2009, Giampaolo Rodola'

3. Requests (github.com/psf/requests)
   License: Apache 2.0
   Copyright (c) Kenneth Reitz and contributors

4. Pydantic (github.com/pydantic/pydantic)
   License: MIT
   Copyright (c) 2017 to present Pydantic Services Inc. and individual contributors

5. Eclipse Zenoh (github.com/eclipse-zenoh/zenoh)
   License: EPL 2.0 OR Apache 2.0
   Copyright (c) 2022 ZettaScale Technology

6. certifi (github.com/certifi/python-certifi)
   License: MPL 2.0
   Copyright (c) 2015 Kenneth Reitz

7. packaging (github.com/pypa/packaging)
   License: Apache 2.0 OR BSD 2-Clause
   Copyright (c) Donald Stufft and individual contributors

#### Development dependencies

8. Ruff (github.com/astral-sh/ruff)
   License: MIT
   Copyright (c) 2022 Charlie Marsh

9. pytest (github.com/pytest-dev/pytest)
   License: MIT
   Copyright (c) 2004 Holger Krekel and others

10. pytest-mock (github.com/pytest-dev/pytest-mock)
    License: MIT
    Copyright (c) 2014 Bruno Oliveira

11. pytest-cov (github.com/pytest-dev/pytest-cov)
    License: MIT
    Copyright (c) 2010 Meme Dough

12. MkDocs (github.com/mkdocs/mkdocs)
    License: BSD 2-Clause
    Copyright (c) 2014-present, Tom Christie

13. Material for MkDocs (github.com/squidfunk/mkdocs-material)
    License: MIT
    Copyright (c) 2016-2026 Martin Donath

14. mkdocstrings (github.com/mkdocstrings/mkdocstrings)
    License: ISC
    Copyright (c) 2019, Timothée Mazzucotelli

#### Plugins

Plugins shipped under `plugins/` (e.g. `language_model`, `audio_transcriber`)
download and run additional third-party engines at install time:

- llama.cpp (MIT)
- whisper.cpp (MIT)
- llama-swap (MIT) — Copyright (c) 2024 Benson Wong (github.com/mostlygeek/llama-swap)

Those engines remain governed by their respective upstream licenses.
