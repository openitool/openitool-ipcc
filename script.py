import asyncio
import glob
import hashlib
import json
import logging
import shutil
import subprocess
import tarfile
import zipfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Callable, Dict, Generic, List, Optional, TypeVar, Union

import aiohttp
from tqdm.asyncio import tqdm

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

IPHONES_PRODUCT_CODES: List[str] = [
    "7,1",
    "7,2",
    "8,1",
    "8,2",
    "8,4",
    "9,1",
    "9,2",
    "9,3",
    "9,4",
    "10,1",
    "10,2",
    "10,4",
    "10,4",
    "10,5",
    "10,6",
    "11,2",
    "11,4",
    "11,6",
    "11,8",
    "12,1",
    "12,3",
    "12,5",
    "12,8",
    "13,1",
    "13,2",
    "13,3",
    "13,4",
    "14,2",
    "14,3",
    "14,4",
    "14,5",
    "14,6",
]


T = TypeVar("T")
E = TypeVar("E")

# for json writing callbacks
J = TypeVar("J")


@dataclass
class Ok(Generic[T]):
    value: T


@dataclass
class Error(Generic[E]):
    error: E


Result = Union[Ok[T], Error[E]]


@dataclass
class Firmwares:
    identifier: str
    version: str
    buildid: str
    sha1sum: str
    md5sum: str
    filesize: int
    url: str
    releasedate: datetime
    uploaddate: datetime
    signed: bool

    @staticmethod
    def from_dict(data: dict) -> "Firmwares":
        return Firmwares(
            identifier=data["identifier"],
            version=data["version"],
            buildid=data["buildid"],
            sha1sum=data["sha1sum"],
            md5sum=data["md5sum"],
            filesize=data["filesize"],
            url=data["url"],
            releasedate=datetime.fromisoformat(data["releasedate"]),
            uploaddate=datetime.fromisoformat(data["uploaddate"]),
            signed=data["signed"],
        )


@dataclass
class Response:
    name: str
    identifier: str
    firmwares: List[Firmwares]
    boardconfig: str
    platform: str
    cpid: int
    bdid: int

    @staticmethod
    def from_dict(data: dict) -> "Response":
        return Response(
            name=data["name"],
            identifier=data["identifier"],
            firmwares=[Firmwares.from_dict(fw) for fw in data["firmwares"]],
            boardconfig=data["boardconfig"],
            platform=data["platform"],
            cpid=data["cpid"],
            bdid=data["bdid"],
        )


async def calculate_hash(file_path: Path) -> str:
    hash_func = hashlib.sha1()
    with file_path.open("rb") as file:
        for chunk in iter(lambda: file.read(4096), b""):
            hash_func.update(chunk)

    return hash_func.hexdigest()


async def download_file(
    firmware: Firmwares, version_folder: Path, session: aiohttp.ClientSession
) -> Result[Path, str]:
    file_path = version_folder / f"{firmware.identifier}-{firmware.version}.ipsw"

    if file_path.exists():
        if (await calculate_hash(file_path)) == firmware.sha1sum:
            return Ok(file_path)

        logger.info("Detected a corrupted file, redownloading")
        file_path.unlink()

    try:
        async with session.get(
            firmware.url, timeout=aiohttp.ClientTimeout(1000)
        ) as response:
            if response.status != 200:
                return Error(
                    f"Failed to download {firmware.identifier}: {response.status} {response.reason}"
                )

            total_size = int(response.headers.get("Content-Length", 0))

            with (
                open(file_path, "wb") as file,
                tqdm(
                    total=total_size, unit="B", unit_scale=True, desc=str(file_path)
                ) as progress,
            ):
                async for chunk in response.content.iter_chunked(8192):
                    file.write(chunk)
                    progress.update(len(chunk))

    except aiohttp.ClientError as e:
        return Error(f"Network error: {e}")

    except Exception as e:
        return Error(f"Error at downloading: {e}")

    finally:
        file_path.unlink(missing_ok=True)  # remove partially downloaded file on error

    if (await calculate_hash(file_path)) != firmware.sha1sum:
        return Error(f"Hash mismatch for {file_path}")

    return Ok(file_path)


async def put_metadata(
    metadata_path: Path, key: str, callback: Callable[[Optional[J]], J]
) -> Result[None, str]:
    """Read & update JSON metadata using a callback."""
    try:
        metadata = json.loads(metadata_path.read_text() or "{}")
        metadata[key] = callback(metadata.get(key))
        metadata["updated_at"] = datetime.now(UTC).isoformat()
        metadata_path.write_text(json.dumps(metadata, indent=4))
    except json.JSONDecodeError as e:
        return Error(f"Invalid JSON: {e}")
    return Ok(None)


async def decrypt_dmg_aea(
    ipsw_file: Path, dmg_file: Path, output: Path
) -> Result[None, str]:
    logger.info(f"decrypting {dmg_file}")

    if shutil.which("ipsw") is None:
        logger.warning("ipsw is not installed")
        deb_path = Path("ipsw.deb")

        if not deb_path.exists():
            subprocess.run(
                [
                    "wget",
                    "https://github.com/blacktop/ipsw/releases/download/v3.1.544/ipsw_3.1.544_linux_x86_64.deb",
                    "--output-document",
                    str(deb_path),
                ],
                check=True,
            )

        subprocess.run(["dpkg", "-i", str(deb_path)], check=True)

    try:
        subprocess.run(
            ["ipsw", "extract", "--fcs-key", str(ipsw_file), "--output", str(output)],
            stderr=subprocess.PIPE,
            text=True,
            check=True,
        )
    except subprocess.CalledProcessError as e:
        return Error(f"Extraction failed: {e.stderr}")

    pem_files = [Path(p) for p in glob.glob(f"{output}/**/*.pem")]

    matching_pem = next((pem for pem in pem_files if pem.stem == dmg_file.name), None)

    if matching_pem:
        logger.info(f"Found a matching PEM file: {matching_pem}")
        pem_file = matching_pem
    else:
        if pem_files:
            logger.warning("No matched PEM, using the first one")
            pem_file = pem_files[0]
        else:
            return Error("No PEM file found.")

    try:
        logger.info("Decrypting")
        # extraction_process = await asyncio.create_subprocess_exec("ipsw", "fw", "aea", "--pem", str(pem_file), "--output", str(output), stderr=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE)
        #
        # while extraction_process.returncode is None:
        #
        subprocess.run(
            [
                "ipsw",
                "fw",
                "aea",
                "--pem",
                str(pem_file),
                str(dmg_file),
                "--output",
                str(output),
            ],
            text=True,
            stderr=subprocess.PIPE,
            check=True,
        )
    except subprocess.CalledProcessError as e:
        return Error(f"Decryption failed: {e.stderr}")

    # we don't need the .dmg.aea
    dmg_file.unlink(missing_ok=True)

    # we don't also need the .pem folder that was created by `ipsw`
    shutil.rmtree(pem_file.parent)

    return Ok(None)


async def extract_the_biggest_dmg(file: Path, output: Path) -> Result[None, str]:
    logger.info(f"Extracting the biggest DMG from {file}")

    with zipfile.ZipFile(file) as zip_file:
        biggest_dmg = max(zip_file.infolist(), key=lambda x: x.file_size)
        biggest_dmg_file_path = output / biggest_dmg.filename
        logger.debug(
            f"Biggest DMG found: {biggest_dmg.filename} ({biggest_dmg.file_size} bytes)"
        )

        if (
            not biggest_dmg_file_path.exists()
            or biggest_dmg_file_path.stat().st_size != biggest_dmg.file_size
        ):
            logger.info(f"Extracting {biggest_dmg.filename} to {output}")
            with (
                zip_file.open(biggest_dmg) as source,
                open(biggest_dmg_file_path, "wb") as target,
                tqdm(
                    total=biggest_dmg.file_size,
                    unit="B",
                    unit_scale=True,
                    desc=f"Extracting {biggest_dmg.filename}",
                ) as progress,
            ):
                while True:
                    chunk = source.read(8192)
                    if not chunk:
                        break

                    target.write(chunk)
                    progress.update(len(chunk))

            zip_file.extract(biggest_dmg, path=output)
        else:
            logger.info("Skipping dmg extraction (file already exists)")

    if "aea" in biggest_dmg_file_path.suffix:
        logger.info("Detected 'aea' in file suffix, starting decryption process")
        result = await decrypt_dmg_aea(file, biggest_dmg_file_path, output)
        if isinstance(result, Error):
            logger.error("Decryption process failed")
            return result

        # removes the last .aea from a path
        biggest_dmg_file_path = (
            biggest_dmg_file_path.parent / biggest_dmg_file_path.stem
        )

    logger.info(f"Extracting bundles from {biggest_dmg_file_path} using 7z")
    command = [
        "7z",
        "x",
        biggest_dmg_file_path,
        f"-o{output}",
        "-aos",  # overwrite
        "-bd",  # no progress
        "-y",
        "System/Library/Carrier Bundles/*",  # where all the bundles are
    ]

    result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    stdout = result.stdout
    stderr = result.stderr

    logger.debug(f"7z stdout: {stdout}")
    logger.debug(f"7z stderr: {stderr}")

    if result.returncode != 0:
        error_msg = f"Couldn't extract {file}, error: {stderr}"
        logger.error(error_msg)
        return Error(error_msg)

    logger.info(f"Cleaning up extracted file: {biggest_dmg_file_path}")
    biggest_dmg_file_path.unlink(missing_ok=True)

    logger.info(f"Cleaning up original IPSW file: {file}")
    file.unlink(missing_ok=True)

    return Ok(None)


async def bundles_glob(path: Path) -> List[str]:
    return glob.glob(f"{path}/System/Library/Carrier Bundles/**/*.bundle")


async def delete_non_bundles(
    base_path: Path, bundles: List[Path]
) -> Result[List[Path], str]:
    for bundle in bundles:
        shutil.move(str(bundle), str(base_path))

    shutil.rmtree(base_path / "System", ignore_errors=True)
    return Ok([base_path / bundle.name for bundle in bundles])


async def tar_and_hash_bundles(
    bundles: List[Path],
) -> Result[List[Dict[str, str | int]], str]:
    output_bundles: List[Dict[str, str | int]] = []

    for bundle in bundles:
        bundle_tar = bundle.with_suffix(".tar")

        with tarfile.open(bundle_tar, "w", format=tarfile.PAX_FORMAT) as tar:
            tar.add(bundle, arcname=bundle.name, recursive=True)

        hash_value = await calculate_hash(bundle_tar)
        output_bundles.append(
            {
                "bundle_name": bundle_tar.stem,
                "tar_file": bundle_tar.name,
                "sha1": hash_value,
                "file_size": bundle_tar.stat().st_size,
                "created_at": datetime.now(UTC).isoformat(),
            }
        )

    return Ok(output_bundles)


async def bake_ipcc(
    response: "Response",
    session: aiohttp.ClientSession,
    semaphore: asyncio.Semaphore,
    group: asyncio.TaskGroup,
):
    for firmware in response.firmwares:

        async def run(firmware):
            async with semaphore:
                try:
                    start_time = datetime.now(UTC)

                    base_path = Path(firmware.identifier)
                    base_path.mkdir(exist_ok=True)

                    version_path = base_path / firmware.version
                    version_path.mkdir(exist_ok=True)

                    json_metadata_path = base_path / "metadata.json"
                    json_metadata_path.touch(exist_ok=True)

                    if firmware.version in json_metadata_path.read_text():
                        return

                    download_result = await download_file(
                        firmware, version_path, session
                    )

                    if isinstance(download_result, Error):
                        return download_result

                    extract_big_result = await extract_the_biggest_dmg(
                        download_result.value, version_path
                    )

                    if isinstance(extract_big_result, Error):
                        return extract_big_result

                    bundles_folders = list(
                        map(lambda s: Path(s), await bundles_glob(version_path))
                    )

                    new_bundles_folders = await delete_non_bundles(
                        version_path, bundles_folders
                    )

                    if isinstance(new_bundles_folders, Error):
                        return new_bundles_folders

                    tarred_with_hash_bundles = await tar_and_hash_bundles(
                        new_bundles_folders.value
                    )

                    for path in new_bundles_folders.value:
                        shutil.rmtree(path)

                    if isinstance(tarred_with_hash_bundles, Error):
                        return tarred_with_hash_bundles

                    bundles_metadata_path = version_path / "bundles.json"
                    bundles_metadata_path.touch(exist_ok=True)

                    tarred_bundles_value = tarred_with_hash_bundles.value

                    await put_metadata(
                        bundles_metadata_path,
                        "bundles",
                        lambda acc: (acc or []) + tarred_bundles_value,
                    )

                    elapsed = (datetime.now(UTC) - start_time).total_seconds()
                    await put_metadata(
                        json_metadata_path,
                        "fw",
                        lambda acc: (acc or [])
                        + [
                            {
                                "version": firmware.version,
                                "buildid": firmware.buildid,
                                "downloaded_at": datetime.now(UTC).isoformat(),
                                "processing_time_sec": elapsed,
                                "sha1": firmware.sha1sum,
                                "status": "processed",
                            }
                        ],
                    )
                except Exception as e:
                    logger.error(f"Something wen wrong, {e}")

        group.create_task(run(firmware))


async def fetch_and_bake(
    session: aiohttp.ClientSession,
    iphone_code: str,
    semaphore: asyncio.Semaphore,
    group: asyncio.TaskGroup,
):
    model = f"iPhone{iphone_code}"
    response = await session.get(
        f"https://api.ipsw.me/v4/device/{model}", params={"type": "ipsw"}
    )

    if response.status == 200:
        parsed_data = Response.from_dict(await response.json())
        bake_result = await bake_ipcc(parsed_data, session, semaphore, group)

        if isinstance(bake_result, Error):
            logger.error(f"Error: {bake_result.error}")
    else:
        logger.error(f"Failed to fetch data for {model}: {await response.text()}")


async def main():
    semaphore = asyncio.Semaphore(5)

    async with aiohttp.ClientSession() as session:
        async with asyncio.TaskGroup() as group:
            for iphone_code in IPHONES_PRODUCT_CODES:
                await fetch_and_bake(session, iphone_code, semaphore, group)


if __name__ == "__main__":
    asyncio.run(main())
