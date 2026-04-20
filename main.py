import asyncio
import os
import subprocess
import sys
from task_manager import TaskManager
from plc_manager import PLCManager
from camera_manager import CameraManager
from image_processing import ImageProcessor
from config_manager import config
import log_config


def get_version_info():
    """尝试读取 git branch + commit. PyInstaller 打包后不可用."""
    here = os.path.dirname(os.path.abspath(__file__))
    try:
        branch = subprocess.check_output(
            ['git', 'branch', '--show-current'],
            cwd=here, text=True, stderr=subprocess.DEVNULL, timeout=2,
        ).strip()
        commit = subprocess.check_output(
            ['git', 'rev-parse', '--short', 'HEAD'],
            cwd=here, text=True, stderr=subprocess.DEVNULL, timeout=2,
        ).strip()
        dirty = subprocess.run(
            ['git', 'diff-index', '--quiet', 'HEAD'],
            cwd=here, stderr=subprocess.DEVNULL,
        ).returncode != 0
        return f"branch={branch} commit={commit}{'+dirty' if dirty else ''}"
    except Exception:
        return "unknown (not a git repo or bundled binary)"


async def main():
    logger = log_config.setup_logging()

    logger.info(f"[SYS] ======== 程序启动 ========")
    logger.info(f"[SYS] 版本: {get_version_info()}")
    logger.info(f"[SYS] Python: {sys.version.split()[0]}  argv: {sys.argv}")
    logger.info(f"[SYS] 工作目录: {os.getcwd()}")

    plc_manager = PLCManager(config.get_plc_ip())
    camera_manager = CameraManager()
    image_processor = ImageProcessor()

    task_manager = TaskManager(plc_manager, camera_manager, image_processor, config, logger)

    await task_manager.run()


if __name__ == "__main__":
    asyncio.run(main())
