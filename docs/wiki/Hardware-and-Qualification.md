# Hardware and qualification

The public runtime is intentionally useful without access to the operator's physical fleet.

## Public hardware boundary

Long Haul consumes normalized records for vessels, resources, links, runtimes, and benchmark measurements. Machine-specific discovery code is an adapter around that boundary.

Fixtures are useful for deterministic scheduler and failure-mode tests, but are marked simulated. A fixture cannot be cited as evidence for real hardware capacity or network quality.

## Qualification

Physical qualification should answer concrete questions:

- what resources exist and are addressable;
- what runtime versions/capabilities are actually usable;
- how much memory and storage are available under the intended workload;
- what local and cross-vessel link characteristics were measured;
- what inference profile has been demonstrated;
- which revision/configuration produced the evidence.

The [hardware validation runbook](https://github.com/pH34r-pH/long-haul/blob/main/docs/hardware-validation.md) defines the public evidence expectations.

## Public/private split

The public project owns schemas, probes, adapters, qualification contracts, and tests. Private Fleet owns concrete vessel inventories, protected credentials, privileged network state, and promotion/rollback operations.

That split makes public pull-request code inspectable without granting it an automatic path to privileged hardware.
