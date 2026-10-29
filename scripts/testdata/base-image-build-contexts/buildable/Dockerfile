# Built for real by ci.yaml on a GitHub-hosted runner, with the redirect frontend-image-ci.yaml
# applies, to prove BuildKit still matches each named context to the FROM it replaces. Small on
# purpose (alpine), and the same shapes as a frontend image: a builder on the build platform
# named through a global ARG, a test stage built on it, and a runtime pinned by tag AND digest
# for the target platform.
#
# The digest is alpine:3.24's image index as both registries served it on 2026-10-29. Docker Hub
# keeps an official image's digests pullable after the tag moves on, so it does not need
# refreshing; if it ever stops resolving, take the current one from
# `docker buildx imagetools inspect docker.io/library/alpine:3.24`.
ARG ALPINE_TAG=3.24
ARG TARGETARCH=arm64

FROM --platform=$BUILDPLATFORM public.ecr.aws/docker/library/alpine:${ALPINE_TAG} AS builder
RUN echo built > /out

FROM builder AS unit-tests
RUN test "$(cat /out)" = built

FROM --platform=linux/${TARGETARCH} public.ecr.aws/docker/library/alpine:3.24@sha256:be2656061b536c1883c4034790e21d0a09c5aaaf51968b89368a3c0371ea2c9a
COPY --from=builder /out /out
