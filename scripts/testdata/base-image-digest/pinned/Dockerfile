# Every shape that must count as pinned: a literal digest, a digest threaded through ARG
# defaults, a stage built on an earlier stage, and scratch.
ARG BUILDER_IMAGE_TAG=1.26-alpine3.24
ARG BUILDER_IMAGE_DIGEST=sha256:1111111111111111111111111111111111111111111111111111111111111111
FROM --platform=$BUILDPLATFORM public.ecr.aws/docker/library/golang:${BUILDER_IMAGE_TAG}@${BUILDER_IMAGE_DIGEST} AS builder
FROM public.ecr.aws/docker/library/alpine:3.24@sha256:2222222222222222222222222222222222222222222222222222222222222222 AS runtime-base
FROM runtime-base
FROM scratch
