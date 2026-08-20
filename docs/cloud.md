# Self-Hosted Cloud Deployment

The container processes data entirely inside the operator's infrastructure.
It has no inbound listener and sends no telemetry.

## Modes

- `local SESSION_FOLDER`: read a mounted session and write MCAPs locally.
- `s3 --input-uri ... --output-uri ...`: download one session prefix, convert,
  upload verified MCAPs, and write a JSON job report.

Use an IAM workload role with `s3:ListBucket`/`s3:GetObject` on the input and
`s3:PutObject` on the output. Generic AWS Batch and Kubernetes examples are
under [`packaging/cloud/examples`](../packaging/cloud/examples).

Each Batch job should process one session prefix. Parallelize across sessions;
within a session, `--video-workers` controls camera conversion concurrency.

For local mounts, preserve the timestamped session directory name inside the
container. Mount its parent directory and pass the full child path; mounting
only the files at a generic path such as `/work/input` removes the UTC clock
anchor from the path and validation will correctly reject the conversion.
The image runs as non-root UID/GID `10001`; local output bind mounts must be
writable by that identity. Workload-role S3 mode needs no host output mount.
