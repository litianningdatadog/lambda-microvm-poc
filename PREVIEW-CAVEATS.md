# Lambda MicroVM Preview — Caveats and Known Gaps

Running notes on places where the private-preview service behaves
differently from what its public schema declares. Intended for:

- **Operators** building tooling against `lambdamicrovms-2025-09-09.json`
  who need to know which fields/operations to rely on today vs. treat as
  aspirational.
- **AWS preview-team feedback** — each gap below is a concrete
  schema-vs-implementation divergence worth escalating.

Last checked against the preview service on **2026-04-24**.

---

## Caveat: Cannot retrieve MicroVM image snapshot size via the control-plane API

**Status:** the `lambda-microvms` preview service declares fields and
operations in its public schema (`lambdamicrovms-2025-09-09.json`) that
are not fully implemented. There is currently **no reliable way to
query the Firecracker snapshot size of a MicroVM image through the AWS
API.**

### Symptom

Given a successfully-built MicroVM image ARN, calling
`describe-micro-vm-image-build` returns a response **missing
`snapshotSizeBytes`** even when `buildState` is `SUCCESSFUL`:

```json
{
  "microVMImageArn": "arn:aws:lambda:us-east-2:…:microvm-image:…",
  "microVMImageVersion": "1.0",
  "buildId": "47c24cbe-dfd2-4908-bb95-25f1525cb98e",
  "buildState": "SUCCESSFUL",
  "architecture": "ARM_64",
  "chipset": "GRAVITON",
  "chipsetGeneration": "3",
  "creationDate": 1777051630380
}
```

`snapshotSizeBytes` is absent from the response entirely (not `null`,
not `0` — the key isn't there).

### Schema-vs-implementation gaps observed

Three related findings in the same schema area, all pointing at the
same preview-maturity pattern:

| Schema declares | Service behavior | Severity |
|-----------------|------------------|----------|
| `DescribeMicroVMImageBuildOutput.snapshotSizeBytes` (optional `PositiveLong`) | Absent from responses for `SUCCESSFUL` builds | ⚠️ Blocks image-sizing use cases |
| `DescribeMicroVMImageBuildOutput.encryptedDataKey` (**required** `NonBlankString`) | Absent from responses | ⚠️ Service non-conformant with its own schema |
| `GetMicroVMImageBuild` operation (schema-identical to `DescribeMicroVMImageBuild`) | Returns `UnknownOperationException` | ⚠️ Operation declared but not implemented |

### Impact

Any tooling built from the schema that expects:

- to read snapshot sizes for capacity/cost analysis,
- to cross-reference `encryptedDataKey` for key-management workflows, or
- to use `get-*`-style operations interchangeably with `describe-*`-style

…will break on the current preview.

### Workarounds

Local approximations that work today, in rough order of proximity to
the real snapshot size:

1. **`docker image inspect` on a locally-built version of the image** (closest):

   ```bash
   docker buildx build --platform=linux/arm64 -t <name> <app-dir>/
   docker image inspect <name> --format '{{.Size}}' \
     | numfmt --to=iec-i --suffix=B
   ```

   Reflects summed rootfs layer size after extraction. Excludes
   MicroVM-specific kernel/init overhead and memory-at-snapshot-time,
   but close enough for "is my image too big?" decisions.

2. **`du` on the zip artifact** (quick but loose):

   ```bash
   du -h <app-dir>.*.zip
   ```

   Upload size only — compressed, no runtime state. Useful as a lower
   bound.

3. **`df -h /` from inside the running MicroVM** (live view):

   Requires shell access (`shellEnabled=true` in the launch config).

   ```bash
   ctr task exec -t --exec-id shell <container-id> /bin/sh -c 'df -h /'
   ```

   Reflects actual rootfs occupancy on the running VM — closest to what
   a snapshot would serialize, but only obtainable after launch, not at
   image-create time.

### Per-MicroVM runtime (post-suspend) snapshot size

Also **not exposed by the API.** `LaunchMicroVMResponse`,
`MicroVMSummary`, and the various per-instance describe paths report
`resourceSpec` (vCPU/memory allocation *ceiling*) but not actual usage.

If an approximation is needed, `system.mem.used{host:<microVmId>}` in
Datadog (or the equivalent CloudWatch metric) just before `/suspend`
fires serves as a reasonable proxy — Firecracker's suspend serializes
guest RAM, so the last-emitted memory figure approximates the suspended
snapshot's size.

### Recommended feedback to the preview service team

Three concrete items to raise with AWS:

1. Either populate `snapshotSizeBytes` in
   `DescribeMicroVMImageBuildOutput` for `SUCCESSFUL` builds, or mark
   the field deprecated in the schema if it's not planned for this
   release.
2. Investigate why `encryptedDataKey` — declared `required` — is absent
   from responses. Either the schema is wrong or the service is.
3. Implement `GetMicroVMImageBuild` (currently returns
   `UnknownOperationException`) or remove it from the schema to avoid
   client-generation drift.

### Related files

- `get-image-size.sh` at the repo root implements the two-step query
  flow and handles the gap gracefully — exits with code 2 and dumps the
  full `describe-micro-vm-image-build` response if `snapshotSizeBytes`
  isn't populated, so you can see exactly which fields did arrive.
- `HOWTO-DATADOG-AGENT.md` cross-references this caveat under its
  "Operational restrictions" section.

---

<!-- Add future caveats below, one `##` section per distinct gap. -->
