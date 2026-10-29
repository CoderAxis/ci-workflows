# The shape of the frontend images frontend-image-ci.yaml builds (inboxxhq-web-app,
# inboxxhq-web-console): global ARGs, a builder stage on the build platform, a unit-test
# stage built on the builder, and an nginx runtime for the target platform.
ARG TARGETARCH=arm64
ARG CORE_REF=main

FROM --platform=$BUILDPLATFORM public.ecr.aws/docker/library/node:22-alpine AS builder
ARG CORE_REF
WORKDIR /app
RUN --mount=type=secret,id=gh_token \
    echo "clone ${CORE_REF}"

# A stage built on an earlier stage pulls nothing.
FROM builder AS unit-tests
RUN echo tests

FROM --platform=linux/${TARGETARCH} public.ecr.aws/docker/library/nginx:1.27-alpine
COPY --from=builder /app/dist /usr/share/nginx/html
