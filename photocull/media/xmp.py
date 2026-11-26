from __future__ import annotations

import errno
import fcntl
import hashlib
import os
import secrets
import stat
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from sqlite3 import Row
from xml.dom import Node, minidom
from xml.parsers.expat import ExpatError

from defusedxml.common import DefusedXmlException
from defusedxml.minidom import parseString

from photocull.config import Settings
from photocull.db import connect
from photocull.media.walk import EXTENSIONS, MediaJail, OutsideLibrary, open_directory, source_key

XMP = "http://ns.adobe.com/xap/1.0/"
RDF = "http://www.w3.org/1999/02/22-rdf-syntax-ns#"
XMLNS = "http://www.w3.org/2000/xmlns/"
MAX_BYTES = 16 * 1024 * 1024
PACKET = f"""<?xpacket begin="\ufeff" id="W5M0MpCehiHzreSzNTczkc9d"?>
<x:xmpmeta xmlns:x="adobe:ns:meta/">
  <rdf:RDF xmlns:rdf="{RDF}">
    <rdf:Description rdf:about="" xmlns:xmp="{XMP}"/>
  </rdf:RDF>
</x:xmpmeta>
<?xpacket end="w"?>"""


class XMPError(ValueError):
    pass


class XMPConflict(XMPError):
    pass


@dataclass(frozen=True)
class XMPPlan:
    sidecar: str
    rating: int
    action: str
    confirmation: str


def _namespace(element: minidom.Element, prefix: str) -> str | None:
    current = element
    while current.nodeType == Node.ELEMENT_NODE:
        if current.hasAttributeNS(XMLNS, prefix):
            return current.getAttributeNS(XMLNS, prefix)
        current = current.parentNode
    return None


def _set_property(node: minidom.Element, value: str) -> bool:
    children = [child for child in node.childNodes if child.nodeType == Node.ELEMENT_NODE]
    if children:
        rdf_values = node.getElementsByTagNameNS(RDF, "value")
        if len(rdf_values) == 1:
            return _set_property(rdf_values[0], value)
        raise XMPError("Existing xmp:Rating has an unsupported RDF structure")
    if node.hasAttributeNS(RDF, "resource"):
        raise XMPError("Existing xmp:Rating is an RDF resource")
    text = [
        child
        for child in node.childNodes
        if child.nodeType in (Node.TEXT_NODE, Node.CDATA_SECTION_NODE)
    ]
    if "".join(child.data for child in text).strip() == value:
        return False
    if text:
        text[0].data = value
        for child in text[1:]:
            node.removeChild(child)
    else:
        node.appendChild(node.ownerDocument.createTextNode(value))
    return True


def _render(existed: bytes | None, rating: int) -> bytes | None:
    try:
        document = parseString(PACKET if existed is None else existed, forbid_dtd=True)
    except (DefusedXmlException, ExpatError, UnicodeError, LookupError) as error:
        raise XMPError(f"Existing sidecar is invalid or unsupported XML: {error}") from error

    value = str(rating)
    with document:
        roots = document.getElementsByTagNameNS(RDF, "RDF")
        if not roots:
            raise XMPError("Existing sidecar has no rdf:RDF element")
        descriptions = [
            node
            for node in roots[0].childNodes
            if (node.namespaceURI, node.localName) == (RDF, "Description")
        ]
        if not descriptions:
            if any(node.nodeType == Node.ELEMENT_NODE for node in roots[0].childNodes):
                raise XMPError("Existing sidecar has no rdf:Description element")
            prefix = roots[0].prefix
            qualified = f"{prefix}:Description" if prefix else "Description"
            description = document.createElementNS(RDF, qualified)
            description.setAttributeNS(XMLNS, "xmlns:rdf", RDF)
            description.setAttributeNS(RDF, "rdf:about", "")
            roots[0].appendChild(description)
            descriptions = [description]

        primary = next(
            (node for node in descriptions if not node.getAttributeNS(RDF, "about")),
            descriptions[0],
        )
        subject = primary.getAttributeNS(RDF, "about")
        present = False
        changed = False
        for node in descriptions:
            if node.getAttributeNS(RDF, "about") != subject:
                continue
            attribute = node.getAttributeNodeNS(XMP, "Rating")
            if attribute is not None:
                present = True
                if attribute.value.strip() != value:
                    attribute.value = value
                    changed = True
            for child in node.childNodes:
                if (child.namespaceURI, child.localName) == (XMP, "Rating"):
                    present = True
                    changed = _set_property(child, value) or changed

        if not present:
            prefix = "xmp"
            counter = 0
            while (bound := _namespace(primary, prefix)) not in (None, XMP):
                counter += 1
                prefix = f"xmp{counter}"
            if bound is None:
                primary.setAttributeNS(XMLNS, f"xmlns:{prefix}", XMP)
            primary.setAttributeNS(XMP, f"{prefix}:Rating", value)
            changed = True

        return document.toxml(encoding="UTF-8") if changed or existed is None else None


def _read(parent: int, name: str) -> tuple[bytes | None, os.stat_result | None]:
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
    except FileNotFoundError:
        return None, None
    except OSError as error:
        if error.errno == errno.ELOOP:
            raise XMPError(f"Sidecar is a symbolic link: {name}") from error
        raise
    with os.fdopen(fd, "rb") as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode):
            raise XMPError(f"Sidecar is not a regular file: {name}")
        data = handle.read(MAX_BYTES + 1)
        if len(data) > MAX_BYTES:
            raise XMPError(f"Sidecar exceeds {MAX_BYTES // (1024 * 1024)} MiB: {name}")
        if source_key(info) != source_key(os.fstat(handle.fileno())):
            raise XMPConflict("Sidecar changed while reading. Preview again.")
        return data, info


def _check_source(jail: MediaJail, path: str, target: Path, indexed_key: str) -> None:
    with jail.open_media(path) as (fd, current):
        if target != current or source_key(os.fstat(fd)) != indexed_key:
            raise XMPConflict("Media changed after indexing. Scan the library again.")


def _check_sidecar(parent: int, name: str, previous: os.stat_result | None) -> None:
    try:
        current = os.stat(name, dir_fd=parent, follow_symlinks=False)
    except FileNotFoundError:
        current = None
    if (current is None) != (previous is None) or (
        current is not None and previous is not None and source_key(current) != source_key(previous)
    ):
        raise XMPConflict("Sidecar changed after preview. Preview again.")


def _write(
    parent: int,
    name: str,
    data: bytes,
    previous: os.stat_result | None,
    validate_source: Callable[[], None],
) -> None:
    temp = f".photocull-xmp-{secrets.token_hex(12)}.tmp"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
    fd = os.open(temp, flags, 0o666 if previous is None else 0o600, dir_fd=parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            if previous is not None:
                os.fchmod(handle.fileno(), stat.S_IMODE(previous.st_mode) & 0o777)
            os.fsync(handle.fileno())
        validate_source()
        _check_sidecar(parent, name, previous)
        os.replace(temp, name, src_dir_fd=parent, dst_dir_fd=parent)
    finally:
        with suppress(FileNotFoundError):
            os.unlink(temp, dir_fd=parent)


def _plan(
    jail: MediaJail,
    asset: Row,
    target: Path,
    *,
    dry_run: bool,
    confirmation: str | None,
) -> XMPPlan:
    path = asset["path"]
    sidecar = target.with_suffix(".xmp")
    if target.suffix[1:].casefold() not in EXTENSIONS:
        raise XMPError(f"Resolved original has an unsupported extension: {target.name}")

    def validate_source() -> None:
        _check_source(jail, path, target, asset["source_key"])

    validate_source()
    rating = 1 if asset["rejected"] else asset["rating"]
    with open_directory(target.parent) as parent:
        with os.scandir(parent) as siblings:
            for sibling in siblings:
                other = Path(sibling.name)
                if (
                    other.name != target.name
                    and other.stem == target.stem
                    and other.suffix[1:].casefold() in EXTENSIONS
                    and sibling.is_file(follow_symlinks=False)
                ):
                    raise XMPError(f"{other.name} shares the same sidecar name as {target.name}")
        previous, info = _read(parent, sidecar.name)
        rendered = _render(previous, rating)
        action = "unchanged" if rendered is None else ("update" if info else "create")
        fingerprint = hashlib.sha256(
            f"{sidecar}\0{asset['source_key']}\0{asset['revision']}\0{rating}\0".encode()
            + (previous or b"")
        ).hexdigest()
        if confirmation is not None and confirmation != fingerprint:
            raise XMPConflict("Rating, original, or sidecar changed after preview. Preview again.")
        if rendered is not None:
            if not os.access(".", os.W_OK | os.X_OK, dir_fd=parent, effective_ids=True):
                raise XMPError(f"Sidecar directory is not writable: {sidecar.parent}")
            if not dry_run:
                _write(parent, sidecar.name, rendered, info, validate_source)
    return XMPPlan(str(sidecar), rating, action, fingerprint)


def export_xmp(
    settings: Settings,
    library: str,
    asset_id: int,
    *,
    dry_run: bool,
    confirmation: str | None = None,
) -> XMPPlan:
    state = settings.library_state(library)
    directory = settings.libraries[library].root
    try:
        # This lock covers CLI and API exports. Worker transactions stay independent.
        with (settings.state_dir / "xmp.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            with connect(state / "index.db") as db:
                asset = db.execute(
                    """SELECT path, source_key, rating, rejected, revision FROM assets
                       WHERE id = ? AND active = 1""",
                    (asset_id,),
                ).fetchone()
            if asset is None:
                raise LookupError(f"Unknown asset {asset_id}")
            jail = MediaJail(settings.libraries[library], settings.state_dir)
            target = jail.locate(asset["path"])
            directory = target.parent
            return _plan(jail, asset, target, dry_run=dry_run, confirmation=confirmation)
    except (OSError, OutsideLibrary, RuntimeError) as error:
        if isinstance(error, OSError) and error.errno in (errno.EACCES, errno.EPERM, errno.EROFS):
            raise XMPError(f"Cannot access or write a sidecar in {directory}: {error}") from error
        raise XMPError(f"Cannot access the original or sidecar: {error}") from error
