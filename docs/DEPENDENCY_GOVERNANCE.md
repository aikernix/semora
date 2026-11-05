# Dependency and license governance

Semora treats dependency provenance as a release gate rather than documentation-only work.

- Every direct Python dependency must use an exact `==` version. Test-only packages live in a separate development requirements file and are not copied into runtime images.
- Node dependencies are reproducibly installed with `npm ci`; each direct package must resolve to a version and integrity hash in `package-lock.json`.
- Go modules and container base images must be versioned. `latest` and unversioned base images are rejected.
- CI runs `pip-audit`, `npm audit`, and reachability-aware `govulncheck`. A known runtime vulnerability fails the build.
- CI emits CycloneDX SBOMs for every Python service, the Web application, and Ontology Core. License reports reject unknown, proprietary, strong-copyleft, SSPL, BUSL, and Commons Clause dependencies.
- Every third-party GitHub Action is pinned to a full commit SHA; moving major-version tags are rejected by the same static gate.
- `THIRD_PARTY_NOTICES.md` records the reviewed upstream role and license. WeKnora and OpenBKN remain design references; unreviewed vendored source trees for either project are rejected.
- Bank releases should mirror approved packages and images into the bank artifact repository, sign images and SBOM attestations, and replace mutable Helm image tags with registry digests.

Run the local static gate with:

```bash
python scripts/verify_dependency_policy.py
```
