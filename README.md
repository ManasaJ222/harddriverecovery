# Hard Drive Recovery Dashboard

A local-first recovery dashboard for safely working through selective file recovery from a large or damaged external drive when there is not enough local storage for a full disk clone.

The project combines:

- a FastAPI web dashboard for status, logs, manual commands, and advisor prompts
- a local LLM recovery loop with strict command allowlists
- a root-owned helper script for the few privileged actions that may be needed
- PhotoRec workflows for selective recovery by file signature family

The source drive should be treated as read-only. The project is designed to avoid destructive actions such as formatting, filesystem repair, partition edits, or writes to the damaged drive.

## Why This Exists

The original use case is a 2 TB external NTFS drive with damaged filesystem metadata and limited local storage. A full clone is not always practical in that situation, so the workflow focuses on:

1. Diagnosing the drive without writing to it.
2. Trying a read-only mount if possible.
3. Falling back to TestDisk or PhotoRec when the filesystem cannot be mounted.
4. Recovering only high-value file categories instead of every executable/cache fragment.
5. Tracking progress through a browser UI.

## Project Layout

```text
.
├── autonomous_recovery.py     # FastAPI app, WebSocket dashboard backend, local LLM loop
├── recovery_advisor.py        # Local Ollama advisor used by the UI advisor endpoint
├── recovery_safe_helper.py    # Root-owned helper for safe privileged recovery actions
├── static/
│   ├── index.html             # Dashboard UI
│   └── style.css              # Dashboard styles
└── .gitignore                 # Excludes recovery logs, sessions, venv, caches
```

Runtime files such as `photorec.log`, `photorec.ses`, `photorec.se2`, recovered files, virtual environments, and Python caches should not be committed.

## Safety Model

The dashboard backend should not receive broad sudo access. Instead, privileged actions go through a small helper installed as `/usr/local/sbin/recovery-safe`.

The helper only exposes named actions:

```text
prepare
mount
umount
diagnose
photorec-media-docs
photorec-office-video
```

The local LLM is not allowed to run arbitrary shell commands with sudo. In the backend it can only request virtual safe commands such as:

```text
safe_prepare
safe_mount
safe_umount
safe_diagnose
safe_photorec_office_video
```

The helper is expected to be root-owned and executable. If passwordless sudo is configured, scope it only to this helper path, not to Python, Bash, mount, PhotoRec, or arbitrary commands.

Example sudoers line:

```sudoers
your_user ALL=(root) NOPASSWD: /usr/local/sbin/recovery-safe
```

Edit sudoers with:

```bash
sudo visudo
```

## Requirements

System tools:

```bash
sudo apt install testdisk ntfs-3g smartmontools
```

Python packages:

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install fastapi uvicorn pydantic
```

Local LLM support is optional but recommended. The backend defaults to an Ollama-compatible OpenAI-style endpoint:

```text
http://localhost:11434/v1/chat/completions
```

Preferred default model order:

```text
mistral-nemo:12b
qwen2.5-coder:7b
qwen2.5:7b
llama3.2:latest
```

## Configuration

The backend can be configured with environment variables:

```bash
export RECOVERY_SOURCE_DEVICE=/dev/sda1
export RECOVERY_OUTPUT_DIR="$HOME/workspace/seagate_mount"
export RECOVERY_SOURCE_MOUNT_DIR=/tmp/recovery_source_mount
export RECOVERY_LLM_API_URL=http://localhost:11434/v1/chat/completions
export RECOVERY_LLM_MODELS=mistral-nemo:12b,qwen2.5-coder:7b,qwen2.5:7b,llama3.2:latest
```

The safe helper also supports environment overrides:

```bash
export RECOVERY_OWNER_USER="$USER"
export RECOVERY_WORKSPACE_DIR="$HOME/workspace"
export RECOVERY_OUTPUT_DIR="$HOME/workspace/seagate_mount"
export RECOVERY_PHOTOREC_OUTPUT_DIR="$HOME/workspace/seagate_mount_filtered"
export RECOVERY_PHOTOREC_OFFICE_VIDEO_OUTPUT_DIR="$HOME/workspace/seagate_mount_office_video"
```

Defaults assume the source device is `/dev/sda1` and the workspace is `$HOME/workspace`.

## Install The Safe Helper

From the project directory:

```bash
sudo install -o root -g root -m 755 recovery_safe_helper.py /usr/local/sbin/recovery-safe
```

Verify available actions:

```bash
/usr/local/sbin/recovery-safe --help
```

If you configured passwordless sudo for this helper, verify:

```bash
sudo -n /usr/local/sbin/recovery-safe diagnose
```

## Run The Dashboard

Start the backend:

```bash
. .venv/bin/activate
python autonomous_recovery.py
```

Open:

```text
http://localhost:8000
```

The UI shows:

- WebSocket connection status
- current recovery phase
- source device and output path
- local LLM endpoint and active model
- agent thoughts and selected command
- terminal output and recent event history
- manual command execution
- advisor prompts
- hourly refresh countdown

## Recovery Workflows

### 1. Diagnose

Use the helper:

```bash
sudo -n /usr/local/sbin/recovery-safe diagnose
```

This runs read-only inspection commands such as `lsblk`, `blkid`, `file -s`, `ntfsinfo`, and recent `dmesg` output.

### 2. Try Read-Only Mount

```bash
sudo -n /usr/local/sbin/recovery-safe prepare
sudo -n /usr/local/sbin/recovery-safe mount
```

The helper attempts read-only NTFS mounts using safe mount points. If NTFS metadata is damaged, mounting may fail. Do not repair the drive just to make it mount.

Unmount:

```bash
sudo -n /usr/local/sbin/recovery-safe umount
```

### 3. PhotoRec Media And Documents

```bash
sudo -n /usr/local/sbin/recovery-safe photorec-media-docs
```

This targets a narrower set to avoid filling local storage:

```text
jpg, png, gif, bmp, tif, pdf, doc
```

PhotoRec 7.1 groups some older Office formats under the `doc` signature family, so this may also find files such as `.xls` and `.ppt`.

Default output:

```text
$HOME/workspace/seagate_mount_filtered*
```

### 4. PhotoRec Office And Video

```bash
sudo -n /usr/local/sbin/recovery-safe photorec-office-video
```

This targets:

```text
doc, zip, mov
```

These PhotoRec signature families are used because:

```text
doc -> doc, xls, ppt
zip -> docx, xlsx, pptx
mov -> mov, mp4, 3gp
```

Default output:

```text
$HOME/workspace/seagate_mount_office_video*
```

## Monitoring A PhotoRec Run

Check whether recovery is running:

```bash
pgrep -af 'photorec|recovery-safe'
```

Check output size:

```bash
du -sch "$HOME"/workspace/seagate_mount_office_video* 2>/dev/null | tail -1
```

Check disk space:

```bash
df -h "$HOME/workspace"
```

Check the latest PhotoRec log:

```bash
tail -40 photorec.log
```

## Sorting Recovered Files

PhotoRec writes files into numbered recovery folders. After a pass finishes, move or copy only the formats you want into a sorted destination.

Example for Office and video files:

```bash
mkdir -p "$HOME/workspace/recovered_sorted/office_video"

find "$HOME"/workspace/seagate_mount_office_video* -type f \( \
  -iname '*.docx' -o -iname '*.xlsx' -o -iname '*.pptx' -o \
  -iname '*.xls' -o -iname '*.ppt' -o \
  -iname '*.mov' -o -iname '*.mp4' -o -iname '*.3gp' \
\) -exec mv -n {} "$HOME/workspace/recovered_sorted/office_video/" \;
```

Use `mv -n` to avoid overwriting files with the same name. For stronger duplicate handling, use a small sorting script that appends unique suffixes.

## Local LLM Loop

The recovery agent asks a local model for one JSON command at a time:

```json
{
  "thought": "Reasoning for the next step.",
  "command_name": "safe_diagnose",
  "args": []
}
```

The parser accepts a few common model variations, but ultimately validates that:

- the response is JSON
- `command_name` is allowed
- `args` is a list of strings
- unsafe commands are rejected

If a model fails schema validation, the backend tries the next local model in the configured queue.

## Advisor Mode

`recovery_advisor.py` is a local, non-executing advisor. It asks Ollama for concise guidance and is intentionally instructed not to suggest destructive recovery actions.

The FastAPI backend uses it by default through:

```text
RECOVERY_ADVISOR_COMMAND="python recovery_advisor.py --prompt"
```

The advisor gives suggestions only. It does not run commands.

## What Not To Do

Avoid these actions on the damaged source drive:

- do not format
- do not run filesystem repair tools such as `fsck` or `ntfsfix` unless you have a verified clone
- do not delete files from the source drive to make later scans easier
- do not write recovered files back to the source drive
- do not grant an LLM broad sudo access
- do not commit recovered personal files, PhotoRec logs, or session files

## Git Hygiene

The `.gitignore` excludes local runtime and recovery artifacts:

```text
.venv/
__pycache__/
*.py[cod]
photorec.log
photorec.ses
photorec.se2
.env
.env.*
```

Before pushing, scan staged files for accidental secrets or personal data:

```bash
rg -n -i 'password|token|secret|api[_-]?key|bearer|authorization|github_pat|ghp_|sk-|BEGIN .*PRIVATE KEY|passport|social security|ssn|dob' .
```

## Current Limitations

- PhotoRec command-line file type names vary by version; this project uses the tokens verified against PhotoRec 7.1.
- Progress is inferred from PhotoRec logs and output folders, not from a structured PhotoRec API.
- Recovered files may have generated names and may need manual review or deduplication.
- The safe helper defaults to `/dev/sda1`; verify the correct source device before running recovery.

## License

This project is licensed under the MIT License. See [LICENSE](LICENSE).
