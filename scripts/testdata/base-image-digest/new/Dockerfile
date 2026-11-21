# A base image that is not on the baseline, pinned by tag only, and a digest ARG with no
# default (the file alone does not prove that pin).
ARG RUNTIME_IMAGE_DIGEST
FROM public.ecr.aws/docker/library/redis:7.4-alpine AS cache
FROM public.ecr.aws/docker/library/alpine:3.24@${RUNTIME_IMAGE_DIGEST}
