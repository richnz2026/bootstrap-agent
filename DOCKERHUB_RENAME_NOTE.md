# Docker Hub Repo Rename — Tag & Push Pattern

Docker Hub has no in-place "rename repo" via CLI. To rename, push the
same image under a new repo name (old repo stays untouched on Docker Hub
unless separately deleted via the web UI Settings page).

## Pattern

```bash
docker pull <namespace>/<old-repo>:<tag>      # only needed if not already local
docker tag <namespace>/<old-repo>:<tag> <namespace>/<new-repo>:<tag>
docker push <namespace>/<new-repo>:<tag>
```

## Worked example — 2026-06-20, blackwell-node-01

Renamed `itsthateasymate/pbro-worker` → `itsthateasymate/psis-worker`:

```bash
docker pull itsthateasymate/pbro-worker:latest
docker tag itsthateasymate/pbro-worker:latest itsthateasymate/psis-worker:latest
docker push itsthateasymate/psis-worker:latest
```

Result: pushed cleanly. All layers were `Mounted from itsthateasymate/pbro-worker`
(Docker Hub recognized identical layers already present in the same
namespace and didn't re-upload them — fast push, no real data transfer).

Final digest: `sha256:703cc5527b8f6f03f5c9842cde8a730baafeaa97375dee604482dedfc3304e02`

`itsthateasymate/psis-worker:latest` now exists as a new repo on Docker
Hub. `itsthateasymate/pbro-worker:latest` still exists unchanged — delete
it manually via Docker Hub web UI if a true rename (not a copy) is
wanted.

## Notes
- Requires `docker login` as a user with push access to the target
  namespace before the push step.
- This host (`blackwell-node-01`) runs `podman` for container *execution*
  due to the kaalia shim bug (see `KAALIA_INCIDENT_RUNBOOK.md`), but
  `docker pull`/`tag`/`push` (registry operations, not container runtime)
  worked fine directly — these are separate code paths from `docker run`.
