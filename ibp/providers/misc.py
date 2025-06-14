"""Misc. methods for IBP inmate providers."""

import asyncio


async def run_curl_exec(args: list[str], timeout: float | None = None) -> bytes:
    """Run a curl command with given arguments."""
    command = ["curl"]
    command.extend(args)

    if timeout is not None:
        command.extend(["--max-time", str(float(timeout))])

    process = await asyncio.create_subprocess_exec(
        *command, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )

    try:
        stdout, *_ = await asyncio.wait_for(process.communicate(), timeout=timeout)
    except asyncio.TimeoutError as error:
        if process.returncode is None:  # Check if process is still running
            process.terminate()
            await process.wait()  # Wait for the process to actually terminate
        message = f"curl command timed out after {timeout} seconds."
        raise TimeoutError(message) from error

    if process.returncode != 0:
        message = f"curl command failed with exit code {process.returncode}"
        raise RuntimeError(message)

    return stdout
