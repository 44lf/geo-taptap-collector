# Windows TapTap end-to-end Spike kit

This directory contains the operational scripts used to prove:

```text
Windows collector -> DEV MinIO inbox -> GEO dry-run/import -> DEV MySQL + DEV MinIO
```

The collector repository and GEO repository must be sibling directories:

```text
E:\
├── geo\
└── geo-taptap-collector\
```

Over RDP drive redirection the same layout appears under `\\tsclient\E\`.

## Runtime boundary

- Run DEV scripts on `172.25.20.57`.
- Run collector/upload scripts on `172.25.25.102`.
- Never send credential transfer files or `.last.log` files through chat.
- `mc*.exe`, the Python installer, the wheelhouse, logs, and `*.transfer.*`
  files are local deployment artifacts and are ignored by Git.
- The restricted uploader credential is installed at
  `C:\ProgramData\GeoTapTap\minio-uploader.env` on the collector.
- The isolated DEV Python is installed at
  `C:\ProgramData\GeoTapTap\python312\python.exe`.

## Proven manual sequence

On DEV (`172.25.20.57`):

1. `create-minio-uploader.cmd`
2. `export-uploader-credentials.cmd`

On the collector (`172.25.25.102`):

3. `install-and-test-minio-upload.cmd`

On DEV:

4. `install-dev-import-environment.cmd`
5. `export-dev-taptap-target.cmd`

On the collector:

6. `collect-and-upload-dev-bundle.cmd`

On DEV:

7. `import-latest-taptap-dev.cmd`

The consumer verifies the archive SHA-256, bundle manifest and all files,
runs a dry-run, imports into `geo_dev`, verifies the game row and MinIO image
objects, then moves the ready marker to `processed/`.

## Diagnostic helpers

- `inspect-dev-import-runtime.cmd`
- `check-dev-import-dependencies.cmd`
- `inspect-dev-game-databases.cmd`

## Before production scheduling

Replace DEV-local root credentials with a least-privilege importer account,
add retry/alerting and retention, and run a multi-day low-frequency stability
test for the collector IP.
