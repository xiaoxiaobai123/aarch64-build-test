# aarch64-build-test

Build a PyInstaller-packaged Python "hello world" for **aarch64** targeting
an **RK3568** running **Debian bookworm**.

The build runs inside a `debian:bookworm` arm64 container on GitHub's
`ubuntu-latest` runner via QEMU, so the produced binary is linked against
the glibc shipped with Debian bookworm and should run on an RK3568 bookworm
rootfs without further adjustment.

## Files

- `hello.py` — the script that gets packaged.
- `.github/workflows/build-aarch64.yml` — the CI workflow.

## Running locally

The workflow runs automatically on push. You can also trigger it manually
via **Actions → Build aarch64 binary → Run workflow**.

The resulting binary is uploaded as the `hello-aarch64-bookworm` artifact.

## Deploying to the RK3568

```sh
scp hello user@rk3568:/tmp/
ssh user@rk3568 /tmp/hello
```
