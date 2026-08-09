import re
from urllib.parse import urlparse

import psutil
from CloudflareBypasser import CloudflareBypasser
from DrissionPage import ChromiumPage, ChromiumOptions
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from pythonjsonlogger import jsonlogger
import argparse
import logging

log_level = logging.INFO
app = FastAPI()

# Pydantic model for the response
class ClientRequest(BaseModel):
    cmd: str = None
    cookies: list = None
    maxTimeout: int = None
    url: str = None
    postData: str = None

class Solution(BaseModel):
    url: str = None
    status: int = None
    response: str = None
    cookies: list = None
    userAgent: str = None
        
class ClientResponse(BaseModel):
    status: str = None
    message: str = ""
    version: str = "1.0.0"
    solution: Solution = None


# Function to check if the URL is safe
def is_safe_url(url: str) -> bool:
    parsed_url = urlparse(url)
    ip_pattern = re.compile(
        r"^(127\.0\.0\.1|localhost|0\.0\.0\.0|::1|10\.\d+\.\d+\.\d+|172\.1[6-9]\.\d+\.\d+|172\.2[0-9]\.\d+\.\d+|172\.3[0-1]\.\d+\.\d+|192\.168\.\d+\.\d+)$"
    )
    hostname = parsed_url.hostname
    if (hostname and ip_pattern.match(hostname)) or parsed_url.scheme == "file":
        return False
    return True


def kill_process_tree(pid, request_url):
    """Force-kill a pid and all its children without going through CDP.

    Backstop for page.quit(force=True): DrissionPage only reaches its own psutil kill
    if a 'SystemInfo.getProcessInfo' CDP call succeeds, which it won't once the browser
    connection is already broken. This never talks to the browser at all.
    """
    if pid is None:
        return
    try:
        proc = psutil.Process(pid)
    except psutil.NoSuchProcess:
        return

    procs = proc.children(recursive=True) + [proc]
    for p in procs:
        try:
            p.kill()
        except psutil.NoSuchProcess:
            pass
        except Exception as e:
            logger.error("Failed to force-kill leftover process %s: %s", getattr(p, "pid", "?"), str(e),
                         extra={'requestUrl': request_url})
    psutil.wait_procs(procs, timeout=5)


def cleanup(page, browser_pid, display, request_url):
    """Guaranteed teardown: each step is independent so one failure can't skip the rest.

    Previous cleanup attempts (see PR #9) chained page.listen.stop()/page.quit()/
    display.stop() so that an exception in an earlier call skipped the later ones --
    including the one call that actually terminates the browser process.
    """
    if page is not None:
        try:
            page.listen.stop()
        except Exception as e:
            logger.error("Error stopping page.listen: %s", str(e), extra={'requestUrl': request_url})
        try:
            page.quit(force=True, timeout=10)
        except Exception as e:
            logger.error("Error in page.quit(): %s", str(e), extra={'requestUrl': request_url})

    kill_process_tree(browser_pid, request_url)

    if display is not None:
        try:
            display.stop()
        except Exception as e:
            logger.error("Error stopping VirtualDisplay: %s", str(e), extra={'requestUrl': request_url})


# Function to bypass Cloudflare protection
def bypass_cloudflare(url: str, retries: int):
    logger.info("Configuring ChromiumPage", extra={'requestUrl': url})
    options = ChromiumOptions()
    options.set_paths(browser_path="/usr/bin/chromium-browser").headless(False).auto_port()
    options.set_argument("--no-sandbox")  # Necessary for Docker
    options.set_argument("--disable-gpu")  # Optional, helps in some cases
    options.set_argument("--accept-lang=en-US")  # Optional, set language

    page = ChromiumPage(addr_or_opts=options)
    browser_pid = page.browser.process_id
    try:
        logger.debug("Fetch ChromiumPage", extra={'requestUrl': url})
        page.listen.start(targets=url)
        page.get(url)

        logger.debug("Apply bypass to ChromiumPage", extra={'requestUrl': url})
        cf_bypasser = CloudflareBypasser(page, retries, logger)
        cf_bypasser.bypass()
        return page, browser_pid
    except Exception:
        cleanup(page, browser_pid, None, url)
        raise

# Endpoint to get Solver response
@app.post("/v1")
async def get_solverr(request: ClientRequest):
    from pyvirtualdisplay import Display
    if not is_safe_url(request.url):
        raise HTTPException(status_code=400, detail="Invalid URL")

    display = None
    page = None
    browser_pid = None
    try:
        if request.cmd == "request.get":
            logger.info("Trying to solve", extra={'request': request})

            # Start Xvfb for Docker
            logger.debug("Start VirtualDisplay", extra={'requestUrl': request.url})
            display = Display(visible=0, size=(1920, 1080))
            display.start()

            # Start bypass
            logger.debug("Start ByPassing", extra={'requestUrl': request.url})
            page, browser_pid = bypass_cloudflare(request.url, 5)
            packet = page.listen.wait()
            cookies = page.cookies(as_dict=False)

            # Build response
            res = ClientResponse()
            res.status = "ok"
            res.solution = Solution(
                url = packet.response.url,
                status = packet.response.status,
                response = page.html,
                userAgent = page.user_agent,
                cookies = cookies,
            )

            return res
    except Exception as e:
        logger.error("An error occured while solving with error: %s", str(e), stack_info=True, extra={'requestUrl': request.url})
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        logger.debug("Closing ChromiumPage and VirtualDisplay", extra={'requestUrl': request.url})
        cleanup(page, browser_pid, display, request.url)


# Main entry point
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Cloudflare bypass api")
    parser.add_argument("--debug", action="store_true", help="Disable logging")
    parser.add_argument("--port", help="Port to bind service", default=8000)

    args = parser.parse_args()
    if args.debug:
        log_level = logging.DEBUG

    # retrieve default logger
    logger = logging.getLogger("root")
    logHandler = logging.StreamHandler()
    
    # set Json formater
    formatter = jsonlogger.JsonFormatter(timestamp=True)
    logHandler.setFormatter(formatter)

    # setup level and handlers to default logger
    logger.handlers = [logHandler]
    logger.setLevel(log_level)

    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(args.port), log_config=None)

