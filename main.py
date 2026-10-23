import asyncio
import sys
import traceback

async def run_bot(filename):
    print(f"[launcher] Starting {filename}", flush=True)
    process = await asyncio.create_subprocess_exec(
        sys.executable, "-u", filename
    )
    print(
        f"[launcher] {filename} started, PID={process.pid}",
        flush=True,
    )
    code = await process.wait()
    print(
        f"[launcher] {filename} exited with code {code}",
        flush=True,
    )

async def main():
    await asyncio.gather(
        run_bot("bot.py"),
        run_bot("bot2.py"),
    )

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception:
        traceback.print_exc()
        raise
