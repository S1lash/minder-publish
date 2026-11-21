# Resolve these exact release tags to immutable digests before a public image release.
ARG PYTHON_IMAGE=python:3.14.10-slim-trixie@sha256:9aaa474097d094040649f2162a627c0049b408f52138d980916788d80f04802f
ARG NGINX_IMAGE=nginx:1.30.7@sha256:7fad6c0dff97965adbddf28027d934b111fc0221a554315dc274712f3fba24d3
# Debian stable updates that are newer than the pinned base image snapshots.
ARG DEBIAN_BASE_FILES_VERSION=13.8+deb13u8
ARG DEBIAN_GLIBC_VERSION=2.41-12+deb13u5
ARG DEBIAN_OPENSSL_VERSION=3.5.9-1~deb13u1
ARG DEBIAN_ZLIB_VERSION=1:1.3.dfsg+really1.3.1-1+deb13u1

FROM ${NGINX_IMAGE} AS proxy
ARG DEBIAN_BASE_FILES_VERSION
ARG DEBIAN_GLIBC_VERSION
ARG DEBIAN_OPENSSL_VERSION
ARG DEBIAN_ZLIB_VERSION
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        "base-files=${DEBIAN_BASE_FILES_VERSION}" \
        "libc6=${DEBIAN_GLIBC_VERSION}" "libc-bin=${DEBIAN_GLIBC_VERSION}" \
        "openssl=${DEBIAN_OPENSSL_VERSION}" "libssl3t64=${DEBIAN_OPENSSL_VERSION}" \
        "openssl-provider-legacy=${DEBIAN_OPENSSL_VERSION}" \
        "zlib1g=${DEBIAN_ZLIB_VERSION}" \
        libfreetype6=2.13.3+dfsg-1+deb13u2 libgd3=2.3.3-14~deb13u3 libnghttp2-14=1.64.0-1.1+deb13u2 \
    # The supplied configuration does not load JavaScript or XSLT modules.
    && apt-get purge -y nginx-module-njs nginx-module-xslt libxslt1.1 libxml2 \
    && rm -rf /var/lib/apt/lists/*

FROM ${PYTHON_IMAGE} AS builder
ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONDONTWRITEBYTECODE=1
WORKDIR /build
COPY requirements.lock /wheels/requirements.lock
RUN python -m pip download --require-hashes --only-binary=:all: --dest /wheels -r /wheels/requirements.lock \
    && python -m pip install --require-hashes --only-binary=:all: --no-index --find-links=/wheels -r /wheels/requirements.lock
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
COPY skill ./skill
COPY scripts/build-client-wheel.py ./scripts/
RUN python -m pip wheel --no-deps --no-build-isolation --wheel-dir /wheels .
RUN python scripts/build-client-wheel.py --wheel-dir /client-wheels

FROM ${PYTHON_IMAGE} AS runtime
ARG DEBIAN_BASE_FILES_VERSION
ARG DEBIAN_GLIBC_VERSION
ARG DEBIAN_OPENSSL_VERSION
ARG DEBIAN_ZLIB_VERSION
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        "base-files=${DEBIAN_BASE_FILES_VERSION}" \
        "libc6=${DEBIAN_GLIBC_VERSION}" "libc-bin=${DEBIAN_GLIBC_VERSION}" \
        "openssl=${DEBIAN_OPENSSL_VERSION}" "libssl3t64=${DEBIAN_OPENSSL_VERSION}" \
        "openssl-provider-legacy=${DEBIAN_OPENSSL_VERSION}" \
        "zlib1g=${DEBIAN_ZLIB_VERSION}" \
    && rm -rf /var/lib/apt/lists/*
ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PUBLISH_DATA_DIR=/data \
    PUBLISH_CLIENT_WHEEL=/opt/minder-publish/client.whl
COPY --from=builder /client-wheels/minder_publish-*.whl /opt/minder-publish/client.whl
RUN --mount=type=bind,from=builder,source=/wheels,target=/wheels \
    python -m pip install --require-hashes --only-binary=:all: --no-index --find-links=/wheels -r /wheels/requirements.lock \
    && python -m pip install --no-deps /wheels/minder_publish-*.whl \
    # Keep venv/ensurepip usable without bootstrapping the base image's older pip.
    # The audited pip version has one source: requirements.lock.
    && python -c 'from importlib.metadata import version; from pathlib import Path; \
import re; import shutil; import sysconfig; import zipfile; from email.parser import BytesParser; \
pip_version = version("pip"); ensurepip_root = Path(sysconfig.get_path("stdlib")) / "ensurepip"; \
assert not sysconfig.get_config_var("WHEEL_PKG_DIR"), "Inspect the external ensurepip wheel directory"; \
module = ensurepip_root / "__init__.py"; source = module.read_text(); \
versions = re.findall(r"^_PIP_VERSION = \x22([^\x22]+)\x22$", source, re.MULTILINE); \
assert len(versions) == 1, "Inspect the ensurepip version assignment"; \
bundled = ensurepip_root / "_bundled"; old_wheel = bundled / ("pip-" + versions[0] + "-py3-none-any.whl"); \
assert list(bundled.glob("pip-*.whl")) == [old_wheel], "Inspect the ensurepip wheel layout"; \
wheel = Path("/wheels") / ("pip-" + pip_version + "-py3-none-any.whl"); \
archive = zipfile.ZipFile(wheel); metadata = BytesParser().parsebytes(archive.read("pip-" + pip_version + ".dist-info/METADATA")); archive.close(); \
assert metadata["Name"] == "pip" and metadata["Version"] == pip_version, "Mismatched bootstrap wheel"; \
assignment = "_PIP_VERSION = " + chr(34) + versions[0] + chr(34); \
assert source.count(assignment) == 1, "Ambiguous ensurepip assignment"; \
shutil.copyfile(wheel, bundled / wheel.name); \
module.write_text(source.replace(assignment, "_PIP_VERSION = " + chr(34) + pip_version + chr(34), 1)); \
old_wheel.unlink() if old_wheel.name != wheel.name else None; \
assert __import__("ensurepip").version() == pip_version, "Bootstrap version mismatch"' \
    && groupadd --gid 10001 publish \
    && useradd --uid 10001 --gid 10001 --no-create-home --home-dir /nonexistent --shell /usr/sbin/nologin publish \
    && install -d -m 0700 -o 10001 -g 10001 /data
COPY deploy/publish/render_config.py deploy/publish/nginx.conf.template /opt/minder-publish/deploy/publish/
USER 10001:10001
WORKDIR /data
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD ["python", "-c", "import os, urllib.request, urllib.parse; host=urllib.parse.urlsplit(os.environ['PUBLISH_BASE_URL']).netloc; request=urllib.request.Request('http://127.0.0.1:8080/healthz', headers={'Host':host}); urllib.request.urlopen(request, timeout=3).close()"]
ENTRYPOINT ["minder-publish-server"]
CMD ["serve", "--host", "0.0.0.0", "--port", "8080"]
