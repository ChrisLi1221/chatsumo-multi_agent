# providers/provider_sumo_runner.py
import os, sys, subprocess, shutil, logging, xml.etree.ElementTree as ET
from typing import Dict, Any
from fastmcp import FastMCP
from exceptiongroup import ExceptionGroup

logging.basicConfig(stream=sys.stderr, level=logging.INFO)
mcp = FastMCP("sumo-runner")


def _ok(message: str = "", artifacts: Dict[str, Any] | None = None, **extra) -> Dict[str, Any]:
    artifacts = artifacts or {}
    return {
        "status": "ok",
        "success": True,
        "message": message,
        "artifacts": artifacts,
        **extra,
    }


def _err(error_type: str, message: str, **extra) -> Dict[str, Any]:
    return {
        "status": "error",
        "success": False,
        "error_type": error_type,
        "message": message,
        "error": message,
        **extra,
    }


def _has_gui() -> bool:
    return bool(os.environ.get("DISPLAY")) and bool(shutil.which("sumo-gui"))


def _remove_if_exists(path: str):
    try:
        if path and os.path.exists(path):
            os.remove(path)
    except Exception as e:
        logging.warning("Failed to remove %s: %s", path, e)


def _tripinfo_from_cfg(cfg_abs: str) -> str | None:
    try:
        root = ET.parse(cfg_abs).getroot()
        report = root.find("report")
        if report is not None:
            # SUMO 的 tripinfo-output 在 <report> 下
            for child in report:
                if child.tag == "tripinfo-output":
                    return os.path.abspath(child.get("value", "").strip())
        # 兼容写法：<tripinfo-output value="..."/>
        el = root.find(".//tripinfo-output")
        if el is not None:
            return os.path.abspath(el.get("value", "").strip())
    except Exception as e:
        logging.warning("parse cfg failed: %s", e)
    return None


@mcp.tool()
def run(
    cfg_path: str,
    gui: bool = False,
    step_limit: int = 1800,
    outputs: list[str] | None = None,
) -> Dict[str, Any]:
    """
    每次运行前：
      1) 解析 cfg，找到 tripinfo 输出路径并删除（如果存在）
      2) 删除上一轮的 sumo.run.log
      3) 在 cfg 所在目录运行 sumo/sumo-gui
    """
    cfg_abs = os.path.abspath(cfg_path)
    cfg_dir = os.path.dirname(cfg_abs)
    cfg_name = os.path.basename(cfg_abs)

    # 1) 删除 tripinfo
    tripinfo = _tripinfo_from_cfg(cfg_abs)
    if tripinfo:
        _remove_if_exists(tripinfo)

    # 2) 删除旧日志
    log_path = os.path.join(cfg_dir, "sumo.run.log")
    _remove_if_exists(log_path)

    try:
        # 3) 选择 bin 并运行（屏蔽所有 stdout 到文件，避免污染 MCP stdout）
        # bin_ = "sumo-gui" if (gui and _has_gui()) else "sumo"
        bin_ = "sumo"
        cmd = [bin_, "-c", cfg_name, "--start", "--quit-on-end"]
        logging.info("running: %s (cwd=%s)", " ".join(cmd), cfg_dir)

        with open(log_path, "w", encoding="utf-8") as logf:
            subprocess.run(
                cmd,
                check=True,
                cwd=cfg_dir,
                stdout=logf,
                stderr=logf,
                env=os.environ.copy(),
            )
        
        logging.info("tripinfo path: %s", tripinfo)
        
        return _ok(
            message="SUMO simulation completed.",
            artifacts={"cfg": cfg_abs, "tripinfo": tripinfo, "log": log_path},
            cfg=cfg_abs,
            tripinfo=tripinfo,
            log=log_path,
            used_gui=(bin_ == "sumo-gui"),
        )
    except Exception as e:
        logging.exception("SUMO run failed")
        return _err(
            "SUMO_RUN_FAILED",
            str(e),
            cfg=cfg_abs,
            tripinfo=tripinfo,
            log=log_path,
        )

def _flatten_exceptiongroup(e: BaseException) -> list[BaseException]:
    """Collect all leaf exceptions from an ExceptionGroup (py3.11 backport style)."""
    leaves: list[BaseException] = []

    def collect(ex: BaseException):
        if isinstance(ex, ExceptionGroup):
            for sub in ex.exceptions:
                collect(sub)
        else:
            leaves.append(ex)

    collect(e)
    return leaves

if __name__ == "__main__":
    try:
        mcp.run()
    except ExceptionGroup as eg:
        leaves = _flatten_exceptiongroup(eg)
        # If every sub-exception is BrokenPipeError, the client closed the pipe.
        if leaves and all(isinstance(x, BrokenPipeError) for x in leaves):
            logging.warning("Client disconnected (BrokenPipe). Exiting cleanly.")
            sys.exit(0)
        # Otherwise, re-raise so real bugs stay visible.
        raise
    except BrokenPipeError:
        logging.warning("Client disconnected (BrokenPipe). Exiting cleanly.")
        sys.exit(0)
    except KeyboardInterrupt:
        logging.info("Interrupted by user. Exiting cleanly.")
        sys.exit(0)
