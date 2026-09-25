package contracts

#Path: string & =~"^[A-Za-z0-9_.][A-Za-z0-9_./-]*$"
#Platform: {
	os: "linux" | "android" | "windows" | "darwin"
	arch: "amd64" | "arm64"
	abi: "glibc" | "bionic" | "msvc" | "darwin"
	if os == "android" { abi: "bionic" }
	if os == "linux" { abi: "glibc" }
	if os == "windows" { abi: "msvc" }
	if os == "darwin" { abi: "darwin" }
}
#Manifest: {
	path: #Path
	kind: "python-project" | "node-project" | "rust-project" | "lock" | "toolchain" | "workflow" | "build" | "documentation"
}
#Need: {
	sources: [#Path, ...#Path]
	notes?: string
}
#Profile: {
	phases: ["build" | "test" | "package" | "runtime" | "deploy" | "inventory", ...("build" | "test" | "package" | "runtime" | "deploy" | "inventory")]
	platforms: [#Platform, ...#Platform]
	tools: [string]: #Need
	// Documentation only: the validator never executes a source entrypoint.
	entrypoints: [...string]
	notes?: string
}
#Requirements: {
	schemaVersion: 1
	repository: string & =~"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$"
	manifests: [#Manifest, ...#Manifest]
	profiles: [string]: #Profile
}
