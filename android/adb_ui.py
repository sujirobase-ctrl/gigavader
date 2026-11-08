"""Small ADB-only UI test driver, scoped to the chosen emulator."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from pathlib import Path

_configured_adb = os.environ.get("ANDROID_ADB")
_sdk_adb = Path(os.environ.get("ANDROID_HOME", "")) / "platform-tools" / "adb.exe"
ADB = Path(_configured_adb or shutil.which("adb") or (_sdk_adb if _sdk_adb.is_file() else "adb"))
SERIAL = "emulator-5554"
QA = Path(__file__).parent / "qa"


def adb(*args, binary=False):
    result = subprocess.run([str(ADB), "-s", SERIAL, *args], capture_output=True, timeout=25)
    # MuMu 15 sometimes crashes the uiautomator process during shutdown,
    # after successfully writing the hierarchy. Accept only that exact case.
    if not ("uiautomator" in args and result.returncode in (135, 139) and b"hierchary dumped" in result.stdout):
        result.check_returncode()
    return result.stdout if binary else result.stdout.decode("utf-8", errors="replace")


def dump():
    adb("shell", "uiautomator", "dump", "/data/local/tmp/buidl-ui.xml")
    xml = adb("exec-out", "cat", "/data/local/tmp/buidl-ui.xml")
    QA.mkdir(exist_ok=True)
    (QA / "ui.xml").write_text(xml, encoding="utf-8")
    return ET.fromstring(xml)


def tap(label, scroll=False):
    for attempt in range(15 if scroll else 1):
        root = dump()
        for node in root.iter("node"):
            if label in (node.get("text"), node.get("content-desc"), node.get("resource-id", "").rsplit("/", 1)[-1]) and node.get("enabled") == "true":
                x1, y1, x2, y2 = map(int, re.findall(r"\d+", node.get("bounds")))
                if x2 > x1 and y2 > y1:
                    adb("shell", "input", "tap", str((x1+x2)//2), str((y1+y2)//2))
                    print("Tapped:", label)
                    return
        if scroll:
            container = next((n for n in root.iter("node") if n.get("scrollable") == "true"), None)
            if container is None:
                break
            x1, y1, x2, y2 = map(int, re.findall(r"\d+", container.get("bounds")))
            adb("shell", "input", "swipe", str((x1+x2)//2), str(y1+(y2-y1)*3//4), str((x1+x2)//2), str(y1+(y2-y1)//3), "650")
            time.sleep(0.15)
    raise AssertionError("Control not found: " + label)


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    action = sys.argv[1]
    if action == "dump":
        for node in dump().iter("node"):
            if node.get("text") or node.get("content-desc"):
                print(node.get("class", "").split(".")[-1], repr(node.get("text")), repr(node.get("content-desc")), node.get("bounds"))
    elif action == "tap":
        tap(sys.argv[2], "--scroll" in sys.argv)
    elif action == "text":
        adb("shell", "input", "text", sys.argv[2])
    elif action == "screenshot":
        QA.mkdir(exist_ok=True)
        target = QA / (sys.argv[2] + ".png")
        target.write_bytes(adb("exec-out", "screencap", "-p", binary=True))
        print(target)
    elif action == "test-screenshots":
        for name in ("qa-gallery.png", "qa-video.png", "qa-video-fallback.png", "qa-failures.png"):
            (QA / name).write_bytes(adb("exec-out", "run-as", "com.sujirobase.buidl", "cat", "files/"+name, binary=True))
            print(QA / name)
    elif action == "qa-screenshots":
        QA.mkdir(exist_ok=True)
        names = adb("shell", "run-as", "com.sujirobase.buidl.qa", "ls", "files").splitlines()
        for name in names:
            if re.fullmatch(r"(?:ui-|qa-)[a-z0-9-]+\.png", name):
                (QA / name).write_bytes(adb("exec-out", "run-as", "com.sujirobase.buidl.qa", "cat", "files/" + name, binary=True))
                print(QA / name)
