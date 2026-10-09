# PostgreSQL 16 (Alpine, identisch zum bisherigen Image) + pgvector.
# Gleiche Basis/libc wie vorher, damit das bestehende Datenvolume ohne
# Dump/Restore weiterverwendet werden kann. Ohne --pull bauen, sonst kann
# der Tag auf eine neuere Minor-Version zeigen.
FROM postgres:16-alpine

ARG PGVECTOR_VERSION=v0.8.0

RUN apk add --no-cache --virtual .build-deps git build-base \
 && git clone --branch "${PGVECTOR_VERSION}" --depth 1 https://github.com/pgvector/pgvector.git /tmp/pgvector \
 && cd /tmp/pgvector \
 && make with_llvm=no OPTFLAGS="" \
 && make with_llvm=no install \
 && cd / && rm -rf /tmp/pgvector \
 && apk del .build-deps
