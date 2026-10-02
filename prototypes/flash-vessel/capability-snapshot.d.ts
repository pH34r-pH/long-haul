export type CapabilityHint<T> =
  | { status: "available"; value: T }
  | { status: "unavailable" }
  | { status: "unknown" };

export type Visibility = "visible" | "hidden" | "unknown";

export interface WebGPUCapabilities {
  features: CapabilityHint<string[]>;
  limits: Record<string, CapabilityHint<number>>;
}

export interface CapabilitySnapshotV1 {
  schemaVersion: 1;
  flashInstanceId: string;
  visibility: Visibility;
  capabilities: {
    logicalProcessors: CapabilityHint<number>;
    deviceMemoryGiB: CapabilityHint<number>;
    wasmSimd: CapabilityHint<true>;
    sharedArrayBuffer: CapabilityHint<true>;
    webGpu: CapabilityHint<WebGPUCapabilities>;
  };
}
