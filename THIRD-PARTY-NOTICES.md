# Third-party notices

Fiduciary's own code is licensed under `AGPL-3.0-or-later` (see `LICENSE`).
It depends on, loads, or can run the third-party components below, each under its
own license. Nothing here changes those licenses. Versions are those resolved in the
built image at the time of the 2026-10-19 license audit; transitive versions float at build time.

This file is informational and is not legal advice.

## Python dependencies (installed in the `portfolio-api` image)

| Package | License |
|---|---|
| fastapi, starlette (BSD-3-Clause), pydantic, pydantic_core, annotated-types, typing-inspection | MIT / BSD-3-Clause |
| uvicorn, httptools, uvloop, watchfiles, websockets, python-dotenv, PyYAML, click, h11, anyio, six | BSD-3-Clause / MIT / Apache-2.0 |
| requests, urllib3, idna, charset-normalizer | Apache-2.0 / MIT / BSD-3-Clause |
| pypdf | BSD-3-Clause |
| snaptrade-python-sdk | MIT |
| aiohttp, aiosignal, frozenlist, multidict, propcache, yarl, attrs, aiohappyeyeballs | Apache-2.0 / MIT / PSF-2.0 |
| itsdangerous | BSD-3-Clause |
| argon2-cffi, argon2-cffi-bindings | MIT |
| pyotp | MIT |
| qrcode | BSD-3-Clause |
| pillow | MIT-CMU (HPND) |
| cryptography, cffi, pycparser | Apache-2.0 OR BSD-3-Clause / MIT-0 / BSD-3-Clause |
| rfc3161-client | Apache-2.0 |
| python-dateutil | Apache-2.0 / BSD-3-Clause |
| typing_extensions | PSF-2.0 |
| http_ece | MIT |
| **certifi** | **MPL-2.0** |
| **pywebpush**, **py-vapid** | **MPL-2.0** |
| **frozendict** | **LGPL-3.0** |

Weak-copyleft components (bold above) are used unmodified as separate, dynamically imported
libraries. `certifi`, `pywebpush`, and `py-vapid` are MPL-2.0 (source available from their
upstream projects); `frozendict` is LGPL-3.0 (a transitive dependency of `snaptrade-python-sdk`,
which is only loaded when the SnapTrade provider is used). You may replace any of them with
your own build.

## Front-end assets (loaded from CDNs, not redistributed in this repository)

| Asset | License |
|---|---|
| Chart.js 4.4.1 (cdnjs) | MIT |
| marked 4.3.0 (cdnjs) | MIT |
| driver.js (jsDelivr) | MIT |
| Catppuccin color palettes (inlined) | MIT, Copyright (c) 2021 Catppuccin contributors |

## Container and system layer

| Component | License | Notes |
|---|---|---|
| `python:3.12-slim-bookworm` base image (Debian 12) | PSF-2.0 and assorted Debian package licenses (including GPL/LGPL userland) | Debian provides corresponding source |
| curl, tar, socat, ca-certificates | curl license; GPL-3.0 (tar); GPL-2.0 (socat); MPL-2.0/GPL (ca-certificates) | Installed from Debian packages |
| acme.sh | GPL-3.0 | Invoked as a separate program; not linked into Fiduciary |
| mcpo (`ghcr.io/open-webui/mcpo`) | MIT | Pulled by the user at deploy time |
| MCP servers run via `uvx` (mcp-server-time, mcp-tavily, monarch-mcp-jamiew) | MIT | Pulled by the user at runtime |
| Ollama | MIT | Installed by the user on the host |

If you build and distribute container images yourself, you are responsible for the
source-offer obligations of the GPL components above.

## Models (not bundled)

No model weights are included in this repository or image. Defaults are downloaded by the
user through Ollama under each model's own license: Qwen chat model (Apache-2.0), `bge-m3`
embeddings (MIT), and the optional Qwen2.5-VL 7B vision model (Apache-2.0). The optional
Anthropic API integration uses the user's own key and is governed by Anthropic's terms.

## Data sources and reference material

Fiduciary calls third-party data services only with the user's own credentials or from the
user's own machine. Those services' terms of service apply to you, not this license.
Some integrations (for example Monarch Money and Yahoo Finance endpoints) are unofficial and may be
restricted by the provider's terms; see `README.md`. Fama-French factor data is
Copyright Eugene F. Fama and Kenneth R. French and is downloaded at runtime, not redistributed.
The reference notes under `knowledge/reference/` are original summaries that cite public
sources; they are licensed with the rest of the project.
