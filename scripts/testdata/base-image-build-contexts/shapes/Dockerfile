# syntax=docker/dockerfile:1
# Every shape base-image-build-contexts.py must read the way BuildKit does. expected.txt
# beside this file is the complete output; a shape that must NOT produce a line says why.

ARG NODE_TAG=22-alpine
# An image named entirely by a global ARG, built from an earlier ARG.
ARG GO_REPO=public.ecr.aws/docker/library/golang
ARG GO_IMAGE=${GO_REPO}:1.26-alpine3.24
# Overridden by --build-arg ALPINE_TAG=3.23 in the test, as the build would be.
ARG ALPINE_TAG=3.24
ARG PYTHON_TAG

FROM --platform=$BUILDPLATFORM public.ecr.aws/docker/library/node:${NODE_TAG} AS Deps
FROM ${GO_IMAGE} AS go-builder
FROM public.ecr.aws/docker/library/alpine:${ALPINE_TAG}

# Unset ARG with a default in the substitution itself.
FROM public.ecr.aws/docker/library/python:${PYTHON_TAG:-3.13-slim}

# Tag and digest: the digest is kept, so the image stays pinned.
FROM public.ecr.aws/docker/library/redis:7@sha256:4f8d9a8bd0bd4b6c2d1f7e5c9f1b6a2e3d4c5b6a7f8e9d0c1b2a3f4e5d6c7b8a
# Digest only.
FROM public.ecr.aws/docker/library/busybox@sha256:1a2b3c4d5e6f708192a3b4c5d6e7f8091a2b3c4d5e6f708192a3b4c5d6e7f809
# No tag: BuildKit resolves :latest.
FROM public.ecr.aws/docker/library/debian

# Stage references and scratch: none of them is an image. BuildKit lowercases a stage's
# name, so the stage declared as Deps above is referenced as deps.
FROM deps AS again
FROM again
FROM scratch

# Continuation lines are one instruction.
FROM \
    --platform=linux/amd64 \
    public.ecr.aws/docker/library/postgres:18-alpine \
    AS db

# An ARG declared after the first FROM is stage-scoped and cannot reach a FROM, so this
# FROM still reads the global NODE_TAG.
ARG NODE_TAG=20-alpine
FROM public.ecr.aws/docker/library/node:${NODE_TAG}

# Images pulled by COPY --from and RUN --mount=from are redirected too; stages are not.
COPY --from=public.ecr.aws/docker/library/caddy:2-alpine /usr/bin/caddy /usr/bin/caddy
COPY --from=go-builder /out /out
RUN --mount=type=bind,from=public.ecr.aws/docker/library/rust:1-alpine,source=/usr/local/cargo,target=/cargo \
    --mount=type=bind,from=deps,source=/,target=/deps true

# A heredoc body is file content, not instructions.
COPY <<EOT /Dockerfile.inner
FROM public.ecr.aws/docker/library/mysql:8
EOT

# Not Docker Hub mirrors: another ECR Public publisher, an explicit Docker Hub reference,
# and a private registry. All pulled exactly as written.
FROM public.ecr.aws/nginx/nginx-unprivileged:1.27-alpine
FROM docker.io/library/nginx:1.27-alpine
FROM 063088900996.dkr.ecr.us-east-1.amazonaws.com/inboxxhq/base:1
