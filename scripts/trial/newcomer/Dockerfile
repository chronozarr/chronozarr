# syntax=docker/dockerfile:1.7
#
# Clean Linux machines for the Phase 0 baseline: a scientist who has never used chronozarr.
#
#   scripts/trial/build-newcomer-images.sh        builds chronozarr-newcomer:ubuntu and chronozarr-newcomer:conda
#
# Supply chain: nothing in these images was published in the 7 days before 2026-10-16 (cutoff 2026-10-09).
#   - base image: ubuntu 24.04 by digest (built 2026-09-18)
#   - apt: snapshot.ubuntu.com at 2026-10-08, so no package newer than that
#   - uv 0.11.2 (2026-03-26), cloudflared 2026.10.0 (2026-10-05), Miniforge 26.7.2-0 (2026-09-08): pinned release assets with sha256
#   - Python packages: the wheelhouse, resolved by the host's uv under its 7-day quarantine (build_wheelhouse.py)
#   - look's Playwright 1.63.0 (2026-09-15) and its Chromium build: Playwright pins the browser for each release
# The wheelhouse is not in the build context: pass it as the named context `wheelhouse` (build-newcomer-images.sh does).

ARG UBUNTU=ubuntu@sha256:534baea6a22c03a63003dbc8dbe78fe34bc0d7e595d9a9dc9834884ff530eb55

FROM ${UBUNTU} AS base
ARG APT_SNAPSHOT=20261008T000000Z
ARG UV_VERSION=0.11.2
ARG UV_SHA256=04792cac761c4a6ba78267f36f2af541b7f92196d42ac55d21d3ff6b0f5ab6a5
ARG CLOUDFLARED_VERSION=2026.10.0
ARG CLOUDFLARED_SHA256=e6422b9d4f72d3194bc5a38676f13667c06666523217b842a877d72a80b5ac08
ARG PLAYWRIGHT_VERSION=1.63.0
# Today minus 7 days, for the few packages that look's own venv takes from PyPI (playwright, greenlet, pyee, typing-extensions).
ARG LOOK_EXCLUDE_NEWER=2026-10-09T00:00:00Z
ENV DEBIAN_FRONTEND=noninteractive

# The snapshot is https and the base image has no CA bundle yet: the first install skips TLS verification, which is safe
# because apt verifies every package through the signed Release files (Signed-By).
RUN printf '%s\n' \
      'Types: deb' \
      "URIs: https://snapshot.ubuntu.com/ubuntu/${APT_SNAPSHOT}/" \
      'Suites: noble noble-updates noble-backports noble-security' \
      'Components: main universe restricted multiverse' \
      'Signed-By: /usr/share/keyrings/ubuntu-archive-keyring.gpg' \
      > /etc/apt/sources.list.d/ubuntu.sources \
 && apt-get -o Acquire::https::Verify-Peer=false -o Acquire::https::Verify-Host=false update \
 && apt-get -o Acquire::https::Verify-Peer=false -o Acquire::https::Verify-Host=false install -y --no-install-recommends ca-certificates \
 && apt-get update \
 && apt-get install -y --no-install-recommends curl git unzip \
 && rm -rf /var/lib/apt/lists/*

RUN curl -fsSL -o /tmp/uv.tar.gz "https://github.com/astral-sh/uv/releases/download/${UV_VERSION}/uv-aarch64-unknown-linux-gnu.tar.gz" \
 && echo "${UV_SHA256}  /tmp/uv.tar.gz" | sha256sum -c - \
 && tar -xzf /tmp/uv.tar.gz -C /usr/local/bin --strip-components=1 uv-aarch64-unknown-linux-gnu/uv uv-aarch64-unknown-linux-gnu/uvx \
 && curl -fsSL -o /usr/local/bin/cloudflared "https://github.com/cloudflare/cloudflared/releases/download/${CLOUDFLARED_VERSION}/cloudflared-linux-arm64" \
 && echo "${CLOUDFLARED_SHA256}  /usr/local/bin/cloudflared" | sha256sum -c - \
 && chmod 755 /usr/local/bin/cloudflared \
 && rm /tmp/uv.tar.gz \
 && uv --version && cloudflared --version

# look: headless Chromium behind a small Python script. Its interpreter, venv and browsers live under /opt and are
# not on anyone's PATH. The Python is a uv-managed download kept in /opt/look/python (not in the scientist's uv dir).
RUN UV_PYTHON_INSTALL_DIR=/opt/look/python uv venv --python 3.12 /opt/look/venv \
 && uv pip install --python /opt/look/venv/bin/python --exclude-newer "${LOOK_EXCLUDE_NEWER}" "playwright==${PLAYWRIGHT_VERSION}" \
 && PLAYWRIGHT_BROWSERS_PATH=/opt/pw-browsers /opt/look/venv/bin/playwright install --with-deps chromium-headless-shell \
 && rm -rf /var/lib/apt/lists/* \
 && chmod -R a+rX /opt/look /opt/pw-browsers

# Ubuntu's image has a user "ubuntu" at uid 1000.
RUN userdel -r ubuntu \
 && useradd --create-home --uid 1000 --shell /bin/bash scientist \
 && mkdir -p /home/scientist/shots \
 && chown scientist:scientist /home/scientist/shots

# Python 3.13 is already installed for the scientist, so `uv tool install --python 3.13` never waits for an interpreter download
# (a few seconds on a good network, minutes on a slow one). --no-bin keeps ~/.local/bin from existing before the first tool install.
USER scientist
RUN uv python install --no-bin 3.13
USER root

# The quarantined package index: files only, no network index. pip reads its settings from the environment; uv 0.11.2 ignores
# UV_NO_INDEX (tested), so uv reads them from the system config file.
COPY --from=wheelhouse . /opt/wheelhouse
RUN chmod -R a+rX /opt/wheelhouse \
 && mkdir -p /etc/uv \
 && printf '%s\n' 'no-index = true' 'find-links = ["/opt/wheelhouse"]' > /etc/uv/uv.toml
ENV LANG=C.UTF-8 \
    PIP_NO_INDEX=1 \
    PIP_FIND_LINKS=/opt/wheelhouse

# uv's own installer puts uv in ~/.local/bin and that directory on PATH, which is also where `uv tool install` puts commands.
# uv is in /usr/local/bin here, so a login shell gets the directory on PATH explicitly.
RUN printf '%s\n' 'export PATH="$HOME/.local/bin:$PATH"' > /etc/profile.d/local-bin.sh

# look's script is copied last, so changing it rebuilds one small layer.
COPY look.py /opt/look/look.py
RUN printf '%s\n' '#!/bin/sh' 'PLAYWRIGHT_BROWSERS_PATH=/opt/pw-browsers exec /opt/look/venv/bin/python /opt/look/look.py "$@"' > /usr/local/bin/look \
 && chmod 755 /usr/local/bin/look /opt/look/look.py

WORKDIR /home/scientist
CMD ["sleep", "infinity"]


FROM base AS ubuntu
RUN apt-get update \
 && apt-get install -y --no-install-recommends python3 python3-venv \
 && rm -rf /var/lib/apt/lists/*
USER scientist


FROM base AS conda
ARG MINIFORGE_VERSION=26.7.2-0
ARG MINIFORGE_SHA256=89b786c8d2c8b0fda7553914c1314ae4ddaa094503802f279377b19ac4463cb2
# Packages from conda channels are not quarantined, so conda may not fetch any: what the installer unpacked is what exists.
ENV CONDA_OFFLINE=true
# The installer is a GitHub release asset, fetched once on the host by build-newcomer-images.sh (the named context `assets`).
USER scientist
RUN --mount=type=bind,from=assets,target=/assets \
    echo "${MINIFORGE_SHA256}  /assets/Miniforge3-${MINIFORGE_VERSION}-Linux-aarch64.sh" | sha256sum -c - \
 && bash "/assets/Miniforge3-${MINIFORGE_VERSION}-Linux-aarch64.sh" -b -p /home/scientist/miniforge3 \
 && /home/scientist/miniforge3/bin/conda init bash
USER root
# conda init writes ~/.bashrc, which Ubuntu's skeleton skips in a non-interactive shell; a login shell reads /etc/profile.d.
RUN printf '%s\n' \
      'if [ -f /home/scientist/miniforge3/etc/profile.d/conda.sh ] && [ -z "${CONDA_DEFAULT_ENV:-}" ]; then' \
      '  . /home/scientist/miniforge3/etc/profile.d/conda.sh' \
      '  conda activate base' \
      'fi' \
      > /etc/profile.d/conda-base.sh
USER scientist
