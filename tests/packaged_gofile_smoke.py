"""Verify that the packaged GoFile resolver parses a literal without running JavaScript."""
import sys
import types
from pathlib import Path

from PyInstaller.archive.readers import CArchiveReader

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def main(executable):
    archive = CArchiveReader(str(executable))
    assert "node.exe" not in {name.lower() for name in archive.toc}
    pyz = archive.open_embedded_archive("PYZ.pyz")
    module = types.ModuleType("packaged_resolvers")
    sys.modules[module.__name__] = module
    try:
        exec(pyz.extract("src.resolvers"), module.__dict__)
        source = "throw Error('never execute'); function generateWT(){return '::bundled_salt123';}"
        client = types.SimpleNamespace(timeout=10, get=lambda *args, **kwargs: types.SimpleNamespace(text=source))
        assert module._discover_gofile_salt(client, force=True) == "bundled_salt123"
        print("PACKAGED GOFILE PASS: static extraction without Node")
    finally:
        del sys.modules[module.__name__]


if __name__ == "__main__":
    main(Path(sys.argv[1]).resolve(strict=True))
