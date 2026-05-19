#!/usr/bin/env python3
import argparse
import os
import pwd
import subprocess
import sys
from pathlib import Path


SOURCE_DEVICE = "/dev/sda1"
SOURCE_MOUNT_DIR = "/tmp/recovery_source_mount"
OWNER_USER = os.environ.get("RECOVERY_OWNER_USER") or os.environ.get("SUDO_USER") or os.environ.get("USER", "")
WORKSPACE_DIR = os.environ.get("RECOVERY_WORKSPACE_DIR") or (
    f"/home/{OWNER_USER}/workspace" if OWNER_USER else ""
)
OUTPUT_DIR = os.environ.get("RECOVERY_OUTPUT_DIR") or f"{WORKSPACE_DIR}/seagate_mount"
PHOTOREC_OUTPUT_DIR = os.environ.get("RECOVERY_PHOTOREC_OUTPUT_DIR") or f"{WORKSPACE_DIR}/seagate_mount_filtered"
PHOTOREC_OFFICE_VIDEO_OUTPUT_DIR = (
    os.environ.get("RECOVERY_PHOTOREC_OFFICE_VIDEO_OUTPUT_DIR")
    or f"{WORKSPACE_DIR}/seagate_mount_office_video"
)


def run(cmd: list[str]) -> int:
    print("+ " + " ".join(cmd), flush=True)
    completed = subprocess.run(cmd, text=True)
    return completed.returncode


def owner_ids() -> tuple[int, int]:
    if not OWNER_USER:
        raise SystemExit("Set RECOVERY_OWNER_USER or run through sudo so SUDO_USER is available.")
    user = pwd.getpwnam(OWNER_USER)
    return user.pw_uid, user.pw_gid


def ensure_expected_paths() -> None:
    if not SOURCE_DEVICE.startswith("/dev/"):
        raise SystemExit("Unsafe source device configuration.")
    if not SOURCE_MOUNT_DIR.startswith("/tmp/recovery_"):
        raise SystemExit("Unsafe source mount directory configuration.")
    if not WORKSPACE_DIR or not Path(WORKSPACE_DIR).is_absolute():
        raise SystemExit("Unsafe workspace directory configuration.")
    for path in (OUTPUT_DIR, PHOTOREC_OUTPUT_DIR, PHOTOREC_OFFICE_VIDEO_OUTPUT_DIR):
        if not path.startswith(f"{WORKSPACE_DIR.rstrip('/')}/"):
            raise SystemExit(f"Unsafe output directory configuration: {path}")
    if not OUTPUT_DIR.startswith(f"{WORKSPACE_DIR.rstrip('/')}/"):
        raise SystemExit("Unsafe output directory configuration.")


def safe_mkdir(path: str) -> int:
    Path(path).mkdir(parents=True, exist_ok=True)
    uid, gid = owner_ids()
    os.chown(path, uid, gid)
    return 0


def safe_mount() -> int:
    ensure_expected_paths()
    safe_mkdir(SOURCE_MOUNT_DIR)
    safe_mkdir(OUTPUT_DIR)
    if os.path.ismount(SOURCE_MOUNT_DIR):
        print(f"{SOURCE_MOUNT_DIR} is already mounted.")
        return 0
    uid, gid = owner_ids()
    attempts = [
        [
            "mount", "-t", "ntfs3",
            "-o", f"ro,norecover,uid={uid},gid={gid}",
            SOURCE_DEVICE, SOURCE_MOUNT_DIR,
        ],
        [
            "ntfs-3g",
            "-o", f"ro,uid={uid},gid={gid}",
            SOURCE_DEVICE, SOURCE_MOUNT_DIR,
        ],
        ["mount", "-o", "ro", SOURCE_DEVICE, SOURCE_MOUNT_DIR],
    ]
    for cmd in attempts:
        code = run(cmd)
        if code == 0:
            return 0
        if os.path.ismount(SOURCE_MOUNT_DIR):
            return 0
    return 1


def safe_umount() -> int:
    ensure_expected_paths()
    if not os.path.ismount(SOURCE_MOUNT_DIR):
        print(f"{SOURCE_MOUNT_DIR} is not mounted.")
        return 0
    return run(["umount", SOURCE_MOUNT_DIR])


def safe_diagnose() -> int:
    ensure_expected_paths()
    commands = [
        ["lsblk", "-f", SOURCE_DEVICE],
        ["blkid", SOURCE_DEVICE],
        ["file", "-s", SOURCE_DEVICE],
        ["ntfsinfo", "-m", SOURCE_DEVICE],
        ["dmesg", "-T"],
    ]
    exit_code = 0
    for cmd in commands:
        print("\n== " + " ".join(cmd) + " ==", flush=True)
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=30,
            )
            output = (result.stdout + result.stderr).strip()
            if cmd[0] == "dmesg":
                lines = output.splitlines()
                output = "\n".join(lines[-80:])
            if output:
                print(output)
            if result.returncode not in (0, 1):
                exit_code = result.returncode
        except FileNotFoundError:
            print(f"{cmd[0]} not installed.")
        except subprocess.TimeoutExpired:
            print(f"{cmd[0]} timed out.")
            exit_code = 1
    return exit_code


def safe_photorec_media_docs() -> int:
    ensure_expected_paths()
    safe_mkdir(PHOTOREC_OUTPUT_DIR)
    uid, gid = owner_ids()
    os.chown(PHOTOREC_OUTPUT_DIR, uid, gid)

    # Keep this list narrow to avoid filling local storage with executables and low-value fragments.
    enabled_types = [
        "jpg", "png", "gif", "bmp", "tif",
        "pdf", "doc",
    ]
    fileopt = ["fileopt", "everything", "disable"]
    for file_type in enabled_types:
        fileopt.extend([file_type, "enable"])

    cmd = [
        "photorec",
        "/log",
        "/d",
        PHOTOREC_OUTPUT_DIR,
        "/cmd",
        "/dev/sda",
        ",".join([
            "partition_i386",
            "1",
            "blocksize",
            "1024",
            *fileopt,
            "options",
            "paranoid",
            "keep_corrupted_file_no",
            "freespace",
            "search",
        ]),
    ]
    return run(cmd)


def safe_photorec_office_video() -> int:
    ensure_expected_paths()
    safe_mkdir(PHOTOREC_OFFICE_VIDEO_OUTPUT_DIR)
    uid, gid = owner_ids()
    os.chown(PHOTOREC_OFFICE_VIDEO_OUTPUT_DIR, uid, gid)

    # PhotoRec 7.1 groups these formats by signature family:
    # doc -> doc/xls/ppt, zip -> docx/xlsx/pptx, mov -> mov/mp4/3gp.
    enabled_types = ["doc", "zip", "mov"]
    fileopt = ["fileopt", "everything", "disable"]
    for file_type in enabled_types:
        fileopt.extend([file_type, "enable"])

    cmd = [
        "photorec",
        "/log",
        "/d",
        PHOTOREC_OFFICE_VIDEO_OUTPUT_DIR,
        "/cmd",
        "/dev/sda",
        ",".join([
            "partition_i386",
            "1",
            "blocksize",
            "1024",
            *fileopt,
            "options",
            "paranoid",
            "keep_corrupted_file_no",
            "freespace",
            "search",
        ]),
    ]
    return run(cmd)


def main() -> int:
    parser = argparse.ArgumentParser(description="Root-owned hard-drive recovery helper.")
    parser.add_argument(
        "action",
        choices=[
            "mount",
            "umount",
            "prepare",
            "diagnose",
            "photorec-media-docs",
            "photorec-office-video",
        ],
    )
    args = parser.parse_args()

    if args.action == "prepare":
        ensure_expected_paths()
        safe_mkdir(SOURCE_MOUNT_DIR)
        safe_mkdir(OUTPUT_DIR)
        return 0
    if args.action == "mount":
        return safe_mount()
    if args.action == "umount":
        return safe_umount()
    if args.action == "diagnose":
        return safe_diagnose()
    if args.action == "photorec-media-docs":
        return safe_photorec_media_docs()
    if args.action == "photorec-office-video":
        return safe_photorec_office_video()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
