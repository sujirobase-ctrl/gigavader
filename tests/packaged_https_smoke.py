"""Create a verified TLS context using SSL code/native libraries from the EXE."""

import sys
import os
from pathlib import Path


def child(bundle):
    os.environ["PATH"] = str(bundle) + os.pathsep + os.environ.get("PATH", "")
    dll_handle = os.add_dll_directory(str(bundle)) if hasattr(os, "add_dll_directory") else None
    sys.path.insert(0, str(bundle))
    import ssl
    import _ssl
    assert Path(ssl.__file__).is_relative_to(bundle), ssl.__file__
    assert Path(_ssl.__file__).is_relative_to(bundle), _ssl.__file__
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    assert context.verify_mode == ssl.CERT_REQUIRED and context.check_hostname
    context.load_verify_locations(cafile=str(bundle / 'certifi' / 'cacert.pem'))
    assert context.cert_store_stats()['x509_ca'] > 0
    print('PACKAGED HTTPS PASS: actual SSL runtime creates verified TLS context and loads certificates')
    if dll_handle is not None:
        dll_handle.close()


def parent(executable):
    import importlib._bootstrap_external
    import subprocess
    from tempfile import TemporaryDirectory
    from PyInstaller.archive.readers import CArchiveReader
    archive = CArchiveReader(str(executable))
    pyz = archive.open_embedded_archive('PYZ.pyz')
    with TemporaryDirectory(prefix='buidl-https-test-') as directory:
        bundle = Path(directory)
        for name in archive.toc:
            normalized = name.replace('\\', '/')
            basename = Path(normalized).name.lower()
            ssl_dll = basename.startswith(('libcrypto-', 'libssl-')) and basename.endswith('.dll')
            if basename == '_ssl.pyd' or ssl_dll or normalized in (
                    'certifi/cacert.pem', 'codex-rejected-cas.pem'):
                target = bundle / normalized
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(archive.extract(name))
        (bundle/'ssl.pyc').write_bytes(importlib._bootstrap_external._code_to_timestamp_pyc(pyz.extract('ssl')))
        subprocess.run([sys.executable, '-I', str(Path(__file__).resolve()), '--child', str(bundle)], check=True, timeout=30)


if __name__ == '__main__':
    if sys.argv[1] == '--child':
        child(Path(sys.argv[2]).resolve())
    else:
        parent(Path(sys.argv[1]).resolve(strict=True))
