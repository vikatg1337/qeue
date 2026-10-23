import asyncio
import multiprocessing
import subprocess
import sys

async def main():
    bot1 = await asyncio.create_subprocess_exec(
        sys.executable, "bot.py"
    )
    bot2 = await asyncio.create_subprocess_exec(
        sys.executable, "bot2.py"
    )

    await asyncio.gather(
        bot1.wait(),
        bot2.wait(),
    )

if __name__ == "__main__":
    asyncio.run(main())
