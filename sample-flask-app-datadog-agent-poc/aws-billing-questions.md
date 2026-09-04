# Lambda MicroVMs — Billing Model Questions for AWS

Draft of questions to send to the AWS Lambda MicroVMs team to clarify the pay-per-use pricing model referenced in the preview developer docs.

## Billed dimensions

1. Which resources are metered — vCPU-time, memory-GB-time, disk, network (ingress/egress), or some combination?
2. Is it a flat "MicroVM-second" rate based on the configured resource shape (e.g. 2 vCPU / 4 GB), or does it meter actual consumption inside those limits (like Lambda's GB-second model)?
3. What is the billing granularity — per-second, per-millisecond, or something coarser? Is there a minimum duration per launch?

## State-dependent pricing

4. How do the three lifecycle states bill?
   - **Active** (serving traffic)
   - **Idle** (autoResume on, within `maxIdleDurationSeconds`)
   - **Suspended** (within `suspendedDurationSeconds`)

   Does suspended time incur any charge, or only the snapshot storage underneath it?
5. Is there a charge for the in-place resume operation itself, or only for the runtime after resume?

## Storage and images

6. Is there a per-GB-month charge for MicroVM Image storage (the Firecracker snapshots)? Does it scale with image size or count?
7. Are build-time costs (the snapshot creation run triggered by `create-micro-vm-image`) billed separately, similar to CodeBuild minutes?
8. Are the zip artifacts in S3 and the CloudWatch log groups under `/aws/lambda/microvms/*` billed at standard S3/CloudWatch rates, or bundled?

## Network

9. How is network egress priced through `INTERNET_EGRESS` vs `ALL_INGRESS` connectors? Is traffic through the proxy endpoint (`*.arp.kepler-analytics.aws.dev`) metered as data transfer?
10. Is auth-token generation (`generate-micro-vm-auth-token`) a billable control-plane call?

## Preview and GA

11. What pricing (if any) applies during the private preview in us-east-2? Will preview usage retroactively be billed at GA rates, or is there a grace period / credit?
12. Do you have indicative GA pricing we can use for capacity planning, even if non-final?
13. How will pricing compare against standard Lambda and Fargate for the "long-running but bursty" workloads MicroVMs target?

## Quotas and overages

14. Are the published quotas (1000 concurrent MicroVMs, 1000 images per account) hard caps, or soft limits with overage pricing?

## Priority order (if trimming)

If asking all 14 at once is too much, lead with: **1, 2, 3, 4, 6, 11**. These cover the core metered dimensions, state-dependent billing, image storage, and preview vs GA pricing — enough to build a first-order cost model.