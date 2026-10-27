"""Owner-private image tuples and recoverable writable generations."""

import contextlib
import fcntl
import hashlib
import json
import os
import re
import shutil
import subprocess
import uuid
from pathlib import Path

MIN_FREE_SPACE = 2 * 1024**3


def home():
    data = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share"))
    root = Path(os.environ.get("MACDESK_HOME", data / "macdesk"))
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    if root.is_symlink() or root.stat().st_uid != os.getuid():
        raise ValueError("MACDESK_HOME must be a directory owned by this user")
    root.chmod(0o700)
    return root.resolve()


def name(value):
    if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,47}", value):
        raise ValueError("names must be 1–48 letters, digits, underscores or hyphens")
    return value


def instance(value):
    path = home() / "instances" / name(value)
    if not (path / "config.json").is_file():
        raise ValueError(f"instance {value!r} does not exist; create it first")
    return path


def load(path):
    return json.loads(Path(path).read_text())


def save(path, body):
    path = Path(path)
    stage = path.with_name("." + path.name + "." + uuid.uuid4().hex)
    try:
        fd = os.open(stage, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as output:
            json.dump(body, output, indent=2)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(stage, path)
        fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    finally:
        stage.unlink(missing_ok=True)


@contextlib.contextmanager
def lease(path, blocking=True):
    with Path(path).open("a") as handle:
        flags = fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB)
        try:
            fcntl.flock(handle, flags)
        except BlockingIOError:
            raise RuntimeError("instance is already owned by a running runtime") from None
        yield


def readonly_file(path):
    path = Path(path).resolve(strict=True)
    if not path.is_file() or path.stat().st_mode & 0o222:
        raise ValueError(f"image input must be a read-only regular file: {path}")
    return str(path)


def sha256(path):
    with Path(path).open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def import_image(image_name, disk, disk_format, aux, firmware, ecid, qemu_img, access=None):
    target = home() / "images"
    target.mkdir(mode=0o700, exist_ok=True)
    path = target / (name(image_name) + ".json")
    with lease(target / ".lock"):
        if path.exists():
            raise ValueError("image name already exists")
        inputs = {
            key: readonly_file(value)
            for key, value in (("disk", disk), ("aux", aux), ("firmware", firmware))
        }
        if not 0 < ecid < 2**64 or Path(inputs["aux"]).stat().st_size != 32 * 1024**2:
            raise ValueError("expected a valid ECID and a 32 MiB AUX payload")
        # Preserve the complete dependency chain of an imported overlay image.
        result = subprocess.run(
            [
                qemu_img,
                "info",
                "--output=json",
                "--backing-chain",
                "-f",
                disk_format,
                inputs["disk"],
            ],
            check=True,
            capture_output=True,
            timeout=60,
        )
        chain = json.loads(result.stdout)
        dependencies = []
        for item in chain:
            file = readonly_file(item["filename"])
            dependencies.append({"path": file, "sha256": sha256(file)})
        save(
            path,
            {
                **inputs,
                "format": disk_format,
                "ecid": ecid,
                "sha256": {key: sha256(value) for key, value in inputs.items() if key != "disk"},
                "dependencies": dependencies,
                "access": access,
            },
        )


def new_generation(path, image, qemu_img):
    if shutil.disk_usage(path).free < MIN_FREE_SPACE + Path(image["aux"]).stat().st_size:
        raise RuntimeError(
            "at least 2 GiB free space plus AUX storage is required for a new overlay"
        )
    generation = "generation-" + uuid.uuid4().hex
    stage = path / generation
    stage.mkdir(mode=0o700)
    try:
        # References are immutable; do not copy a large base just to create an instance.
        for dependency in image["dependencies"]:
            readonly_file(dependency["path"])
        readonly_file(image["aux"])
        shutil.copyfile(image["aux"], stage / "aux.img")
        os.chmod(stage / "aux.img", 0o600)
        subprocess.run(
            [
                qemu_img,
                "create",
                "-f",
                "qcow2",
                "-F",
                image["format"],
                "-b",
                image["disk"],
                str(stage / "root.qcow2"),
            ],
            check=True,
            capture_output=True,
            timeout=60,
        )
        os.chmod(stage / "root.qcow2", 0o600)
        previous = load(path / "active.json") if (path / "active.json").exists() else None
        save(
            path / "active.json",
            {
                "generation": generation,
                "previous": previous["generation"] if previous else None,
                "enrolled": False,
            },
        )
    except BaseException:
        # A directory fsync can fail after publishing the pointer. Keep any
        # generation the active pointer may already reference.
        active = load(path / "active.json") if (path / "active.json").exists() else {}
        if active.get("generation") != generation:
            shutil.rmtree(stage)
        raise
    return stage


def seal(path, image_name):
    name(image_name)
    manifest = home() / "images" / (image_name + ".json")
    if manifest.exists():
        raise ValueError("image name already exists")
    config = load(path / "config.json")
    active = load(path / "active.json")
    if not (path / "shutdown.json").is_file():
        raise ValueError("seal requires a verified clean guest shutdown; run stop first")
    proof = load(path / "shutdown.json")
    if not proof["clean"] or proof["generation"] != active["generation"]:
        raise ValueError("seal requires a verified clean shutdown of the active generation")
    image = load(home() / "images" / (config["image"] + ".json"))
    source = path / active["generation"]
    required = (source / "root.qcow2").stat().st_blocks * 512 + 32 * 1024**2
    if shutil.disk_usage(path).free < required + MIN_FREE_SPACE:
        raise RuntimeError("insufficient space to seal while preserving the 2 GiB reserve")
    files = home() / "images" / (image_name + "-" + uuid.uuid4().hex)
    files.mkdir(mode=0o700)
    try:
        subprocess.run(
            [
                config["qemu_img"],
                "convert",
                "-f",
                "qcow2",
                "-O",
                "qcow2",
                "-B",
                image["disk"],
                "-F",
                image["format"],
                str(source / "root.qcow2"),
                str(files / "root.qcow2"),
            ],
            check=True,
            capture_output=True,
        )
        subprocess.run(
            [config["qemu_img"], "check", "-f", "qcow2", str(files / "root.qcow2")],
            check=True,
            capture_output=True,
        )
        subprocess.run(
            [
                config["qemu_img"],
                "compare",
                "-f",
                "qcow2",
                "-F",
                "qcow2",
                str(source / "root.qcow2"),
                str(files / "root.qcow2"),
            ],
            check=True,
            capture_output=True,
        )
        shutil.copyfile(source / "aux.img", files / "aux.img")
        shutil.copyfile(config["key"], files / "access.key")
        (files / "access.key").chmod(0o600)
        for filename in ("root.qcow2", "aux.img"):
            (files / filename).chmod(0o400)
        access = {
            "user": config["user"],
            "key": str(files / "access.key"),
            "guest_uid": config["guest_uid"],
            "guest_gid": config["guest_gid"],
        }
        import_image(
            image_name,
            files / "root.qcow2",
            "qcow2",
            files / "aux.img",
            image["firmware"],
            image["ecid"],
            config["qemu_img"],
            access,
        )
    except BaseException:
        # Publication may have completed before a final directory fsync failed.
        published = load(manifest) if manifest.exists() else {}
        if published.get("disk") != str(files / "root.qcow2"):
            shutil.rmtree(files)
        raise
