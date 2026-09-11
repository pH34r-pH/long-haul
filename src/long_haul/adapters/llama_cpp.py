"""llama.cpp CLI adapter. Its flags stay wholly at this edge."""
from __future__ import annotations

import hashlib
import subprocess
import time
from pathlib import Path

from ..models import InferenceProfile
from ..runtime import (
    ExecutionRequest,
    ExecutionResult,
    FailureClass,
    ProfileValidation,
    RuntimeIdentity,
    Timing,
    ValidationState,
)


class LlamaCppAdapter:
    runtime_id = "llama.cpp"
    def __init__(self, binary: str | Path): self.binary = Path(binary)
    def identity(self) -> RuntimeIdentity:
        if not self.binary.is_file(): return RuntimeIdentity(runtime_id=self.runtime_id,binary_path=str(self.binary))
        version = subprocess.run([str(self.binary),"--version"],capture_output=True,text=True,check=False).stdout.strip()
        digest = hashlib.sha256(self.binary.read_bytes()).hexdigest()[:16]
        return RuntimeIdentity(runtime_id=self.runtime_id,version=version or None,build_id=digest,binary_path=str(self.binary),backends=["cpu"],capabilities={"resident":True})
    def validate(self, profile: InferenceProfile) -> ProfileValidation:
        runtime=self.identity()
        if not self.binary.is_file(): state,rationale=ValidationState.UNKNOWN,"llama.cpp binary is unavailable"
        elif profile.runtime_id != self.runtime_id: state,rationale=ValidationState.UNSUPPORTED,"profile names a different runtime"
        elif profile.strategy != "resident": state,rationale=ValidationState.UNSUPPORTED,"reference adapter only validates CPU resident strategy"
        elif not profile.options.get("model_path"): state,rationale=ValidationState.UNKNOWN,"profile has no adapter model_path option"
        elif not Path(str(profile.options["model_path"])).is_file(): state,rationale=ValidationState.UNSUPPORTED,"GGUF model artifact is missing"
        else: state,rationale=ValidationState.SUPPORTED,"CPU llama.cpp resident profile and GGUF are available"
        return ProfileValidation(runtime=runtime,profile_id=profile.id,artifact_key=profile.artifact.key,resources=profile.participating_resources,strategy=profile.strategy,options=profile.options,state=state,rationale=rationale,provenance="probe")
    def execute(self, request: ExecutionRequest) -> ExecutionResult:
        validation=self.validate(request.profile); runtime=validation.runtime
        if validation.state is not ValidationState.SUPPORTED:
            return ExecutionResult(request_id=request.request_id,runtime=runtime,success=False,error_class=FailureClass.RUNTIME,error_detail=validation.rationale)
        started=time.monotonic()
        command=[str(self.binary),"-m",str(request.profile.options["model_path"]),"-p",request.prompt,"-n",str(request.max_tokens),"--temp",str(request.temperature),"--no-warmup","--log-disable"]
        try:
            run=subprocess.run(command,capture_output=True,text=True,timeout=request.timeout_seconds,check=False)
        except subprocess.TimeoutExpired:
            return ExecutionResult(request_id=request.request_id,runtime=runtime,success=False,error_class=FailureClass.TIMEOUT,error_detail="llama.cpp timed out",timings=Timing(total_seconds=time.monotonic()-started))
        if run.returncode:
            return ExecutionResult(request_id=request.request_id,runtime=runtime,success=False,error_class=FailureClass.EXECUTION,error_detail=run.stderr[-1000:],raw_exit_code=run.returncode,timings=Timing(total_seconds=time.monotonic()-started))
        output=run.stdout.removeprefix(request.prompt).strip()
        return ExecutionResult(request_id=request.request_id,runtime=runtime,success=True,output=output,timings=Timing(total_seconds=time.monotonic()-started))
