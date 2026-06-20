# Session Log — 2026-06-20 — LTX-2.3 / ComfyUI Manual Validation on blackwell-node-01

## Goal
Manually validate an LTX-2.3 text-to-video pipeline on the RTX 5090 host
(`blackwell-node-01`, `192.168.50.51`, user `rich-rob`) as a precursor to
later building idle-guard + Vast.ai dashboard integration for automated
short video generation when the GPU is idle (not rented out).

Full step-by-step ComfyUI/LTX install detail, working node graphs, and
sample workflow JSONs now live in the dedicated
**`comfyui-ltx-rtx5090`** repo. This log covers the **host/infra-level**
findings relevant to `bootstrap-agent` and Vast.ai operations generally.

---

## 1. kaalia_docker_shim is still broken — confirmed again, podman is the workaround

See `KAALIA_INCIDENT_RUNBOOK.md` for prior history. Today's session hit the
exact same failure mode again: every `docker run` on this host fails with

```
OCI runtime create failed ... exit status 101
```

Diagnosed via process inspection, `kaalia` restart, `containerd` restart,
and the existing update script — none fixed it. Root cause still unknown
(Vast.ai's shim is closed-source). This is now the second time we've hit
this exact issue; treat it as a standing characteristic of this host, not
a one-off.

**Confirmed workaround: use `podman` instead of `docker`.** Podman talks
to `runc` directly, bypassing `kaalia`/`containerd` entirely. Full GPU
passthrough works via:

```bash
podman run -d --device nvidia.com/gpu=all ...
```

No CDI config changes were needed beyond what's already on this host —
`nvidia.com/gpu=all` resolved correctly out of the box.

**Recommendation for bootstrap-agent tooling:** any new containerized
workload on this host should default to podman, not docker, until kaalia
is fixed upstream by Vast.ai (no timeline expected — this has now
persisted across at least two incidents).

### Renter-instance VRAM note
We kept a personal renter instance idling on a second Vast.ai account
during this session, to keep the GPU "occupied"/rented from Vast.ai's
perspective while we worked. Confirmed safe: an idle desktop container
does not hold a CUDA context, only OpenGL, so VRAM impact is negligible.
Useful pattern if we need to do host-level GPU work again without
releasing the rental.

---

## 2. Podman UID mapping + CUDA compat — two host-specific gotchas

**UID mapping:** custom node installs into a container's filesystem from
the host user's shell need `--userns=keep-id`, or file ownership inside
the container ends up wrong and breaks subsequent `pip3 install` /
`git` operations. Add this flag on every `podman run` going forward for
any workload that writes into bind-mounted host directories.

**CUDA driver version mismatch (Blackwell-specific):** the container
image we used (`docker.io/clasyc/comfyui:cuda13.1`) bundles its own
`/usr/local/cuda-13.1/compat/libcuda.so` at v590.44.01, which is *newer*
than this host's actual driver (580.142). This produces:

```
Error 804: forward compatibility was attempted on non supported HW
```

**Fix, required on every container launch on this host:**

```bash
-e LD_PRELOAD="/lib/x86_64-linux-gnu/libcuda.so.1"
```

This forces the container to use the host's real driver lib ahead of the
image's bundled (and incompatible) compat lib. This is a host-driver-
version-specific issue — if the host driver is ever upgraded to ≥590.44,
re-test whether this flag is still required.

---

## 3. Podman state does not persist across container recreate

Learned the hard way mid-session: changes made via `podman exec` into a
running container (e.g. `git checkout`, `pip3 install`) live only in that
specific container instance's writable layer. Doing `podman rm` + a fresh
`podman run` from the original base image **throws all of that away** —
you're back to square one.

**Fix pattern going forward:** once a container reaches a good state via
exec-applied fixes, immediately commit it to a new local image before
doing anything else that might require a recreate (changing launch flags,
restarting cleanly, etc.):

```bash
podman commit <container-name> <new-image-name>:latest
```

Then base all subsequent `podman run` calls on
`localhost/<new-image-name>:latest`, not the original upstream image tag.
We lost ~15 minutes re-doing a ComfyUI version upgrade tonight because we
hadn't committed before recreating the container to change a launch flag.

---

## 4. RTX 5090 / Blackwell (sm_120) hardware-specific bugs encountered

Two distinct, externally-documented Blackwell compatibility bugs hit us
tonight. Both are upstream PyTorch/cuDNN issues, not something fixable
via our configuration:

**a) conv3d kernel segfault.** Any 3D convolution op (specifically hit
this in a VAE decoder's causal conv3d path) can hard-segfault
(`SIGSEGV`, container exits `139`) on Blackwell GPUs. Root cause:
cuDNN currently only selects sm80 (Ampere-era) conv kernels on Blackwell
hardware even when proper sm100/sm120 kernels should be available — a
confirmed open NVIDIA/cuDNN bug, not specific to any one application.

Neither `--novram` nor `--disable-dynamic-vram` fixed this — ruling out
VRAM-pressure as the cause. **The only working fix found:** force the
specific operation (in our case, VAE decode) onto CPU instead of GPU.
ComfyUI exposes `--cpu-vae` for this; other applications hitting the same
underlying bug will need an equivalent CPU-fallback flag or manual device
override for whichever op is hitting conv3d.

**b) NVFP4 quantization not yet properly supported.** Native Blackwell
NVFP4 acceleration is in early rollout across the ecosystem and isn't
reliable yet (confirmed via research, not directly hit tonight since we
used GGUF quantization instead). Worth knowing if future work considers
NVFP4 checkpoints on this hardware — expect rough edges.

**General takeaway for this host:** when something crashes with no clean
Python traceback (just a `Fatal Python error: Segmentation fault` and a
bare extension-module dump), check the exact line number in the stack —
on this host, treat any crash inside a conv3d-related code path as a
likely Blackwell/cuDNN compatibility issue first, before assuming OOM or
misconfiguration.

---

## 5. Outcome

Manual LTX-2.3 text-to-video generation validated end-to-end on
`blackwell-node-01` via podman, producing a real output MP4. Quality
tuning (CFG, sampler, resolution, quantization level) is ongoing — see
`comfyui-ltx-rtx5090` repo for current best-known settings and remaining
open questions.

**Not yet started:** idle-guard watchdog (poll GPU process state, gate
generation jobs when a Vast.ai renter is active), dashboard → ComfyUI
REST API integration. Both deferred to a future session, to be tracked in
`bootstrap-agent` once scoped.

## Cross-references
- `KAALIA_INCIDENT_RUNBOOK.md` — prior kaalia shim failure history
- `comfyui-ltx-rtx5090` repo — full ComfyUI/LTX install steps, node
  graphs, working workflow JSONs, model file inventory
