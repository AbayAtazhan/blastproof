import os
import shutil
import subprocess

import pytest


def _docker_ready() -> bool:
    if not shutil.which("docker"):
        return False
    image = os.environ.get("SANDBOX_IMAGE", "blastproof-sandbox:latest")
    return subprocess.run(["docker", "image", "inspect", image], capture_output=True).returncode == 0


docker = pytest.mark.skipif(not _docker_ready(), reason="docker + sandbox image required")
