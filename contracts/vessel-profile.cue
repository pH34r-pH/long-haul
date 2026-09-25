package contracts

// Companion metadata to models.Vessel, not a replacement resource/topology model.
#VesselProfile: {
	schemaVersion: 1
	vesselId: string & !=""
	// Existing Vessel.class_name remains orthogonal to provenance class.
	formClass: "station" | "ship" | "light_craft"
	provenanceClass: "custom" | "published-consumer" | "published-devboard" | =~"^extension\\.[a-z0-9.-]+$"
	form: "physical" | "virtual"
	platform: #Platform
	roles: [...string]
	contract: {
		identity: true
		inventory: true
		execution: true
		lifecycle: true
		diagnostics: true
		consent: true
		telemetry: true
	}
	vendorSources: [...{
		url: string & =~"^https://"
		revision: string & !=""
	}]
	capabilities: [string]: {
		state: "unknown" | "declared" | "observed" | "qualified" | "unsupported" | "denied"
		supported?: bool
		evidence?: {
			scope: "fixture" | "physical"
			reference: string & !=""
			environmentRevision: string & !=""
		}
	}
	quirks: [...{
		id: string & !=""
		appliesTo: [string]: string
		reference: string & =~"^https://"
		workaround: string & !=""
		retest: string & !=""
	}]
}
// Describes necessary evidence only; does not authorize execution or replace
// structural/preflight/execution/benchmark policy, freshness or consent checks.
#Admission: {
	profile: #VesselProfile
	requiredCapabilities: [...string]
	for capability in requiredCapabilities {
		profile: capabilities: "\(capability)": {
			state: "qualified"
			supported: true
			evidence: scope: "physical"
		}
	}
}
