"""Build-time support for the JavaScript runtime shipped with Windows apps."""

import re
import shutil
import subprocess
import ssl
from pathlib import Path
from urllib.request import urlopen


def ssl_runtime_data():
    """Preserve auxiliary certificate policy files required by the build Python."""
    runtime_file = Path(ssl.__file__).with_name("codex-rejected-cas.pem")
    if runtime_file.is_file():
        return [(str(runtime_file), ".")]
    source = Path(ssl.__file__)
    if source.is_file() and "codex-rejected-cas.pem" in source.read_text(encoding="utf-8"):
        raise RuntimeError("The Python TLS policy file is missing; repair the build runtime before packaging.")
    return []


def node_runtime_artifacts():
    node = shutil.which("node")
    if not node:
        raise RuntimeError("Install Node.js before packaging: GoFile needs its bundled JavaScript runtime.")
    version = subprocess.check_output([node, "--version"], text=True).strip()
    if not re.fullmatch(r"v\d+\.\d+\.\d+", version):
        raise RuntimeError("Unexpected Node.js version: " + version)
    license_path = Path("build") / f"node-{version}" / "LICENSE"
    if not license_path.is_file():
        with urlopen(f"https://raw.githubusercontent.com/nodejs/node/{version}/LICENSE", timeout=20) as response:
            license_text = response.read()
        license_path.parent.mkdir(parents=True, exist_ok=True)
        license_path.write_bytes(license_text)
    return [(node, ".")], [(str(license_path), "licenses/node")]
