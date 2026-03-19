"""
scraper.py — Playwright auth + OnSinch API calls + position filtering/parsing.
"""

import asyncio
import json
import logging
import os
from datetime import datetime, timezone
from typing import Any, Optional

import httpx
from playwright.async_api import async_playwright, Browser, BrowserContext, Page

from config import (
    LOGIN_TIMEOUT_MS,
    ONSINCH_BASE_URL,
    POSITIONS_API_BODY,
    POSITIONS_API_HEADERS,
    SESSIONS_DIR,
    API_RETRY_COUNT,
)

logger = logging.getLogger(__name__)

COOKIE_NAME = "Sinch_app_cookie_splendid"


# ---------------------------------------------------------------------------
# Session file helpers
# ---------------------------------------------------------------------------

def _session_path(chat_id: int) -> str:
    return os.path.join(SESSIONS_DIR, f"{chat_id}.json")


def _read_cookie_from_state(chat_id: int) -> Optional[str]:
    """Read the session cookie directly from the saved Playwright state file."""
    path = _session_path(chat_id)
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r") as f:
            state = json.load(f)
        for cookie in state.get("cookies", []):
            if cookie.get("name") == COOKIE_NAME:
                return cookie["value"]
    except Exception as exc:
        logger.warning("Failed to read session state for chat_id %s: %s", chat_id, exc)
    return None


# ---------------------------------------------------------------------------
# Playwright login
# ---------------------------------------------------------------------------

async def _playwright_login(email: str, password: str, chat_id: int) -> str:
    """
    Launch a stealth Chromium browser, log in, save storage state, return cookie.
    Raises RuntimeError on failure.
    """
    from playwright_stealth import stealth_async  # imported here to keep startup fast

    logger.info("Launching Playwright for login (chat_id=%s)", chat_id)
    async with async_playwright() as p:
        browser: Browser = await p.chromium.launch(headless=True)
        context: BrowserContext = await browser.new_context(
            viewport={"width": 1920, "height": 1080},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:148.0) "
                "Gecko/20100101 Firefox/148.0"
            ),
        )
        page: Page = await context.new_page()
        await stealth_async(page)

        try:
            logger.debug("Navigating to login page")
            await page.goto(f"{ONSINCH_BASE_URL}/", timeout=LOGIN_TIMEOUT_MS)

            await page.fill('input[name="email"]', email)
            await page.fill('input[name="password"]', password)
            await page.click('button[type="submit"]')

            # Wait for navigation away from the login page (302 → dashboard)
            try:
                await page.wait_for_url(
                    lambda url: "/users/login" not in url and url != f"{ONSINCH_BASE_URL}/",
                    timeout=LOGIN_TIMEOUT_MS,
                )
            except Exception:
                # Still on login page → credentials failed
                current_url = page.url
                logger.warning("Login failed for chat_id=%s — still on %s", chat_id, current_url)
                raise RuntimeError("Login failed: still on login page after submit")

            # Save storage state
            os.makedirs(SESSIONS_DIR, exist_ok=True)
            await context.storage_state(path=_session_path(chat_id))
            logger.info("Session saved for chat_id=%s", chat_id)

            # Extract cookie
            cookies = await context.cookies()
            for cookie in cookies:
                if cookie["name"] == COOKIE_NAME:
                    return cookie["value"]

            raise RuntimeError(f"Login succeeded but {COOKIE_NAME} cookie not found")
        finally:
            await context.close()
            await browser.close()


# ---------------------------------------------------------------------------
# API call
# ---------------------------------------------------------------------------

async def _call_positions_api(cookie_value: str) -> dict:
    """
    POST to the OnSinch positions API and return the parsed JSON body.
    Raises httpx.HTTPStatusError on non-2xx; returns dict on success.
    """
    headers = {**POSITIONS_API_HEADERS, "Cookie": f"{COOKIE_NAME}={cookie_value}"}

    last_exc: Optional[Exception] = None
    for attempt in range(1, API_RETRY_COUNT + 1):
        try:
            async with httpx.AsyncClient(follow_redirects=False, timeout=20) as client:
                response = await client.post(
                    f"{ONSINCH_BASE_URL}/api",
                    json=POSITIONS_API_BODY,
                    headers=headers,
                )
                if response.status_code in (301, 302, 303, 307, 308):
                    # Redirect → session expired
                    raise httpx.HTTPStatusError(
                        "Session expired (redirect)",
                        request=response.request,
                        response=response,
                    )
                response.raise_for_status()
                return response.json()
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code in (401, 403) or "Session expired" in str(exc):
                raise  # let caller handle re-login
            last_exc = exc
        except Exception as exc:
            last_exc = exc

        if attempt < API_RETRY_COUNT:
            wait = 2 ** attempt
            logger.warning(
                "API call attempt %d/%d failed, retrying in %ds: %s",
                attempt,
                API_RETRY_COUNT,
                wait,
                last_exc,
            )
            await asyncio.sleep(wait)

    raise RuntimeError(f"API call failed after {API_RETRY_COUNT} attempts") from last_exc


# ---------------------------------------------------------------------------
# Filtering & parsing
# ---------------------------------------------------------------------------

def _is_session_expired_error(exc: Exception) -> bool:
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code in (401, 403) or "Session expired" in str(exc)
    return False


def _filter_and_parse(data: dict) -> list[dict]:
    """Apply all filtering rules and return parsed position dicts."""
    entities = data["entities"]
    positions = entities.get("Position", {})
    shifts = entities.get("Shift", {})
    professions = entities.get("Profession", {})
    locations = entities.get("Location", {})
    attendances = entities.get("PositionAttendance", {})

    # Build set of position IDs the user is already booked on
    booked_position_ids: set[int] = set()
    if isinstance(attendances, dict):
        for att in attendances.values():
            booked_position_ids.add(att["position"])

    now_utc = datetime.now(timezone.utc)
    result = []

    for pos_id in data["result"]["positionIds"]:
        pos = positions.get(str(pos_id))
        if pos is None:
            continue

        # Exclusion filters
        if pos.get("role") == 1:
            continue  # standby
        if pos.get("cancelled"):
            continue
        if pos.get("hidden"):
            continue
        if pos.get("freeCapacity", 0) <= 0:
            continue
        if pos.get("applicants"):
            continue  # already applied
        if pos["id"] in booked_position_ids:
            continue  # already booked

        start_time_str = pos["startTime"]
        try:
            start_dt = datetime.fromisoformat(start_time_str)
            if start_dt < now_utc:
                continue  # past
        except ValueError:
            pass

        # Resolve entity references
        shift = shifts.get(str(pos["shift"]), {})
        profession = professions.get(str(pos["profession"]), {})
        location = locations.get(str(pos["location"]), {})

        result.append({
            "id": pos["id"],
            "shift_name": shift.get("name", "Unknown Shift"),
            "profession": profession.get("name", "Unknown Profession"),
            "location": location.get("name", "Unknown Location"),
            "title": pos.get("title", "").strip(),
            "start_time": pos["startTime"],
            "end_time": pos["endTime"],
            "free_capacity": pos.get("freeCapacity", 0),
            "total_capacity": pos.get("totalCapacity", 0),
            "role": pos.get("role", 0),
            "featured": pos.get("featured", False),
            "in_conflict": pos.get("inConflict", False),
            "conflicting_positions": pos.get("conflicting", {}).get("position", []),
            "requirements_failed": pos.get("requirementsFailed", False),
        })

    return result


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

async def scrape_positions(chat_id: int, email: str, password: str) -> list[dict]:
    """
    Main entry point.  Returns a list of filtered, parsed position dicts.
    Raises RuntimeError if authentication fails definitively.
    """
    cookie = _read_cookie_from_state(chat_id)

    if cookie:
        try:
            data = await _call_positions_api(cookie)
            positions = _filter_and_parse(data)
            logger.info(
                "Scraped %d positions for chat_id=%s (session reused)",
                len(positions),
                chat_id,
            )
            return positions
        except Exception as exc:
            if _is_session_expired_error(exc):
                logger.info("Session expired for chat_id=%s, re-logging in", chat_id)
            else:
                raise

    # Need a fresh login
    cookie = await _playwright_login(email, password, chat_id)
    data = await _call_positions_api(cookie)
    positions = _filter_and_parse(data)
    logger.info(
        "Scraped %d positions for chat_id=%s (fresh login)",
        len(positions),
        chat_id,
    )
    return positions


async def login_and_validate(email: str, password: str, chat_id: int) -> list[dict]:
    """
    Used during /start onboarding: perform a fresh login, validate the session,
    return the current positions list.  Raises RuntimeError on failure.
    """
    # Remove any stale session first
    path = _session_path(chat_id)
    if os.path.exists(path):
        os.remove(path)

    cookie = await _playwright_login(email, password, chat_id)
    data = await _call_positions_api(cookie)
    return _filter_and_parse(data)
