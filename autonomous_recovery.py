import json
import logging
import re
import shlex
import urllib.request
import asyncio
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Dict, Any

from pydantic import BaseModel
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse
import uvicorn

# Logging setup
logging.basicConfig(level=logging.INFO, format='[%(asctime)s] %(levelname)s - %(message)s')
logger = logging.getLogger("RecoveryPipeline")
BASE_DIR = Path(__file__).resolve().parent
DEFAULT_ADVISOR_SCRIPT = BASE_DIR / "recovery_advisor.py"
DEFAULT_SOURCE_MOUNT_DIR = Path("/tmp/recovery_source_mount")
DEFAULT_WORKSPACE_DIR = Path.home() / "workspace"

@dataclass
class RecoveryConfig:
    source_device: str
    output_directory: str
    llm_api_url: str
    llm_models: List[str] = field(default_factory=lambda: [
        "mistral-nemo:12b",
        "qwen2.5-coder:7b",
        "qwen2.5:7b",
        "llama3.2:latest",
    ])
    advisor_command: str = ""
    max_iterations: int = 10
    timeout_seconds: int = 3600
    source_mount_dir: str = str(DEFAULT_SOURCE_MOUNT_DIR)
    safe_helper: str = "/usr/local/sbin/recovery-safe"
    allowed_commands: List[str] = field(default_factory=lambda: ['lsblk', 'testdisk', 'photorec', 'smartctl', 'mount', 'mkdir', 'ls', 'cp', 'rsync', 'find', 'cat', 'grep', 'blkid', 'df', 'du', 'safe_prepare', 'safe_mount', 'safe_umount', 'safe_diagnose', 'safe_photorec_office_video'])
    manual_allowed_commands: List[str] = field(default_factory=lambda: ['lsblk', 'testdisk', 'photorec', 'smartctl', 'mount', 'mkdir', 'ls', 'cp', 'rsync', 'find', 'cat', 'grep', 'sudo', 'df', 'du', 'dmesg', 'blkid', 'gemini'])

class PolicyValidator:
    def __init__(self, config: RecoveryConfig):
        self.config = config

    def validate(self):
        if not self.config.source_device.startswith('/dev/'):
            raise ValueError(f"Invalid source device path: {self.config.source_device}")
        
        if re.match(r'^/dev/(vda\d*|nvme0n1p?\d*)$', self.config.source_device):
            raise ValueError(f"Source device looks like a root drive: {self.config.source_device}. Aborting for safety.")
        
        if not self.config.output_directory:
            raise ValueError("Output directory must be specified.")

@dataclass
class LLMResponse:
    thought: str
    command_name: str
    args: List[str]

class ResponseParser:
    def __init__(self, allowed_commands: List[str]):
        self.allowed_commands = allowed_commands

    def _extract_json_object(self, raw_json: str) -> str:
        json_str = raw_json.strip()
        if json_str.startswith("```json"):
            json_str = json_str[7:]
        elif json_str.startswith("```"):
            json_str = json_str[3:]
        if json_str.endswith("```"):
            json_str = json_str[:-3]
        json_str = json_str.strip()

        if json_str.startswith("{") and json_str.endswith("}"):
            return json_str

        start = json_str.find("{")
        end = json_str.rfind("}")
        if start != -1 and end != -1 and start < end:
            return json_str[start:end + 1]

        return json_str

    def _preview(self, value: Any) -> str:
        text = value if isinstance(value, str) else repr(value)
        text = re.sub(r"\s+", " ", text).strip()
        if len(text) > 500:
            return text[:500] + "..."
        return text

    def parse_and_validate(self, raw_json: str) -> LLMResponse:
        try:
            json_str = self._extract_json_object(raw_json)
            data = json.loads(json_str)
        except json.JSONDecodeError as e:
            raise ValueError(f"Failed to parse LLM response as JSON. Error: {e}\nRaw Response: {raw_json}")

        if isinstance(data, dict) and isinstance(data.get("action"), dict):
            action = data["action"]
            data = {**data, **action}

        thought = data.get('thought') or data.get('thoughts') or data.get('reasoning') or data.get('rationale')
        command_name = data.get('command_name') or data.get('command') or data.get('cmd') or data.get('tool')
        args = data.get('args')
        if args is None:
            args = data.get('arguments')
        if args is None:
            args = data.get('params')

        if isinstance(args, str):
            args = shlex.split(args)
        elif args is None:
            args = []
        elif isinstance(args, tuple):
            args = list(args)

        if thought is None:
            thought = "No thought provided by model."

        if isinstance(command_name, str) and not args:
            command_parts = shlex.split(command_name)
            if command_parts:
                command_name = command_parts[0]
                args = command_parts[1:]

        if not isinstance(thought, str) or not isinstance(command_name, str) or not isinstance(args, list):
            raise ValueError(
                "Invalid schema. Expected JSON like "
                '{"thought":"...","command_name":"lsblk","args":["-f"]}. '
                f"Received types: thought={type(thought).__name__}, "
                f"command_name={type(command_name).__name__}, args={type(args).__name__}. "
                f"Response preview: {self._preview(data)}"
            )

        if not all(isinstance(arg, str) for arg in args):
            raise ValueError(
                "Invalid schema. Every args item must be a string. "
                f"Response preview: {self._preview(data)}"
            )

        if command_name != 'finish' and command_name not in self.allowed_commands:
            raise ValueError(f"Command '{command_name}' is not in the allowed list.")

        return LLMResponse(thought=thought, command_name=command_name, args=args)


@dataclass
class LLMQueryResult:
    model: str
    raw_content: str
    parsed: LLMResponse


class RecoveryAgent:
    def __init__(self, config: RecoveryConfig, broadcast_callback):
        self.config = config
        self.validator = PolicyValidator(config)
        self.parser = ResponseParser(config.allowed_commands)
        self.history: List[Dict[str, Any]] = []
        self.broadcast = broadcast_callback
        self.is_running = False
        self.current_iteration = 0
        self.current_thought = "Waiting for agent to start..."
        self.current_action = "None"
        self.phase = "Idle"
        self.active_model = ""
        self.available_models: List[str] = []
        self.last_error = ""
        self.events: List[Dict[str, Any]] = [{"type": "log", "message": "Terminal initialized...", "level": "system"}]
        
        self.system_prompt = f"""You are an autonomous data recovery expert orchestrating a recovery pipeline.
Your goal is to safely mount the failing drive {config.source_device} and selectively copy important files to {config.output_directory}.
You have access to the following commands: {', '.join(config.allowed_commands)}.

Important: We are using "Selective File Recovery" because we do not have enough local storage for a 2TB full disk clone. 
You MUST NOT use `ddrescue`.
Prefer read-only inspection. Mount the source read-only when mounting is needed, inspect the filesystem using `ls` and `find`, and selectively copy files using `cp` or `rsync` to `{config.output_directory}`.
Never mount the source drive on `{config.output_directory}`. Use `{config.source_mount_dir}` as the source mount point; `{config.output_directory}` is only the copy destination.
The backend cannot enter sudo passwords and must not try password workarounds. Do not use sudo, echo, shell password piping, askpass, or interactive commands.
For privileged setup, use only these virtual commands: `safe_prepare`, `safe_mount`, `safe_umount`, `safe_diagnose`, and `safe_photorec_office_video`. They call a root-owned helper if the user installed it. Do not call sudo directly.
If `safe_mount` fails because NTFS metadata is damaged, use `safe_diagnose`, then switch to non-mount recovery planning with `testdisk` or `photorec`.
If the helper is not installed or sudo is not configured, output `finish` and explain that the user must install the safe helper.
Do not run destructive commands. Do not format, repair, fsck, write to the source disk, or change partition tables.

You must output exactly one JSON object in your response, matching this schema:
{{
  "thought": "Your reasoning for the next step based on the previous output.",
  "command_name": "The CLI command to run or 'finish' if complete.",
  "args": ["list", "of", "arguments"]
}}
"""

    def _remember_event(self, message: Dict[str, Any]):
        if message.get("type") in {"log", "output", "error"}:
            self.events.append(message)
            self.events = self.events[-300:]

    async def _publish(self, message: Dict[str, Any]):
        self._remember_event(message)
        await self.broadcast(message)

    async def _log(self, msg: str, level="info"):
        logger.info(msg)
        await self._publish({"type": "log", "message": msg, "level": level})

    def snapshot(self) -> Dict[str, Any]:
        return {
            "type": "snapshot",
            "running": self.is_running,
            "phase": self.phase,
            "current_iteration": self.current_iteration,
            "max_iterations": self.config.max_iterations,
            "thought": self.current_thought,
            "action": self.current_action,
            "source_device": self.config.source_device,
            "output_directory": self.config.output_directory,
            "source_mount_dir": self.config.source_mount_dir,
            "safe_helper": self.config.safe_helper,
            "llm_api_url": self.config.llm_api_url,
            "active_model": self.active_model,
            "model_queue": self.config.llm_models,
            "available_models": self.available_models,
            "advisor_command": self.config.advisor_command or "disabled",
            "last_error": self.last_error,
            "events": self.events,
        }

    def _ollama_base_url(self) -> str:
        return self.config.llm_api_url.split("/v1/")[0].rstrip("/")

    def _available_models_sync(self) -> List[str]:
        tags_url = f"{self._ollama_base_url()}/api/tags"
        req = urllib.request.Request(tags_url, headers={'Content-Type': 'application/json'})
        with urllib.request.urlopen(req, timeout=10) as response:
            result = json.loads(response.read().decode('utf-8'))
        return [model.get("name") for model in result.get("models", []) if model.get("name")]

    async def refresh_available_models(self):
        try:
            self.available_models = await asyncio.to_thread(self._available_models_sync)
        except Exception as e:
            self.available_models = []
            await self._log(f"Could not query local LLM model list: {e}", "error")

    def _model_candidates(self) -> List[str]:
        if not self.available_models:
            return self.config.llm_models

        preferred = [model for model in self.config.llm_models if model in self.available_models]
        extras = [model for model in self.available_models if model not in preferred]
        return preferred + extras

    def _query_llm_sync(self, user_prompt: str, model: str) -> str:
        messages = [{"role": "system", "content": self.system_prompt}]
        for msg in self.history:
            messages.append(msg)
        
        messages.append({"role": "user", "content": user_prompt})
        
        payload = json.dumps({
            "model": model,
            "messages": messages,
            "temperature": 0.0,
            "response_format": {"type": "json_object"}
        }).encode('utf-8')

        req = urllib.request.Request(
            self.config.llm_api_url,
            data=payload,
            headers={'Content-Type': 'application/json'}
        )
        with urllib.request.urlopen(req, timeout=120) as response:
            result = json.loads(response.read().decode('utf-8'))
            return result['choices'][0]['message']['content']

    async def _query_and_parse(self, user_prompt: str) -> LLMQueryResult:
        errors = []
        for model in self._model_candidates():
            try:
                self.active_model = model
                self.phase = "Querying local LLM"
                await self._publish({"type": "status", "running": self.is_running, "phase": self.phase, "active_model": self.active_model})
                await self._log(f"Querying local LLM: {model}")
                llm_content = await asyncio.to_thread(self._query_llm_sync, user_prompt, model)
                parsed = self.parser.parse_and_validate(llm_content)
                self.history.append({"role": "user", "content": user_prompt})
                self.history.append({"role": "assistant", "content": llm_content})
                self.last_error = ""
                return LLMQueryResult(model=model, raw_content=llm_content, parsed=parsed)
            except Exception as e:
                error = f"{model}: {e}"
                errors.append(error)
                await self._log(f"LLM attempt failed: {error}", "error")

        self.last_error = " | ".join(errors)
        raise ValueError(f"All local LLM attempts failed. {self.last_error}")

    async def _execute_command(self, response: LLMResponse) -> str:
        if response.command_name == 'finish':
            return "Execution finished by LLM."

        safe_actions = {
            "safe_prepare": "prepare",
            "safe_mount": "mount",
            "safe_umount": "umount",
            "safe_diagnose": "diagnose",
            "safe_photorec_office_video": "photorec-office-video",
        }
        if response.command_name in safe_actions:
            action = safe_actions[response.command_name]
            if not Path(self.config.safe_helper).exists():
                return (
                    "Manual setup required: safe sudo helper is not installed.\n"
                    f"Install {BASE_DIR / 'recovery_safe_helper.py'} as root-owned "
                    f"{self.config.safe_helper}, then allow only that helper with sudoers.\n"
                    "Do not grant passwordless sudo to Python, bash, mount, or arbitrary commands."
                )
            cmd_list = ["sudo", "-n", self.config.safe_helper, action]
            cmd_str = shlex.join(cmd_list)
            await self._log(f"Executing safe privileged helper: {cmd_str}")
            try:
                proc = await asyncio.create_subprocess_exec(
                    *cmd_list,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE
                )
                stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=120)
                output = f"Exit Code: {proc.returncode}\n"
                if stdout:
                    output += f"STDOUT:\n{stdout.decode('utf-8', errors='ignore')}\n"
                if stderr:
                    output += f"STDERR:\n{stderr.decode('utf-8', errors='ignore')}\n"
                return output
            except Exception as e:
                return f"Error executing safe helper: {str(e)}"

        cmd_list = [response.command_name] + response.args
        if response.command_name == "sudo":
            return (
                "Manual action required: backend cannot provide a sudo password.\n"
                f"Run this in your terminal if you approve the mount:\n"
                f"sudo mkdir -p {self.config.source_mount_dir} && "
                f"sudo mount -o ro {self.config.source_device} {self.config.source_mount_dir}\n"
                "Then return to the UI and start the loop again."
            )

        cmd_str = shlex.join(cmd_list)
        await self._log(f"Executing safely: {cmd_str}")
        
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd_list,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=self.config.timeout_seconds)
            
            output = f"Exit Code: {proc.returncode}\n"
            if stdout:
                output += f"STDOUT:\n{stdout.decode('utf-8', errors='ignore')}\n"
            if stderr:
                output += f"STDERR:\n{stderr.decode('utf-8', errors='ignore')}\n"
                
            return output
        except asyncio.TimeoutError:
            return f"Error: Command timed out after {self.config.timeout_seconds} seconds."
        except Exception as e:
            return f"Error executing command: {str(e)}"

    async def run(self):
        if self.is_running:
            return
        
        self.is_running = True
        self.phase = "Running"
        await self._publish({"type": "status", "running": True, "phase": self.phase, "active_model": self.active_model})
        await self._log("Starting Autonomous Recovery Agent...")
        await self.refresh_available_models()
        if self.available_models:
            await self._log(f"Local LLM models available: {', '.join(self.available_models)}")
        await self._log(f"Model preference order: {', '.join(self._model_candidates())}")
        
        try:
            self.validator.validate()
        except Exception as e:
            await self._log(f"Validation failed: {e}", "error")
            self.is_running = False
            self.phase = "Validation failed"
            self.last_error = str(e)
            await self._publish({"type": "status", "running": False, "phase": self.phase, "active_model": self.active_model})
            return
            
        current_prompt = f"Initial prompt: Please start diagnosing {self.config.source_device}."
        
        for iteration in range(self.config.max_iterations):
            if not self.is_running:
                break
                
            self.current_iteration = iteration + 1
            await self._publish({"type": "iteration", "current": self.current_iteration, "max": self.config.max_iterations})
            await self._log(f"--- Iteration {iteration + 1}/{self.config.max_iterations} ---")
            
            try:
                query_result = await self._query_and_parse(current_prompt)
                parsed = query_result.parsed
                
                self.current_thought = parsed.thought
                self.current_action = shlex.join([parsed.command_name] + parsed.args)
                await self._publish({"type": "thought", "thought": self.current_thought})
                await self._publish({"type": "action", "action": self.current_action})
                
                if parsed.command_name == 'finish':
                    await self._log("LLM determined that the task is finished.")
                    self.phase = "Finished"
                    break
                
                self.phase = "Executing command"
                await self._publish({"type": "status", "running": True, "phase": self.phase, "active_model": self.active_model})
                cmd_output = await self._execute_command(parsed)
                await self._publish({"type": "output", "output": cmd_output})
                
                current_prompt = f"Previous command output:\n{cmd_output}\n\nPlease provide the next command in JSON format."
                
            except Exception as e:
                self.last_error = str(e)
                await self._log(f"Error during loop: {e}", "error")
                current_prompt = f"An error occurred: {e}\nPlease correct the issue and provide the next step."
                await asyncio.sleep(2) # Prevent rapid failure looping

        await self._log("Autonomous Recovery Pipeline terminated.")
        self.is_running = False
        if self.phase in {"Running", "Stopping"}:
            self.phase = "Stopped"
        await self._publish({"type": "status", "running": False, "phase": self.phase, "active_model": self.active_model})

    def stop(self):
        self.is_running = False
        self.phase = "Stopping"

# FastAPI Setup
app = FastAPI(title="Autonomous Data Recovery Dashboard")

app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")

active_connections: List[WebSocket] = []

async def broadcast_ws(message: dict):
    for connection in active_connections:
        try:
            await connection.send_json(message)
        except Exception:
            pass

def env_list(name: str, default: List[str]) -> List[str]:
    value = os.environ.get(name, "")
    if not value.strip():
        return default
    return [item.strip() for item in value.split(",") if item.strip()]

def default_advisor_command() -> str:
    return shlex.join([sys.executable, str(DEFAULT_ADVISOR_SCRIPT), "--prompt"])

config = RecoveryConfig(
    source_device=os.environ.get("RECOVERY_SOURCE_DEVICE", "/dev/sda1"),
    output_directory=os.environ.get("RECOVERY_OUTPUT_DIR", str(DEFAULT_WORKSPACE_DIR / "seagate_mount")),
    llm_api_url=os.environ.get("RECOVERY_LLM_API_URL", "http://localhost:11434/v1/chat/completions"),
    llm_models=env_list("RECOVERY_LLM_MODELS", [
        "mistral-nemo:12b",
        "qwen2.5-coder:7b",
        "qwen2.5:7b",
        "llama3.2:latest",
    ]),
    advisor_command=os.environ.get("RECOVERY_ADVISOR_COMMAND", default_advisor_command()),
    max_iterations=int(os.environ.get("RECOVERY_MAX_ITERATIONS", "10")),
    timeout_seconds=int(os.environ.get("RECOVERY_TIMEOUT_SECONDS", "3600")),
    source_mount_dir=os.environ.get("RECOVERY_SOURCE_MOUNT_DIR", str(DEFAULT_SOURCE_MOUNT_DIR)),
    safe_helper=os.environ.get("RECOVERY_SAFE_HELPER", "/usr/local/sbin/recovery-safe"),
)

# Global agent instance
agent = RecoveryAgent(config, broadcast_ws)

@app.get("/")
async def get_index():
    with open(BASE_DIR / "static" / "index.html", "r") as f:
        return HTMLResponse(f.read())

@app.get("/style.css")
async def get_style():
    from fastapi.responses import FileResponse
    return FileResponse(BASE_DIR / "static" / "style.css")

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    active_connections.append(websocket)
    await websocket.send_json(agent.snapshot())
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        if websocket in active_connections:
            active_connections.remove(websocket)

@app.get("/status")
async def get_status():
    return agent.snapshot()

@app.get("/models")
async def get_models():
    await agent.refresh_available_models()
    return {
        "preferred": config.llm_models,
        "available": agent.available_models,
        "active": agent.active_model,
    }

@app.post("/start")
async def start_recovery():
    if not agent.is_running:
        asyncio.create_task(agent.run())
    return {"status": "started"}

@app.post("/stop")
async def stop_recovery():
    agent.stop()
    return {"status": "stopped"}

class CommandRequest(BaseModel):
    command: str

class AdvisorRequest(BaseModel):
    prompt: str

def advisor_command_allowed(base_cmd: List[str]) -> bool:
    if len(base_cmd) >= 2:
        command_path = Path(base_cmd[0]).resolve()
        script_path = Path(base_cmd[1]).resolve()
        if command_path == Path(sys.executable).resolve() and script_path == DEFAULT_ADVISOR_SCRIPT:
            return True
    return bool(base_cmd) and base_cmd[0] in config.manual_allowed_commands

@app.post("/execute_command")
async def execute_command(req: CommandRequest):
    try:
        cmd_list = shlex.split(req.command)
        if not cmd_list:
            return {"output": "No command provided."}

        command_name = cmd_list[0]
        if command_name not in config.manual_allowed_commands:
            return {"output": f"Command '{command_name}' is not in the manual allowed list."}

        proc = await asyncio.create_subprocess_exec(
            *cmd_list,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=60)
        output = f"Exit Code: {proc.returncode}\n"
        if stdout:
            output += f"STDOUT:\n{stdout.decode('utf-8', errors='ignore')}\n"
        if stderr:
            output += f"STDERR:\n{stderr.decode('utf-8', errors='ignore')}\n"

        await agent._publish({"type": "log", "message": f"Manual command: {shlex.join(cmd_list)}", "level": "info"})
        await agent._publish({"type": "output", "output": output})
        return {"output": output}
    except Exception as e:
        return {"output": str(e)}

@app.post("/advisor")
async def ask_advisor(req: AdvisorRequest):
    if not config.advisor_command:
        return {"output": "Advisor command is disabled. Set RECOVERY_ADVISOR_COMMAND, for example: gemini -p"}

    try:
        base_cmd = shlex.split(config.advisor_command)
        if not base_cmd:
            return {"output": "Advisor command is empty."}

        if not advisor_command_allowed(base_cmd):
            return {"output": f"Advisor command '{base_cmd[0]}' is not allowed."}

        prompt = (
            "You are advising a safe selective hard-drive recovery workflow. "
            "Do not suggest destructive writes, formatting, fsck repair, partition edits, or full-disk cloning. "
            "Never mount the source drive on the recovery output directory; use a separate read-only source mount point. "
            f"Preferred source mount point: {config.source_mount_dir}. "
            "The backend cannot enter sudo passwords; privileged mount steps must be run manually in a terminal. "
            "Return concise advice only; do not execute commands.\n\n"
            f"Source device: {config.source_device}\n"
            f"Output directory: {config.output_directory}\n"
            f"Source mount point: {config.source_mount_dir}\n"
            f"Current phase: {agent.phase}\n"
            f"Current action: {agent.current_action}\n\n"
            f"{req.prompt}"
        )
        proc = await asyncio.create_subprocess_exec(
            *base_cmd,
            prompt,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=180)
        output = f"Exit Code: {proc.returncode}\n"
        if stdout:
            output += f"STDOUT:\n{stdout.decode('utf-8', errors='ignore')}\n"
        if stderr:
            output += f"STDERR:\n{stderr.decode('utf-8', errors='ignore')}\n"

        await agent._publish({"type": "log", "message": f"Advisor command: {shlex.join(base_cmd)}", "level": "info"})
        await agent._publish({"type": "output", "output": output})
        return {"output": output}
    except Exception as e:
        return {"output": str(e)}

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
