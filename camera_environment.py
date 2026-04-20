import os
import platform
import sys
import log_config

logger = log_config.setup_logging()

def setup_camera_environment():
    os_type = platform.system()
    arch_type = platform.machine()

    if os_type == "Windows":
        lib_path = r"C:\Program Files (x86)\MVS\Development\Samples\Python\MvImport"
        # 将 lib_path 添加到 Python 的搜索路径
        sys.path.append(lib_path)
        # 可选：如果需要，也可以添加到 PATH 环境变量
        # os.environ['PATH'] = lib_path + os.pathsep + os.environ['PATH']
    elif os_type == "Linux":
        if arch_type == "aarch64":
            lib_path = "/opt/MVS/Samples/aarch64/Python/MvImport"
        elif arch_type == "x86_64":
            os.environ['MVCAM_COMMON_RUNENV'] = '/opt/MVS/lib'
            lib_path = "/opt/MVS/Samples/64/Python/MvImport"
        else:
            logger.error("Unsupported Linux architecture")
            return False
        sys.path.append(lib_path)
        os.environ['LD_LIBRARY_PATH'] = lib_path + os.pathsep + os.environ.get('LD_LIBRARY_PATH', '')
    else:
        logger.error("Unsupported operating system")
        return False

    logger.info(f"Camera environment set up for {os_type} on {arch_type}")
    logger.info(f"Python path: {sys.path}")
    return True