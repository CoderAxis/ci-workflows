# Tag-only pins of images on the baseline: debt, never new.
ARG BUILDER_IMAGE_TAG=1.26-alpine3.24
ARG RUNTIME_IMAGE_TAG=3.24
FROM --platform=$BUILDPLATFORM public.ecr.aws/docker/library/golang:${BUILDER_IMAGE_TAG} AS builder
FROM public.ecr.aws/docker/library/alpine:${RUNTIME_IMAGE_TAG}
