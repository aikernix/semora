# Compiled private delivery

This opt-in build raises the cost of extracting Semora implementation code. It is
not encryption, DRM, or protection against copying, patching, or reverse engineering.
No heartbeat, license check, or change to query behavior is introduced.
The toolchain is free/open source; it does not use Nuitka Commercial or a paid
constant-protection plugin. Prompts are compiled with the implementation, not
delivered as separate prompt source files, but can still be extracted as runtime
strings. No claim of static string encryption is made.

## Contents

- API, Agent, contracts, telemetry and Executor implementation modules are Cython
  native extensions. The compiler and generated C stay in the private build stage.
- Generated `__init__.py` loaders only import extensions and contain no original
  business logic. The runtime verifier allows only those exact loaders and native
  modules inside the owned packages. Third-party Python dependencies retain their
  normal files and license obligations.
- Python reflection/annotations and tool docstrings are retained for FastAPI,
  Pydantic and LangChain. Prompts, SQL text and other runtime strings remain
  extractable from binaries or memory; they are not secrets.
- The Go ontology binary is stripped and built with `-trimpath`.
- Web source maps are explicitly disabled. Browser-delivered JavaScript remains
  inspectable and modifiable.
- Database migrations remain readable deployment assets. Credentials, customer
  data, Git history and local artifacts must never be included in delivery images.

## Build and verify (development machine or isolated CI)

```sh
docker compose -f docker-compose.yml -f deploy/compose.compiled.yml build
docker run --rm --entrypoint python semora-api:compiled-v1 /usr/local/bin/semora-verify-payload /app
docker run --rm --entrypoint python semora-executor:compiled-v1 /usr/local/bin/semora-verify-payload /app
```

Build for each target OS/CPU/Python ABI. Do not copy Windows extensions into Linux
images. The compiler and runtime use the same Python base stage.

The private `Compiled delivery validation` workflow builds Linux images, verifies
native imports, tests HTTP/OpenAPI and executes API regressions using only compiled
implementations. It does not publish images or alter a live deployment.

The LangGraph migration uses an import entrypoint because CPython `python -m`
cannot execute a native extension module directly. The development entrypoint is
unchanged.

## Delivery boundary

Give the partner a separate deployment repository with image-only Compose,
configuration examples and notices; do not give access to this source repository,
compiler stages, CI caches or build contexts. Publish only the verified final
runtime image to a private registry, preferably pinned by digest. Never derive the
final protected runtime from an existing source-containing application image:
deleting files in a later layer does not remove them from earlier layers.

Existing source deployments are not retroactively protected by this change. This
branch does not replace the live release or remove existing source checkouts.
Compiled artifacts do not stop a recipient from copying a downloaded image.
