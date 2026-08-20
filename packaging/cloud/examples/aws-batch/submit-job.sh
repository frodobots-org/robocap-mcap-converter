#!/usr/bin/env bash
set -euo pipefail

: "${JOB_QUEUE:?Set JOB_QUEUE}"
: "${INPUT_S3_URI:?Set INPUT_S3_URI}"
: "${OUTPUT_S3_URI:?Set OUTPUT_S3_URI}"

aws batch submit-job \
  --job-name "robocap-mcap-$(date +%Y%m%d-%H%M%S)" \
  --job-queue "$JOB_QUEUE" \
  --job-definition robocap-mcap-converter-0-1-0 \
  --parameters \
input_uri="$INPUT_S3_URI",output_uri="$OUTPUT_S3_URI",video_workers="${VIDEO_WORKERS:-4}"
