# Independent Blender worker validation

This candidate keeps the Railway service as a durable MCP control plane. The Blender worker polls it over outbound HTTPS. It needs no public port. Its credential differs from the MCP URL and artifact-signing key.

## Staging setup

The acceptance service runs in a separate Railway project and a separately named staging environment. It is pinned to the validation commit. The original Railway project, MCP address and Git main branch retain their original deployment.

The control plane uses `BLENDER_V2_WORKER_KEY` to turn on relay mode. Grant that credential only to the registered worker and store it outside Git. The control process stages one claimed job, issues a 45-second renewable lease, and accepts an output archive only from the holder of that lease. It validates archive paths, entry types, output names, sizes and expected file signatures before updating the catalog. There is one active worker job per control service. Versioned state remains on the control volume.

The worker requires Blender 5.3.0 and the matching reviewed recipe source, Bubblewrap, FFmpeg, Open CASCADE for STEP, and a host that allows unprivileged user, PID and network namespaces. Start it with `python -m v2.remote_worker` and the environment variables below. Do not register a host whose preflight fails.

| Variable | Purpose |
| --- | --- |
| `BLENDER_REMOTE_CONTROL` | Staging control-plane HTTPS origin. |
| `BLENDER_V2_WORKER_KEY` | Worker secret issued by the operator. |
| `BLENDER_REMOTE_WORKSPACE` | Private worker scratch directory. |
| `BLENDER_V2_BINARY` | Checked Blender 5.3 binary. |
| `BLENDER_V2_MEMORY_MIB`, `BLENDER_V2_JOB_DISK_MIB` | Limits sized to measured host capacity and control-plane upload limits. |

Preflight tests network namespaces, absence of service environment variables, inaccessible files outside the mount, read-only inputs and the kernel resource ceilings. All production Blender commands continue to run through the same Bubblewrap worker. Loss of the lease or cancellation stops work and prevents delayed publication. Deploy or restart recovery marks in-flight tasks interrupted and leaves old immutable versions available.

The test Linux machine is a temporary Codex environment. Its results establish compatibility and connectivity for this topology. Registration in a service supervisor, node longevity, scheduled storage backups, longer shots and production rollout need separate verification. No payment method or paid service was provisioned.

## Recipe capability review

`product_showcase` supports framing, camera motion, neutral materials and presentation lights. It is suitable for establishing reference views of real meshes. `edit_objects` supports local transforms and rotation keyframes, parenting, simple follow constraints, object-specific materials, lamps, camera settings and keyed camera cuts. The input validation rejects missing targets and retains preceding versions.

The shipped recipes do not infer assembly identities. For a six-product mechanical release, use verified part mappings, parent transforms, pivot and axis, travel or angular range, timing, and contact and end conditions. The present operations cover simple commanded translation or rotation once those values are supplied. Pivot calibration, specialized mating constraints, verified collision tolerances and imported mechanism validation need project-specific review.

For more complex jobs, register private immutable script packages with a recipe ID, Git revision or package SHA-256, supported Blender version, versioned parameter schema, input/output contract, reviewer and approval time. Install only reviewed packages on the worker. Expose recipe ID and typed parameters through `run_script`. The job manifest should record the exact package hash and project versions. Advance to an independently reviewed revision only after isolated smoke tests and a deterministic fixture run.

## Observed staging results

- Pinned code `68456db63f63706b7fb84e9ec88d4fba24d28582` ran in the separate Railway staging project. Health and actual MCP calls were verified via HTTPS.
- The external worker reported Blender 5.3.0. Unauthenticated claim and result submission were denied. The actual isolated Workbench probe completed and returned a PNG.
- A private industrial model passed signed upload and hash validation, import, structure inspection, the approved showcase recipe, a selective material revision, and image review. Verified output was 48 frames and a two-second H.264 MP4 at 854 × 480. The editable Blender file and previous versions remained accessible.
- A 24-frame 1080p Eevee run would have exceeded the initial timeout. It was cancelled through the API after progress reached 6/24. The subsequent bounded 1080p technical sample produced a two-second H.264 MP4 with four rendered frames at 2 fps. The worker measured 63.26 seconds elapsed, with a sampled process-group RSS peak of 2.52 GiB. This establishes 1080p CPU output, with normal-frame-rate throughput pending.
- The original production service processed a separate legacy preview job successfully. No production source or deployment was switched. Actual original-chat V2 calls are still a release gate.
- Staging persisted the original asset and editable project across an actual Railway control-plane restart. An active render became `interrupted` on startup and completed when retried. Running cancellation was acknowledged by the worker after process termination. A deterministic script error remained explicit when retried.
- Candidate is held before formal release. The acceptance node is temporary, the six production assemblies still require confirmed motion data, and the original ChatGPT thread has not been switched or exercised with V2.
