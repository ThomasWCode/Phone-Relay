#!/usr/bin/env python3
"""Builds phone-relay-helper.jar (classes.dex) from src/ with the Android SDK.

Needs a JDK (javac, 17 or later) and the Android SDK: a platform's android.jar and
build-tools' d8. The SDK is found from ANDROID_HOME, ANDROID_SDK_ROOT or the usual
install folders; the newest platform and build-tools are used.

    python phone/helper/build.py           # writes phone/helper/phone-relay-helper.jar
    python phone/helper/build.py --check   # exits 1 if the jar does not match src/

The jar carries source.sha256, the hash of src/, so a test can tell a stale jar
from a fresh one without the SDK. Rebuild and commit the jar after changing src/.
"""

import argparse
import glob
import hashlib
import os
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(HERE, "src")
JAR = os.path.join(HERE, "phone-relay-helper.jar")
MIN_API = 30  # Android 11: what the helper's APIs need


def source_hash() -> str:
    """sha256 over src/'s .java files: their paths and contents, line endings made LF."""
    h = hashlib.sha256()
    for path in sorted(glob.glob(os.path.join(SRC, "**", "*.java"), recursive=True)):
        rel = os.path.relpath(path, SRC).replace(os.sep, "/")
        with open(path, "rb") as f:
            h.update(rel.encode() + b"\0" + f.read().replace(b"\r\n", b"\n") + b"\0")
    return h.hexdigest()


def jar_hash(path: str = JAR) -> str | None:
    if not os.path.exists(path):
        return None
    with zipfile.ZipFile(path) as z:
        return z.read("source.sha256").decode().strip()


def _version_key(name: str) -> tuple:
    return tuple(int(p) for p in re.findall(r"\d+", name))


def find_sdk() -> tuple[str, str]:
    """(android.jar, d8) from the newest platform and build-tools."""
    roots = [os.environ.get("ANDROID_HOME"), os.environ.get("ANDROID_SDK_ROOT"),
             os.path.join(os.environ.get("LOCALAPPDATA", ""), "Android", "Sdk"),
             os.path.expanduser("~/Android/Sdk"), os.path.expanduser("~/Library/Android/sdk")]
    for root in filter(None, roots):
        jars = glob.glob(os.path.join(root, "platforms", "android-*", "android.jar"))
        d8s = glob.glob(os.path.join(root, "build-tools", "*", "d8.bat" if os.name == "nt" else "d8"))
        if jars and d8s:
            jar = max(jars, key=lambda p: _version_key(os.path.basename(os.path.dirname(p))))
            d8 = max(d8s, key=lambda p: _version_key(os.path.basename(os.path.dirname(p))))
            return jar, d8
    sys.exit("Android SDK not found: set ANDROID_HOME (it needs platforms/*/android.jar and build-tools/*/d8)")


def build() -> None:
    android_jar, d8 = find_sdk()
    javac = shutil.which("javac") or sys.exit("javac not found: install a JDK (17 or later)")
    sources = sorted(glob.glob(os.path.join(SRC, "**", "*.java"), recursive=True))
    with tempfile.TemporaryDirectory() as tmp:
        classes, dex = os.path.join(tmp, "classes"), os.path.join(tmp, "dex")
        os.makedirs(classes)
        os.makedirs(dex)
        # Java 8: the newest level javac still compiles against another boot class path (android.jar).
        subprocess.run([javac, "-source", "8", "-target", "8", "-Xlint:-options", "-encoding", "UTF-8",
                        "-bootclasspath", android_jar, "-d", classes, *sources], check=True)
        class_files = sorted(glob.glob(os.path.join(classes, "**", "*.class"), recursive=True))
        subprocess.run([d8, "--release", "--min-api", str(MIN_API), "--lib", android_jar,
                        "--output", dex, *class_files], check=True)
        with open(os.path.join(dex, "classes.dex"), "rb") as f:
            dex_bytes = f.read()
    # Fixed timestamps, so the same dex always makes the same jar.
    with zipfile.ZipFile(JAR, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data in (("classes.dex", dex_bytes), ("source.sha256", source_hash().encode() + b"\n")):
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            z.writestr(info, data)
    print(f"built {JAR} ({os.path.getsize(JAR)} bytes) with {os.path.basename(os.path.dirname(android_jar))}, "
          f"build-tools {os.path.basename(os.path.dirname(d8))}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--check", action="store_true", help="only check the jar matches src/")
    if parser.parse_args().check:
        fresh = jar_hash() == source_hash()
        print("helper jar matches src/" if fresh else "helper jar is stale: run python phone/helper/build.py")
        return 0 if fresh else 1
    build()
    return 0


if __name__ == "__main__":
    sys.exit(main())
