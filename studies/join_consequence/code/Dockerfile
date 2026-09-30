FROM ubuntu:22.04

ENV DEBIAN_FRONTEND=noninteractive
RUN printf '#!/bin/sh\nexit 101\n' > /usr/sbin/policy-rc.d \
    && chmod +x /usr/sbin/policy-rc.d \
    && apt-get update \
    && apt-get install -y --no-install-recommends \
       build-essential ca-certificates clang-14 libicu70 libldap-2.5-0 \
       libllvm14 liblz4-1 libpam0g libpq-dev libpython3.10 libreadline8 \
       libselinux1 libssl3 libsystemd0 libuuid1 libxml2 libxslt1.1 \
       llvm-14-dev locales postgresql-14 postgresql-client-14 \
       postgresql-client-common postgresql-common postgresql-plpython3-14 \
       postgresql-server-dev-14 \
       python3 python3-matplotlib python3-psycopg2 sensible-utils ssl-cert \
       tzdata zlib1g \
    && locale-gen C.UTF-8 \
    && rm -rf /var/lib/apt/lists/*

ARG HOST_UID=1000
ARG HOST_GID=1000
RUN groupadd --gid "${HOST_GID}" experiment \
    && useradd --uid "${HOST_UID}" --gid "${HOST_GID}" --create-home experiment

WORKDIR /workspace
