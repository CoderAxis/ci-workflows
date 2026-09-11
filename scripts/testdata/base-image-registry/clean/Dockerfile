# Every base image names a registry, so nothing here is pulled from Docker Hub.
FROM --platform=linux/amd64 public.ecr.aws/docker/library/node:20-alpine AS builder
WORKDIR /src

# A later stage built on an earlier one pulls nothing and must not be flagged.
FROM builder AS unit-tests
RUN echo tests

# An ARG-driven reference is resolved at build time; DS-0003 reviews those defaults.
ARG RUNTIME_IMAGE=public.ecr.aws/docker/library/nginx:1.27-alpine
FROM ${RUNTIME_IMAGE}
