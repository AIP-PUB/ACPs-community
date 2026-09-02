"""容器运行时的双 listener supervisor。"""

from __future__ import annotations

import os
import signal
import subprocess  # nosec B404 - supervisor launches fixed internal listener entrypoints
import sys
import time
from collections.abc import Iterable

from app.core.config import settings

PUBLIC_PROCESS_NAME = "public"
MTLS_PROCESS_NAME = "mtls"
POLL_INTERVAL_SECONDS = 0.5

type ManagedProcesses = dict[str, subprocess.Popen[bytes]]


def _mtls_listener_enabled() -> bool:
    """返回是否应启动 9002 mTLS listener。"""
    return settings.enable_mtls_listener


def _build_public_command() -> list[str]:
    """构建 public listener 的启动命令。"""
    command = [
        sys.executable,
        "-m",
        "uvicorn",
        "app.main:app",
        "--host",
        settings.uvicorn_host,
        "--port",
        str(settings.uvicorn_port),
        "--log-level",
        settings.uvicorn_log_level,
        "--timeout-keep-alive",
        os.getenv("UVICORN_TIMEOUT_KEEP_ALIVE", "65"),
        "--timeout-graceful-shutdown",
        os.getenv("UVICORN_TIMEOUT_GRACEFUL_SHUTDOWN", "30"),
        "--limit-concurrency",
        os.getenv("UVICORN_LIMIT_CONCURRENCY", "100"),
        "--no-access-log",
    ]

    if settings.uvicorn_reload:
        command.append("--reload")

    return command


def _build_mtls_command() -> list[str]:
    """构建 mTLS listener 的启动命令。"""
    return [sys.executable, "-m", "app.main_mtls"]


def _start_process(command: list[str]) -> subprocess.Popen[bytes]:
    """启动子进程并复用容器标准输出。"""
    return subprocess.Popen(command)  # noqa: S603  # nosec B603 - command is built from internal entrypoints without shell expansion


def _start_processes() -> ManagedProcesses:
    """启动 public 与 mTLS 两个 listener 进程。"""
    processes: ManagedProcesses = {
        PUBLIC_PROCESS_NAME: _start_process(_build_public_command()),
    }

    if _mtls_listener_enabled():
        processes[MTLS_PROCESS_NAME] = _start_process(_build_mtls_command())

    return processes


def _terminate_process(process: subprocess.Popen[bytes], *, sig: int) -> None:
    """向仍在运行的子进程发送终止信号。"""
    if process.poll() is None:
        process.send_signal(sig)


def _terminate_processes(processes: ManagedProcesses, *, sig: int, exclude_name: str | None = None) -> None:
    """终止受管子进程，可选跳过已退出的进程名。"""
    for name, process in processes.items():
        if name == exclude_name:
            continue
        _terminate_process(process, sig=sig)


def _wait_for_shutdown(processes: Iterable[subprocess.Popen[bytes]]) -> None:
    """等待所有子进程结束。"""
    for process in processes:
        try:
            process.wait(timeout=30)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


def _poll_exited_process(processes: ManagedProcesses) -> tuple[str, int] | None:
    """返回首个已退出的子进程及其退出码。"""
    for name, process in processes.items():
        return_code = process.poll()
        if return_code is not None:
            return name, return_code
    return None


def main() -> int:
    """启动并监督 public 与 mTLS 两个 listener。"""
    processes = _start_processes()
    stop_requested = False

    def _handle_signal(signum: int, _frame: object) -> None:
        nonlocal stop_requested
        stop_requested = True
        for process in processes.values():
            _terminate_process(process, sig=signum)

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    try:
        while True:
            exited = _poll_exited_process(processes)
            if exited is not None:
                exited_name, return_code = exited
                if not stop_requested:
                    _terminate_processes(processes, sig=signal.SIGTERM, exclude_name=exited_name)

                _wait_for_shutdown(processes.values())
                return 0 if stop_requested else return_code

            time.sleep(POLL_INTERVAL_SECONDS)
    finally:
        _terminate_processes(processes, sig=signal.SIGTERM)
        _wait_for_shutdown(processes.values())


if __name__ == "__main__":
    raise SystemExit(main())
