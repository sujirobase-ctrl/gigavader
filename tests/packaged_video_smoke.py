"""Decode with the actual av modules/DLLs embedded in a built GUI executable."""

from __future__ import annotations

import importlib._bootstrap_external
import io
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory


def decode_from_bundle(bundle):
    sys.path.insert(0, str(bundle))
    import av

    assert Path(av.__file__).is_relative_to(bundle), av.__file__
    assert Path(av._core.__file__).is_relative_to(bundle), av._core.__file__
    with av.open(bundle / "sample.mp4") as container:
        frames = [frame.to_image() for frame in container.decode(video=0)]
    assert len(frames) == 16 and frames[0].size == (160, 90)
    assert frames[0].getpixel((0, 0)) != frames[-1].getpixel((0, 0))
    print(f"PACKAGED VIDEO PASS: {len(frames)} frames; PyAV {av.__version__} from EXE")


def run(executable):
    import av
    from PIL import Image
    from PyInstaller.archive.readers import CArchiveReader

    executable = executable.resolve(strict=True)
    archive = CArchiveReader(str(executable))
    pyz = archive.open_embedded_archive("PYZ.pyz")
    assert "src.video_preview" in pyz.toc and "src.video_preview_gui" in pyz.toc
    with TemporaryDirectory(prefix="packaged-video-", dir=executable.parent.parent / "build") as folder:
        bundle = Path(folder)

        def put(name, data):
            path = (bundle / name).resolve()
            if not path.is_relative_to(bundle):
                raise ValueError("Unexpected path in executable")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)

        native_count = 0
        for name in archive.toc:
            if name.replace("\\", "/").startswith(("av/", "av.libs/")):
                put(name, archive.extract(name))
                native_count += 1
        assert native_count > 10
        for name, entry in pyz.toc.items():
            if name == "av" or name.startswith("av."):
                path = name.replace(".", "/") + ("/__init__.pyc" if entry[0] == 1 else ".pyc")
                put(path, importlib._bootstrap_external._code_to_timestamp_pyc(pyz.extract(name)))

        output = io.BytesIO()
        with av.open(output, "w", format="mp4") as container:
            stream = container.add_stream("mpeg4", rate=8)
            stream.width, stream.height, stream.pix_fmt = 160, 90, "yuv420p"
            for index in range(16):
                frame = av.VideoFrame.from_image(Image.new("RGB", (160, 90), (index * 15, 40, 180)))
                for packet in stream.encode(frame):
                    container.mux(packet)
            for packet in stream.encode():
                container.mux(packet)
        put("sample.mp4", output.getvalue())
        # Fresh interpreter: cannot reuse codecs imported to generate the fixture.
        subprocess.run([sys.executable, "-I", str(Path(__file__).resolve()), "--decode", str(bundle)],
                       check=True, timeout=30)


if __name__ == "__main__":
    if sys.argv[1] == "--decode":
        decode_from_bundle(Path(sys.argv[2]))
    else:
        run(Path(sys.argv[1]))
