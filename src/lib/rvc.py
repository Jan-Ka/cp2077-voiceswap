import os
import asyncio
from tqdm import tqdm
from itertools import chain
from util import Parallel, SubprocessException, find_files
import lib.ffmpeg as ffmpeg
import config


async def poetry_get_venv(path: str):
    process = await asyncio.create_subprocess_exec(
        "poetry",
        "env",
        "list",
        "--full-path",
        cwd=path,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, _stderr = await process.communicate()
    return stdout.splitlines()[0].decode()


async def get_rvc_executable():
    rvc_path = os.getenv("RVC_PATH")
    venv = os.getenv("RVC_VENV")
    if venv is None or venv == "":
        venv = await poetry_get_venv(rvc_path)
    return os.path.join(rvc_path, venv, "python.exe")


async def uvr(input_path: str, output_vocals_path: str, output_rest_path: str):
    """Splits audio files to vocals and the rest."""

    cwd = os.getcwd()

    parallel = Parallel("Isolating vocals")

    uvr_process = await asyncio.create_subprocess_exec(
        await get_rvc_executable(),
        os.path.join(cwd, "libs\\rvc_uvr.py"),
        os.path.join(cwd, config.TMP_PATH),
        os.path.join(cwd, output_vocals_path),
        os.path.join(cwd, output_rest_path),
        cwd=os.getenv("RVC_PATH"),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
    )

    callbacks = {}
    loop = asyncio.get_event_loop()

    async def submit(file: str):
        future = loop.create_future()

        def callback():
            future.set_result(None)
            del callbacks[file]

        callbacks[file] = callback
        uvr_process.stdin.write((file + "\n").encode())
        await uvr_process.stdin.drain()

        return await future

    async def checker():
        while not uvr_process.stdout.at_eof():
            try:
                line = await asyncio.wait_for(uvr_process.stdout.readline(), 5)
                file = line.decode().strip()
                if file in callbacks:
                    callbacks[file]()
                else:
                    tqdm.write(file)
            except asyncio.TimeoutError:
                pass

    async def process(path):
        os.makedirs(os.path.join(config.TMP_PATH, os.path.dirname(path)), exist_ok=True)

        tmp_path = path + ".reformatted.wav"
        await ffmpeg.convert(
            os.path.join(input_path, path),
            os.path.join(config.TMP_PATH, tmp_path),
            "-vn",
            *("-c:a", "pcm_s16le"),
            *("-ac", "2"),
            *("-ar", "44100"),
        )
        await submit(tmp_path)

    for path in find_files(input_path):
        parallel.run(process, path)

    await asyncio.gather(parallel.wait(), checker())

    uvr_process.stdin.write_eof()

    result = await uvr_process.wait()

    if result != 0:
        raise SubprocessException(f"Converting files failed with exit code {result}")


async def batch_rvc(input_path: str, opt_path: str, **kwargs):
    """Run RVC over given folder."""

    cwd = os.getcwd()

    tqdm.write("Starting RVC...")

    _input_path = os.path.join(cwd, input_path)
    _opt_path = os.path.join(cwd, opt_path)

    os.makedirs(_opt_path, exist_ok=True)

    process = await asyncio.create_subprocess_exec(
        await get_rvc_executable(),
        os.path.join(cwd, "libs\\infer_batch_rvc.py"),
        *("--input_path", _input_path),
        *("--opt_path", _opt_path),
        *chain(*(("--" + k, str(v)) for k, v in kwargs.items() if v is not None)),
        cwd=os.getenv("RVC_PATH"),
    )
    result = await process.wait()

    if result != 0:
        raise SubprocessException(f"Converting files failed with exit code {result}")
