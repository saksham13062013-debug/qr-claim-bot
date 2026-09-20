"""
Single process entrypoint: runs the Telegram bot (long polling) on a background
thread and the FastAPI admin server (uvicorn) on the main thread, sharing one
SQLite database. This is what Railway's Procfile invokes.
"""
import asyncio
import threading

import uvicorn

import config
from bot import build_application
import admin_api


def run_bot_thread(loop: asyncio.AbstractEventLoop, application):
    asyncio.set_event_loop(loop)

    async def _run():
        await application.initialize()
        await application.start()
        await application.updater.start_polling(drop_pending_updates=True)

    loop.run_until_complete(_run())
    loop.run_forever()


def main():
    application = build_application()
    admin_api.app.state.bot_application = application

    bot_loop = asyncio.new_event_loop()
    thread = threading.Thread(target=run_bot_thread, args=(bot_loop, application), daemon=True)
    thread.start()

    uvicorn.run(admin_api.app, host="0.0.0.0", port=config.PORT)


if __name__ == "__main__":
    main()
